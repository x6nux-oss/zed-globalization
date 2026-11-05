# 翻译流水线改用 Claude Code + MCP tool

日期：2026-11-06
状态：已确认，待实现
前置文档：`2026-11-06-opencode-translation-design.md`（opencode 方案，因兼容性问题作废，
保留其根因分析作为决策依据）

## 背景

现有翻译流程（`src/zedl10n/translate.py`）由 Python 并发调用 OpenAI 兼容接口，
靠 JSON → XML → 编号格式三级降级解析模型的自由文本输出。该降级链存在的唯一原因是
模型输出格式不可靠。本次改造把翻译执行交给 AI agent，译文通过 MCP tool 结构化落盘，
从根上消除"解析自由文本"这一环。

原定使用 opencode，实测发现与本项目指定的 provider 存在协议不兼容（见下节），
改用 Claude Code。**改用并非退让**：Claude Code 走 Anthropic 原生协议，
恰好绕开该 provider 在 OpenAI 端点上的缺陷。

## 关键技术发现：为何弃用 opencode

### provider 的 OpenAI 兼容端点违反 SSE 规范

逐字节验证指定 provider（`https://nekocode.ai/v1/chat/completions`）：

```
块1 结尾字节 b'"}\n'  → 缺少空行分隔
按 SSE 规范解析 → 事件0 非法 JSON: Extra data: line 2 column 1
```

该端点在拼接两个后端（响应 id 分别为 `chatcmpl-3hn3e...` 与 `01a1aca7...`）时，
**第一个 SSE 事件后漏了空行**。按规范事件须以空行终止，否则相邻事件被拼成一条非法 JSON。
opencode 使用严格 SSE 解析器，直接抛：

```
Error: Invalid ai/openai-compatible-chat stream event
```

**纯文本对话同样失败**，证明与 tool 机制无关，根因在 provider 的流式实现。
非流式正常，这也解释了老的 `_call_ai`（OpenAI Python SDK，容错更宽）为何一直未踩坑。

### Anthropic 原生端点完全合规

同一 provider 的 `/v1/messages` 实测：

- 26 个事件全部为合法 JSON
- `event:` / `data:` / 空行分隔规范
- `message_stop` 收尾完整
- 返回标准 Anthropic 结构（`content` 数组含 `thinking` / `text` 块）

Claude Code 使用该原生协议，因此可正常工作。

## 决策记录

| 决策点 | 选择 |
| --- | --- |
| 执行引擎 | Claude Code（因 provider 的 OpenAI 端点 SSE 不合规） |
| 版本策略 | 固定 `2.1.280`（本机已实测验证的版本） |
| tool 实现 | Python MCP stdio server（与项目同语言，无新运行时） |
| 工作单元 | 一个批次一个会话 |
| 批次粒度 | 200 条/会话 |
| 调用方式 | 子进程 + 并发池 |
| 旧 API 路径 | 完全删除，不留降级兜底 |
| 模型 | 环境变量强制覆盖，默认 `deepseek-v4.1-flash` |
| 思考强度 | `--effort low` |

## 本机实测结论（Claude Code 2.1.280）

1. **无头调用跑通**：`claude -p "..." --output-format json` 返回
   `subtype: success`，8 秒完成。

2. **MCP tool 调用跑通**：`--mcp-config` + `--strict-mcp-config` 加载 Python
   stdio server，agent 调用 `mcp__l10n__submit_translation` 成功写入 JSONL。

3. **自定义 agent 加载跑通**：`.claude/agents/translator.md` 配 `tools` 白名单，
   `--agent translator` 生效。

4. **模型强制覆盖已验证**。以下环境变量同时设置后，请求 `sonnet` 别名、
   或不传 `--model`，实际均路由到指定模型：

   ```
   ANTHROPIC_MODEL
   ANTHROPIC_DEFAULT_MODEL
   ANTHROPIC_DEFAULT_HAIKU_MODEL
   ANTHROPIC_DEFAULT_SONNET_MODEL
   ANTHROPIC_DEFAULT_OPUS_MODEL
   ANTHROPIC_DEFAULT_FABLE_MODEL
   ANTHROPIC_SMALL_FAST_MODEL
   CLAUDE_CODE_SUBAGENT_MODEL
   CLAUDE_CODE_NO_MODEL_FALLBACK=1
   ```

   实测 `modelUsage` 只有 `deepseek-v4.1-flash` 一个条目，无隐藏模型兜底。
   `ANTHROPIC_SMALL_FAST_MODEL` 与 `CLAUDE_CODE_SUBAGENT_MODEL` 尤为关键：
   前者是后台小任务模型，后者是子 agent 模型，不覆盖会偷偷请求未指定模型。

5. **必须隔离全局配置**。不传 `--agent` 时会加载用户级 `~/.claude` 配置
   （实测回复带上了用户全局 CLAUDE.md 的人格设定）。CI 中须用
   `--agent translator` 配合 `--setting-sources` 限定配置来源。

6. **`--effort low` 存在**（对应 opencode 方案中的 `reasoningEffort: low` 诉求）。
   翻译属格式转换型任务，无需深度推理。

7. **`unrecognized_model` 警告可忽略**：每次调用 stderr 会出现
   `[claude-code:unrecognized_model]`，因该模型不在内置目录中，实测不影响运行。

## 架构

```
extract.py（不变）→ string.json / string_context.json
        ↓
oc_translate.py：按文件合批（200 条/会话）
        ↓
每批一个 Claude Code 会话（并发池控制）
  claude -p "<批次提示词>" \
    --agent translator --model $AI_MODEL \
    --output-format stream-json \
    --permission-mode bypassPermissions \
    --mcp-config <生成的 mcp.json> --strict-mcp-config \
    --setting-sources project
        ↓ agent 通过 MCP tool 干活
  read              → 读 zed/ 源文件取上下文
  glossary_lookup   → 查术语表既定译法
  submit_translation→ 追加写 out/<batch_id>.jsonl
  mark_untranslatable → 追加写 dnt/<batch_id>.jsonl
        ↓ 会话结束，Python 收 JSONL
占位符校验（复用 validate_placeholders）
  → 术语表一致性修复（复用 consistency.py）
  → normalize_fullwidth → save_json
```

## 组件

### 新增：`src/zedl10n/mcp_server.py`

MCP stdio server，注册三个 tool。运行上下文（批次 id、输出目录、词表路径）
经环境变量传入。MCP SDK 固定版本。

- `glossary_lookup(term)` — 查术语表；`terms` 返回约定译法，
  `keep_original` 内的专有名词返回"保持原文不译"
- `submit_translation(original, translated)` — 校验后追加写 `out/<batch_id>.jsonl`
- `mark_untranslatable(original, reason)` — 追加写 `dnt/<batch_id>.jsonl`

追加写入保证会话中途被打断时已完成条目不丢失。

术语表为 YAML（`{terms: {...}, keep_original: [...]}`），因 YAML 已在项目依赖中，
直接用 `pyyaml` 解析，无需自实现。

### 新增：`src/zedl10n/oc_translate.py`

批次分组、生成 `mcp.json`、拼装命令行、起子进程、收集 JSONL、合并结果。
对外暴露 `translate_batches(...)` 供 `translate.py` 调用。

### 新增：`.claude/agents/translator.md`

翻译专用 agent。`tools` 白名单限定为 `Read` 与三个 MCP tool。
译文只经 tool 落盘，不在正文输出。

### 改造：`src/zedl10n/translate.py`

- 删除 `_call_ai`、`_fetch_translation` 及三级降级链
- 保留 `_translate_batch` 的占位符校验与最终丢弃逻辑（作用于 JSONL 收集结果）
- `translate_all` 保留为编排入口，内部改调 `oc_translate`
- 保留时限机制：到时限取消未完批次、保存已完成部分、退出码 75

### 改造：`src/zedl10n/batch.py`

从"按字符预算切批次"改为"按文件合批"：

- 按声明顺序累积文件，达 200 条封批
- 单文件超 300 条单独成批，并按字符预算继续拆
- 字符预算逻辑保留，用于超大文件

### 改造：`.github/workflows/01-translate.yml`

- 安装 Claude Code，**固定版本 2.1.280**
- 设置上述九个环境变量强制模型覆盖
- 渲染 `mcp.json` 后调用翻译

### 改造：`pyproject.toml`

加 `l10n` extra，固定 `claude-code` 与 MCP SDK 版本。

## 会话预算

原 opencode 方案的 5~10 秒/会话数据在 provider 限流前测得，不可靠。
Claude Code 实测：简单对话 8 秒、含一次 tool 调用 11 秒。

按 200 条/会话估算：

- 总条目 52947 → 约 265 个会话
- 并发 10，保守按 120 秒/会话（含读源码的复杂批次）：
  `265 × 120 ÷ 10 ≈ 53 分钟`

落在 5h50m 预算内。**实现后需实测校准**，据此调整并发与批次粒度。

## 错误处理

不设降级通道。会话失败（退出码非 0 / 超时 / JSONL 为空）时：

- 该批次所有条目保持空值，记失败日志
- 空值在下次增量运行时自动作为缺口重翻（现有逻辑已如此）
- 单会话超时 300 秒（可配）；全局受 5h50m 时限约束，到时限存进度退出码 75

失败代价是"这批留到下次"，不是静默改走其他路径。

## 测试

沿用 `test_zedl10n.py` 的裸 assert 风格，不引测试框架：

- 按文件合批的确定性与边界：恰好 200 条、超大文件、空文件、空输入
- JSONL 收集与合并：正常、缺行、字段缺失、重复条目
- 占位符校验在 JSONL 路径上仍生效
- 会话失败时缺口保留且不污染已有译文
- MCP server 的 tool 逻辑单测（直接调函数，不经协议层）

另留手工冒烟脚本，取 20~30 个真实批次跑通，观察质量与耗时，不进 CI。

## 风险

1. **provider 稳定性**：探针期间遇到过账号限流（`all Command Code accounts are
   rate limited`）与冷启动 522。CI 中需容错重试，失败批次留待下次。
2. **Claude Code 版本漂移**：固定 2.1.280。升级前需重跑冒烟脚本验证。
3. **模型 tool 调用可靠性**：若模型不调 tool 直接吐文本，该批次 JSONL 为空即判失败。
4. **`unrecognized_model`**：模型不在内置目录，可能影响上下文窗口推断。
   实测 contextWindow 报 200000，与 provider 能力相符，暂不处理。
