# 翻译流水线改用 opencode + 自定义 tool

日期：2026-11-06
状态：已确认，待实现

## 背景与目标

现有翻译流程（`src/zedl10n/translate.py`）由 Python 直接并发调用 OpenAI 兼容接口，
再靠 JSON → XML(CDATA) → 编号格式三级降级解析模型的自由文本输出。这套降级链存在的
唯一原因是模型输出格式不可靠：一旦 JSON 里少个引号、多个逗号，整批翻译就得重来。

本次改造把翻译执行交给 opencode agent，模型改用结构化 tool 调用提交译文，
按 tool 参数逐条落盘，从根上消除"解析自由文本"这一环。

范围（经确认）：**opencode 主导翻译，Python 负责字符串提取与最终校验**。
保留 Python 侧已有的提取、批次切分、占位符校验、禁翻清单、术语表一致性校验、
增量续翻与时限机制；只替换"调用模型产出译文"这一段。

## 决策记录

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 改造范围 | opencode 主导翻译，Python 做提取校验 | 质量提升来自 agent 能读源码、自查 |
| 工作单元 | 一个批次一个会话 | 会话数可控，agent 仍能按需读上下文 |
| 工具集 | read（自带）+ glossary_lookup + submit_translation + mark_untranslatable | 覆盖读上下文、查词表、落盘、标记禁翻 |
| 调用方式 | 子进程 + 并发池 | 与现有 asyncio 结构最接近 |
| 旧 API 路径 | 完全删除，不留降级兜底 | 代码干净；失败代价是"留到下次"而非静默换路 |
| 模型 | 可配置，默认 `xiaomi/mimo-v2.6-flash`（实测通过） | 避免写死，CI 可换 |
| 批次粒度 | 200 条/会话 | 见"会话预算" |
| provider 名 | `ai` | 调用形如 `--model ai/$AI_MODEL` |

## 本机实测结论（opencode v2.2.5）

以下均为实跑验证，非文档推断：

1. **V1 的 `tool()` 写法已废弃**。官方文档中
   `import { tool } from "@opencode-ai/plugin"` + `.opencode/tool/xxx.ts`
   是 V1 形态。V2 必须在插件里注册：

   ```ts
   export default {
     id: "l10n",
     async setup(ctx) {
       await ctx.tool.transform((editor) => {
         editor.add({
           name: "submit_translation",
           description: "...",
           input: { type: "object", properties: {...}, required: [...], additionalProperties: false },
           async execute(input) { return { content: "recorded" } },
         })
       })
     },
   }
   ```

   插件放 `.opencode/plugin/*.ts`，实测加载成功且 agent 能调用。

2. **模型通过 codemode 调用 tool**，实测调用形态：
   `execute {"code":"return await tools.submit_translation({...});"}`。
   即 tool 参数经一层 JS 包装传入，返回值须为 `{ content: string }`。

3. **单次无头会话约 5~10 秒**（含一次文件读取）。**注意**：该数据在 provider
   限流生效前测得，仅供参考（见实测结论 8）。真实成本需在有可用配额时复测。

4. **全局默认 agent 会介入**。本机 `~/.config/opencode/opencode.json` 的
   `default_agent` 指向 `unrestricted`，不显式指定 `--agent` 时该 agent 被加载。
   CI 中必须显式传 `--agent translator`。

5. **模型可用性差异**：`cmdcode/xiaomi/mimo-v2.6-flash` 可用；
   `lfree/*` 系列在无头模式下挂起（上游通道问题，非 opencode 故障）。
   故默认模型取前者，且做成可配置。

8. **探针期间 provider 被限流，导致中途大量"挂起"**。排查后确认根因在 provider
   侧而非 opencode 或 tool 机制：

   ```
   AI.Error.RateLimit: all Command Code accounts are rate limited;
     next account available in 22h15m26s
   AI.Error.ProviderInternal: No available channel for model
     deepseek-v4.1-flash under group openai (distributor)
   ```

   表现为会话长时间无输出直到超时，且纯文本调用与 tool 调用**同样**受影响，
   证明与 tool 机制无关。踩坑过程中的误判记录如下，供后续调试参考：

   - 曾误以为自定义 agent 或 `reasoningEffort` 字段导致挂起。对照实验
     （同一 tool、同一模型，仅切换 `--agent build`）同样挂起，排除该假设。
   - 判定方法：若纯文本调用也挂起，则问题在 provider 侧；若仅 tool 调用挂起，
     才需要怀疑 tool 机制。

   **尚未验证**：高负载下的稳定性、真实吞吐成本。实现后需在有可用配额时复测，
   并据此校准"会话预算"一节的并发与批次参数。

9. **`reasoningEffort: low` 生效路径已验证**。写进 agent frontmatter 后，
   `opencode debug agents` 显示其被解析到 `request.body.reasoningEffort: "low"`，
   由 opencode 透传给 provider。字段名与文档一致，非猜测。

6. **无头调用形态**：

   ```
   opencode run --standalone --auto --agent translator \
     --model ai/<model> [--format json] "<prompt>"
   ```

   `--standalone` 起私有服务，避免依赖后台常驻服务。
   `--format json` 输出 JSONL 事件流，每行含 `type`（step_start/text/step_finish 等）。

7. **自定义 provider 配置**：

   ```json
   {
     "$schema": "https://opencode.ai/config.json",
     "provider": {
       "ai": {
         "npm": "@ai-sdk/openai-compatible",
         "name": "Translation AI",
         "options": { "baseURL": "{env:AI_BASE_URL}", "apiKey": "{env:AI_API_KEY}" },
         "models": { "<model-id>": { "name": "<model-id>" } }
       }
     }
   }
   ```

## 架构

```
extract.py（不变）→ string.json / string_context.json
        ↓
oc_translate.py：按文件合批（目标 200 条/会话）
        ↓
每批一个 opencode 会话（并发池控制）
  opencode run --standalone --auto --agent translator --model ai/$AI_MODEL
        ↓ agent 通过 tool 干活
  read              → 读 zed/ 下源文件，按需取上下文
  glossary_lookup   → 查术语表既定译法
  submit_translation→ 追加写 out/<batch_id>.jsonl
  mark_untranslatable → 追加写 dnt/<batch_id>.jsonl
        ↓ 会话结束，Python 收 JSONL
占位符校验（复用 validate_placeholders）
  → 术语表一致性修复（复用 consistency.py）
  → normalize_fullwidth → save_json
```

核心变化：模型不再输出整块 JSON，改为用 tool 逐条提交，落盘即结构化数据。

## 组件

### 新增：`.opencode/plugin/l10n.ts`

注册三个 tool。从环境变量读运行上下文（批次 id、输出目录、词表路径、源码根），
避免每个会话重新传参。tool 职责：

- `glossary_lookup(term)` — 读术语表返回既定译法，未收录返回提示
- `submit_translation(original, translated)` — 校验参数后追加写 `out/<batch_id>.jsonl`
- `mark_untranslatable(original, reason)` — 追加写 `dnt/<batch_id>.jsonl`

追加写入（而非整文件重写）保证会话中途被打断时已完成条目仍在。

### 新增：`.opencode/agent/translator.md`

翻译专用 agent，`mode: primary`，权限限制为：可读 `zed/` 与运行目录，
可调用上述三个 tool，不执行 shell。必须显式指定，原因见实测结论 4。

frontmatter 中设 `reasoningEffort: low`（见实测结论 9）：翻译是格式转换型任务，
不需要深度推理，压低思考强度可显著减少 token 消耗与延迟。

### 新增：`opencode.json.template`

provider 配置模板，模型字段留占位，由驱动在运行前渲染出 `opencode.json`。

### 新增：`src/zedl10n/oc_translate.py`

职责：批次分组、渲染配置、起子进程、收集 JSONL、合并结果。
对外暴露 `translate_batches(...)`，由 `translate.py` 调用。

### 改造：`src/zedl10n/translate.py`

- 删除 `_call_ai`、`_fetch_translation`、`XML_FALLBACK_INSTRUCTION` 三级降级链
- 保留 `_translate_batch` 的占位符校验与最终丢弃逻辑（改为作用于 JSONL 收集结果）
- `translate_all` 保留为编排入口，内部改调 `oc_translate`
- 保留时限机制：到时限取消未完批次、保存已完成部分、退出码 75

### 改造：`src/zedl10n/batch.py`

从"按字符预算切批次"改为"按文件合批"：

- 按声明顺序累积文件，达到 200 条即封一批
- 单文件超过 300 条时单独成批，并按字符预算继续拆分
- 字符预算逻辑（`MAX_INPUT_CHARS`、源文件智能截断）保留，用于超大文件

### 改造：`.github/workflows/01-translate.yml`

- 安装 opencode CLI
- 传入 `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL`
- 渲染 `opencode.json` 后调用翻译

## 会话预算

实测单次会话约 10 秒。若沿用旧粒度（一批 = 一次 API 请求），52947 条字符串会切出
上千个会话，启动开销即数小时，必然击穿 5h50m 预算。

按 200 条/会话估算：

- 总条目 52947 → 约 265 个会话
- 并发 10，保守按 90 秒/会话：`265 × 90 ÷ 10 ≈ 40 分钟`

远在预算内，留有充足余量应对个别会话变慢。

## 错误处理

不设降级通道。会话失败（退出码非 0 / 超时 / JSONL 为空）时：

- 该批次所有条目保持空值，记失败日志
- 空值在下次增量运行时自动作为缺口重翻（现有逻辑已如此处理）
- 单会话超时 300 秒（可配）；全局仍受 5h50m 时限约束，到时限存进度退出码 75

失败的代价是"这批留到下次"，不是静默改用其他路径。

## 测试

沿用 `test_zedl10n.py` 的裸 assert 风格，不引入测试框架：

- 文件合批的确定性与边界：恰好 200 条、超大文件、空文件、空字符串输入
- JSONL 收集与合并：正常、缺行、字段缺失、重复条目
- 占位符校验在 JSONL 路径上仍生效
- 会话失败时缺口保留且不污染已有译文

另留一个手工冒烟脚本，取 20~30 个真实批次跑通，观察质量与耗时，不进 CI。

## 风险

1. **opencode CLI 版本漂移**：V1 → V2 已破坏性变更一次，插件 API 仍有变动可能。
   缓解：CI 中固定 opencode 版本号。
2. **模型 tool 调用可靠性**：部分模型对 function calling 支持较弱，可能出现
   不调 tool 直接吐文本。缓解：prompt 明确要求走 tool，成功后加校验（JSONL 为空即失败）。
3. **CI 中 opencode 认证**：`--standalone` 需把 provider key 以环境变量传入。
   缓解：用 `{env:...}` 插值，密钥只走 secrets。
