# 翻译流水线改用 Claude Code + MCP tool 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把翻译执行从"Python 并发调 OpenAI 兼容接口 + JSON/XML/编号三级降级解析"换成
"Claude Code 会话 + MCP tool 结构化落盘"，消除解析自由文本这一环。

**Architecture:** Python 侧保留字符串提取、批次分组、占位符校验、禁翻清单、一致性修复、
时限续翻；只替换"调用模型产出译文"这段。每个批次起一个 `claude -p` 子进程，
agent 通过本地 MCP stdio server 暴露的三个 tool 把译文逐条追加写入 JSONL，
Python 收 JSONL 后走原有校验链。

**Tech Stack:** Python 3.11+、`mcp>=2.2.0`（`MCPServer`）、Claude Code `2.1.280`（npm 固定版本）、
`pyyaml`、asyncio

**Spec:** `docs/superpowers/specs/2026-11-06-claude-code-translation-design.md`

## Global Constraints

- Claude Code 版本固定 **`2.1.280`**，不得用 `@latest`。
- `mcp` 依赖固定 **`>=2.2.0,<3`**。该 SDK 已发生一次破坏性改名
  （v2 中 `FastMCP` → `MCPServer`），放宽上界会引入同类风险。
- MCP server 必须用 `from mcp.server.mcpserver import MCPServer`，
  **不要**用 `mcp.server.fastmcp`（mcp 2.x 中该模块已不存在）。
- 模型经环境变量强制覆盖，九个变量一个都不能少（见 Task 5）：
  `ANTHROPIC_MODEL`、`ANTHROPIC_DEFAULT_MODEL`、`ANTHROPIC_DEFAULT_HAIKU_MODEL`、
  `ANTHROPIC_DEFAULT_SONNET_MODEL`、`ANTHROPIC_DEFAULT_OPUS_MODEL`、
  `ANTHROPIC_DEFAULT_FABLE_MODEL`、`ANTHROPIC_SMALL_FAST_MODEL`、
  `CLAUDE_CODE_SUBAGENT_MODEL`、`CLAUDE_CODE_NO_MODEL_FALLBACK=1`。
- 调用 Claude Code 必须带 `--agent translator` 与 `--setting-sources project`，
  否则会加载用户级 `~/.claude` 配置导致结果不可复现。
- 批次粒度 **200 条/会话**；单文件超 **300 条**单独成批。
- 不保留旧的 API 调用路径，不做降级兜底。
- 代码注释用中文，与现有代码库一致。

---

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `src/zedl10n/batch.py`（改造） | 从"按字符切批"改为"按文件合批" |
| `src/zedl10n/mcp_server.py`（新增） | MCP stdio server，暴露三个 tool |
| `src/zedl10n/oc_translate.py`（新增） | 生成 mcp.json、起子进程、收 JSONL |
| `src/zedl10n/translate.py`（改造） | 编排入口改调 oc_translate；删三级降级 |
| `src/zedl10n/prompts.py`（改造） | 删 XML/编号降级指令，加批次提示词构建 |
| `src/zedl10n/utils.py`（改造） | 删 `parse_xml_response` / `parse_numbered_response` |
| `.claude/agents/translator.md`（新增） | 翻译专用 agent 定义 |
| `.github/workflows/01-translate.yml`（改造） | 装 Claude Code、设环境变量 |
| `pyproject.toml`（改造） | 加 `l10n` extra |
| `test_zedl10n.py`（改造） | 新增各任务测试 |
| `scripts/smoke_translate.py`（新增） | 手工冒烟脚本，不进 CI |

---

### Task 1: 按文件合批

把 `split_batch` 的调用语义从"单文件内按字符切批"改为"跨文件按条目数合批"。
超大文件仍走字符预算拆分。

**Files:**
- Modify: `src/zedl10n/batch.py`
- Test: `test_zedl10n.py`

**Interfaces:**
- Consumes: 无（本任务自足）
- Produces:
  - `group_batches(all_strings: dict[str, dict[str, str]], target_size: int = 200, oversize: int = 300) -> list[dict[str, dict[str, str]]]`
    — 返回批次列表，每批是 `{file_path: {原文: ""}}`
  - 保留既有 `split_batch(...)` 签名不变，供超大文件复用

- [ ] **Step 1: 写失败测试**

追加到 `test_zedl10n.py`（放在 `main()` 之前）：

```python
def test_group_batches_by_file_accumulation() -> None:
    """按文件累积合批：小文件凑满 target_size 才封批"""
    strings = {
        "a.rs": {f"s{i}": "" for i in range(120)},
        "b.rs": {f"t{i}": "" for i in range(120)},
        "c.rs": {"u0": ""},
    }
    batches = group_batches(strings, target_size=200)
    assert len(batches) == 2, [list(b) for b in batches]
    assert list(batches[0]) == ["a.rs", "b.rs"], list(batches[0])
    assert list(batches[1]) == ["c.rs"]


def test_group_batches_oversize_file_alone() -> None:
    """单文件超 oversize 门槛时单独成批，不与小文件混装"""
    strings = {
        "small.rs": {"s0": ""},
        "huge.rs": {f"h{i}": "" for i in range(350)},
        "tail.rs": {"t0": ""},
    }
    batches = group_batches(strings, target_size=200, oversize=300)
    names = [list(b) for b in batches]
    assert ["small.rs"] in names, names
    assert ["huge.rs"] in names, names
    assert ["tail.rs"] in names, names


def test_group_batches_exact_boundary_and_empty() -> None:
    """恰好等于 target_size 时封批；空输入返回空列表"""
    assert len(group_batches({}, target_size=200)) == 0
    exact = {"a.rs": {f"s{i}": "" for i in range(200)}}
    assert len(group_batches(exact, target_size=200)) == 1


def test_group_batches_preserves_all_entries() -> None:
    """合批不得丢失或改写任何条目"""
    strings = {
        "a.rs": {"Save": "", "Open": ""},
        "b.rs": {"Quit": ""},
    }
    batches = group_batches(strings, target_size=200)
    merged: dict[str, dict[str, str]] = {}
    for b in batches:
        for fp, entries in b.items():
            merged.setdefault(fp, {}).update(entries)
    assert merged == strings
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 test_zedl10n.py 2>&1 | tail -8`
Expected: 报 `NameError: name 'group_batches' is not defined`

- [ ] **Step 3: 实现**

在 `src/zedl10n/batch.py` 末尾追加：

```python
# 目标批次规模（条目数）。按 52947 条总量估算约 265 个会话，
# 并发 10 时约 1 小时，落在 5h50m 预算内。
TARGET_BATCH_SIZE = 200

# 超过此条目数的文件单独成批：混装会让单个会话过长，
# 失败重试的代价也更大。
OVERSIZE_FILE_ENTRIES = 300


def group_batches(
    all_strings: dict[str, dict[str, str]],
    target_size: int = TARGET_BATCH_SIZE,
    oversize: int = OVERSIZE_FILE_ENTRIES,
) -> list[dict[str, dict[str, str]]]:
    """按文件累积合批，每批总量接近 target_size。

    超大文件（超过 oversize 条）单独成批，避免与小文件混装后
    单个会话过长——会话越长，失败重试浪费的 token 越多。
    """
    batches: list[dict[str, dict[str, str]]] = []
    current: dict[str, dict[str, str]] = {}
    current_size = 0

    for file_path, strings in all_strings.items():
        if not strings:
            continue
        size = len(strings)

        if size > oversize:
            # 先封掉手头这批，再让超大文件独占一批
            if current:
                batches.append(current)
                current, current_size = {}, 0
            batches.append({file_path: strings})
            continue

        if current_size + size > target_size and current:
            batches.append(current)
            current, current_size = {}, 0

        current[file_path] = strings
        current_size += size

    if current:
        batches.append(current)
    return batches
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 test_zedl10n.py 2>&1 | tail -6`
Expected: 全部通过（22/22）

- [ ] **Step 5: 提交**

```bash
git add src/zedl10n/batch.py test_zedl10n.py
git commit -m "feat(l10n): 批次分组改为按文件合批，为会话式翻译控制会话数"
```

---

### Task 2: MCP server（三个 tool）

独立、可单测的 MCP stdio server。tool 逻辑写成纯函数，便于直接调用测试，
不经协议层。

**Files:**
- Create: `src/zedl10n/mcp_server.py`
- Modify: `pyproject.toml`（加 `l10n` extra）
- Test: `test_zedl10n.py`

**Interfaces:**
- Consumes: `batch.py`（无直接依赖，但同属翻译链路）
- Produces:
  - `record_translation(out_dir: str, batch_id: str, original: str, translated: str) -> str`
    — 追加写 `<out_dir>/<batch_id>.jsonl`，返回确认文本
  - `record_untranslatable(dnt_dir: str, batch_id: str, original: str, reason: str) -> str`
    — 追加写 `<dnt_dir>/<batch_id>.jsonl`
  - `lookup_term(glossary_path: str, term: str) -> str` — 查表返回译法文本
  - `build_server() -> MCPServer` — 装配三个 tool 的 server 实例
  - 环境变量：`L10N_OUT_DIR`、`L10N_DNT_DIR`、`L10N_BATCH_ID`、`L10N_GLOSSARY`

- [ ] **Step 1: 加依赖**

修改 `pyproject.toml` 的 `[project.optional-dependencies]`：

```toml
[project.optional-dependencies]
excel = ["pandas>=2.0", "openpyxl>=3.1"]
ai = ["openai>=1.0,<3.8"]
# 会话式翻译：mcp 固定 <3，该 SDK v2 已发生破坏性改名
l10n = ["mcp>=2.2.0,<3"]
all = ["zedl10n[excel,ai,l10n]"]
```

- [ ] **Step 2: 写失败测试**

追加到 `test_zedl10n.py`：

```python
def test_mcp_record_translation_appends_jsonl(tmp_path) -> None:
    """每次提交追加一行 JSONL，不覆盖已有内容"""
    out = tmp_path / "out"
    from zedl10n.mcp_server import record_translation
    record_translation(str(out), "b1", "Save", "保存")
    record_translation(str(out), "b1", "Open", "打开")

    import json
    lines = (out / "b1.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2, lines
    assert json.loads(lines[0]) == {"original": "Save", "translated": "保存"}
    assert json.loads(lines[1]) == {"original": "Open", "translated": "打开"}


def test_mcp_record_translation_survives_interrupt(tmp_path) -> None:
    """会话中断后已写条目仍在：追加写入而非整文件重写"""
    out = tmp_path / "out"
    from zedl10n.mcp_server import record_translation
    record_translation(str(out), "b2", "A", "甲")
    # 模拟中断：进程消失，但文件内容已在磁盘
    content = (out / "b2.jsonl").read_text(encoding="utf-8")
    assert "甲" in content


def test_mcp_lookup_term_terms_and_keep_original(tmp_path) -> None:
    """terms 返回约定译法；keep_original 返回保持原文"""
    g = tmp_path / "glossary.yaml"
    g.write_text(
        "terms:\n  workspace: 工作区\n"
        "keep_original:\n  - Zed\n  - GitHub\n",
        encoding="utf-8",
    )
    from zedl10n.mcp_server import lookup_term
    assert "工作区" in lookup_term(str(g), "workspace")
    assert "保持原文" in lookup_term(str(g), "Zed")
    assert "未收录" in lookup_term(str(g), "nonexistent_term")


def test_mcp_lookup_term_missing_file_is_not_fatal(tmp_path) -> None:
    """词表缺失时返回未收录，而非抛异常中断翻译"""
    from zedl10n.mcp_server import lookup_term
    assert "未收录" in lookup_term(str(tmp_path / "nope.yaml"), "workspace")


def test_mcp_record_untranslatable(tmp_path) -> None:
    """标记禁翻写入独立文件，与译文分开"""
    dnt = tmp_path / "dnt"
    from zedl10n.mcp_server import record_untranslatable
    record_untranslatable(str(dnt), "b3", "tool_name", "identifier")

    import json
    rec = json.loads((dnt / "b3.jsonl").read_text(encoding="utf-8").strip())
    assert rec == {"original": "tool_name", "reason": "identifier"}


def test_mcp_server_exposes_three_tools() -> None:
    """server 必须暴露三个 tool，名称与 agent 白名单一致"""
    import asyncio
    from zedl10n.mcp_server import build_server
    # 用公共 list_tools，避免依赖 SDK 私有属性
    tools = asyncio.run(build_server().list_tools())
    names = {t.name for t in tools}
    assert names == {
        "glossary_lookup", "submit_translation", "mark_untranslatable",
    }, names
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python3 test_zedl10n.py 2>&1 | tail -8`
Expected: 报 `ModuleNotFoundError: No module named 'zedl10n.mcp_server'`

- [ ] **Step 4: 实现**

创建 `src/zedl10n/mcp_server.py`：

```python
"""翻译流水线的 MCP stdio server。

向 Claude Code 暴露三个 tool：查词表、提交译文、标记禁翻。
译文用「追加写 JSONL」落盘：会话中途被打断时，已完成条目仍在文件里，
不会像整文件重写那样丢掉整批成果。

工具逻辑与协议层分离：record_* / lookup_term 是纯函数，可直接单测；
build_server 只负责把它们挂到 MCP 协议上。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

log = logging.getLogger(__name__)


def _append_jsonl(dir_path: str, batch_id: str, record: dict) -> Path:
    """追加一行 JSONL，返回写入路径"""
    if not dir_path:
        raise RuntimeError("未配置输出目录（L10N_OUT_DIR / L10N_DNT_DIR）")
    target = Path(dir_path) / f"{batch_id}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    # 追加模式 + 单行写入：即使随后进程被杀死，已写条目也完整落盘
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return target


def record_translation(
    out_dir: str, batch_id: str, original: str, translated: str,
) -> str:
    """记录一条译文"""
    _append_jsonl(out_dir, batch_id, {"original": original, "translated": translated})
    return f"已记录: {original} => {translated}"


def record_untranslatable(
    dnt_dir: str, batch_id: str, original: str, reason: str,
) -> str:
    """记录一条无需翻译的条目"""
    _append_jsonl(dnt_dir, batch_id, {"original": original, "reason": reason})
    return f"已标记无需翻译: {original} ({reason})"


def lookup_term(glossary_path: str, term: str) -> str:
    """查术语表。文件缺失或结构异常时返回未收录，不抛异常。

    词表问题不该中断整批翻译——最坏结果是这条按常规语境翻译。
    """
    terms: dict[str, str] = {}
    keep: list[str] = []
    path = Path(glossary_path) if glossary_path else None
    if path and path.exists():
        try:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            terms = data.get("terms") or {}
            keep = data.get("keep_original") or []
        except Exception as e:
            log.warning("术语表解析失败，按空表处理: %s", e)

    if term in terms:
        return f"{term} => {terms[term]}"
    for k in keep:
        if str(k).lower() == term.lower():
            return f"{k} => {k}（专有名词，保持原文不译）"
    return f'术语表未收录 "{term}"，请按常规技术语境翻译。'


def build_server() -> MCPServer:
    """装配三个 tool 的 MCP server"""
    out_dir = os.environ.get("L10N_OUT_DIR", "")
    dnt_dir = os.environ.get("L10N_DNT_DIR", "")
    batch_id = os.environ.get("L10N_BATCH_ID", "batch")
    glossary = os.environ.get("L10N_GLOSSARY", "")

    server = MCPServer("l10n")

    @server.tool(
        description=(
            "查询术语表：给出英文术语，返回本项目约定的译法。"
            "遇到术语表内的技术名词时先查这里，保证全项目译法一致。"
        ),
    )
    def glossary_lookup(term: str) -> str:
        """查询术语表，返回约定译法"""
        return lookup_term(glossary, term)

    @server.tool(
        description=(
            "提交一条译文。**每翻译完一条就立即调用本工具**，"
            "不要在回复正文里输出译文。original 必须与原文完全一致。"
        ),
    )
    def submit_translation(original: str, translated: str) -> str:
        """提交一条译文，追加写入 JSONL"""
        return record_translation(out_dir, batch_id, original, translated)

    @server.tool(
        description=(
            "标记某条字符串无需翻译（纯代码标识符、变量名、key 名等）。"
            "注意：正常界面文案必须翻译，只有确定是标识符时才用本工具。"
        ),
    )
    def mark_untranslatable(original: str, reason: str) -> str:
        """标记一条无需翻译的条目"""
        return record_untranslatable(dnt_dir, batch_id, original, reason)

    return server


def main() -> None:
    """stdio 入口"""
    build_server().run("stdio")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 test_zedl10n.py 2>&1 | tail -6`
Expected: 全部通过（28/28）

- [ ] **Step 6: 协议层端到端验证**

用真实 MCP 握手确认三个 tool 都能通过协议调用（不只是直接调函数）：

```bash
cat > /tmp/mcp_e2e.py <<'PY'
import json, os, subprocess, sys
env = dict(os.environ, L10N_OUT_DIR="/tmp/mcp_e2e_out", L10N_BATCH_ID="e2e",
           L10N_GLOSSARY="config/glossary.yaml", L10N_DNT_DIR="/tmp/mcp_e2e_dnt")
srv = subprocess.Popen([sys.executable, "-m", "zedl10n.mcp_server"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    text=True, bufsize=1, env=env)
def send(o): srv.stdin.write(json.dumps(o)+"\n"); srv.stdin.flush()
def recv(): return json.loads(srv.stdout.readline())
send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{
    "protocolVersion":"2024-11-05","capabilities":{},
    "clientInfo":{"name":"t","version":"1"}}})
recv(); send({"jsonrpc":"2.0","method":"notifications/initialized"})
send({"jsonrpc":"2.0","id":2,"method":"tools/list"})
tools = {t["name"] for t in recv()["result"]["tools"]}
assert tools == {"glossary_lookup","submit_translation","mark_untranslatable"}, tools
send({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
    "name":"submit_translation","arguments":{"original":"Save","translated":"保存"}}})
assert not recv()["result"].get("isError")
send({"jsonrpc":"2.0","id":4,"method":"tools/call","params":{
    "name":"glossary_lookup","arguments":{"term":"workspace"}}})
assert "工作区" in recv()["result"]["content"][0]["text"]
srv.terminate()
print("协议层三 tool 验证通过")
PY
rm -rf /tmp/mcp_e2e_out /tmp/mcp_e2e_dnt
PYTHONPATH=src python3 /tmp/mcp_e2e.py && cat /tmp/mcp_e2e_out/e2e.jsonl
```

Expected: 打印"协议层三 tool 验证通过"，并输出
`{"original": "Save", "translated": "保存"}`

- [ ] **Step 7: 提交**

```bash
git add pyproject.toml src/zedl10n/mcp_server.py test_zedl10n.py
git commit -m "feat(l10n): 新增 MCP stdio server，暴露查词表/提交译文/标记禁翻三个 tool"
```

---

### Task 3: 会话驱动（起子进程、收 JSONL）

把"一个批次 → 一个 Claude Code 会话 → 收 JSONL"封装成可独立测试的模块。

**Files:**
- Create: `src/zedl10n/oc_translate.py`
- Modify: `.gitignore`（忽略会话临时产物）
- Test: `test_zedl10n.py`

**Interfaces:**
- Consumes: Task 1 的 `group_batches`；Task 2 的 `mcp_server`（作为子进程被 Claude Code 拉起）
- Produces:
  - `build_batch_prompt(file_path: str, strings: dict[str, str], lang: str) -> str`
    — 构建批次提示词，列出待翻译条目
  - `write_mcp_config(path: str, mcp_script: str) -> None` — 写 `mcp.json`
  - `collect_jsonl(path: str) -> dict[str, str]` — 读 JSONL，返回 `{原文: 译文}`；
    跳过坏行与空译文
  - `run_session(args: SessionArgs) -> SessionResult` — 起子进程跑一个批次
  - `SessionArgs` dataclass：`batch_id`、`prompt`、`model`、`out_dir`、`dnt_dir`、
    `glossary_path`、`mcp_config_path`、`timeout`、`claude_bin`

- [ ] **Step 1: 写失败测试**

追加到 `test_zedl10n.py`：

```python
def test_collect_jsonl_normal(tmp_path) -> None:
    """正常 JSONL 解析为 原文→译文 映射"""
    p = tmp_path / "b.jsonl"
    p.write_text(
        '{"original": "Save", "translated": "保存"}\n'
        '{"original": "Open", "translated": "打开"}\n',
        encoding="utf-8",
    )
    assert collect_jsonl(str(p)) == {"Save": "保存", "Open": "打开"}


def test_collect_jsonl_skips_bad_lines(tmp_path) -> None:
    """坏行（截断的 JSON）跳过，不影响其余条目"""
    p = tmp_path / "b.jsonl"
    p.write_text(
        '{"original": "Save", "translated": "保存"}\n'
        '{"original": "Open", "translat\n'
        '{"original": "Quit", "translated": "退出"}\n',
        encoding="utf-8",
    )
    assert collect_jsonl(str(p)) == {"Save": "保存", "Quit": "退出"}


def test_collect_jsonl_skips_empty_and_missing_fields(tmp_path) -> None:
    """空译文与字段缺失的条目跳过：它们等同未翻译，留给下次"""
    p = tmp_path / "b.jsonl"
    p.write_text(
        '{"original": "Save", "translated": ""}\n'
        '{"original": "Open"}\n'
        '{"translated": "无原文"}\n'
        '{"original": "Quit", "translated": "退出"}\n',
        encoding="utf-8",
    )
    assert collect_jsonl(str(p)) == {"Quit": "退出"}


def test_collect_jsonl_duplicate_last_wins(tmp_path) -> None:
    """同一条重复提交时取最后一次：agent 修正是正常行为"""
    p = tmp_path / "b.jsonl"
    p.write_text(
        '{"original": "Save", "translated": "保存"}\n'
        '{"original": "Save", "translated": "存储"}\n',
        encoding="utf-8",
    )
    assert collect_jsonl(str(p)) == {"Save": "存储"}


def test_collect_jsonl_missing_file(tmp_path) -> None:
    """文件不存在返回空映射：会话失败时即此情形"""
    assert collect_jsonl(str(tmp_path / "nope.jsonl")) == {}


def test_build_batch_prompt_lists_entries() -> None:
    """提示词必须逐条列出待翻译原文，并指明目标语言"""
    prompt = build_batch_prompt("a.rs", {"Save": "", "Open": ""}, "zh-CN")
    assert "Save" in prompt and "Open" in prompt
    assert "zh-CN" in prompt
    assert "submit_translation" in prompt


def test_write_mcp_config(tmp_path) -> None:
    """mcp.json 结构必须能被 Claude Code 识别"""
    import json
    p = tmp_path / "mcp.json"
    write_mcp_config(str(p), "/abs/path/zedl10n/mcp_server.py")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    entry = cfg["mcpServers"]["l10n"]
    assert entry["command"]
    assert "mcp_server.py" in " ".join(entry["args"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 test_zedl10n.py 2>&1 | tail -8`
Expected: 报 `NameError: name 'collect_jsonl' is not defined`

- [ ] **Step 3: 实现**

创建 `src/zedl10n/oc_translate.py`：

```python
"""Claude Code 会话驱动：把批次交给 agent，收 JSONL 回来。

每个批次起一个 `claude -p` 子进程，agent 通过 MCP tool 把译文逐条追加写入
JSONL。Python 侧只负责拼命令行、控并发、读回结果——译文不经过自由文本解析，
这是与旧实现最本质的区别。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# 单个会话的墙钟超时。批次 200 条 + 若干次读源码，正常远低于此值；
# 超时按会话失败处理，缺口留给下次增量。
SESSION_TIMEOUT = 300.0

# Claude Code 版本固定，升级需重跑 scripts/smoke_translate.py 验证
CLAUDE_BIN = os.environ.get("L10N_CLAUDE_BIN", "claude")


@dataclass
class SessionArgs:
    """一个会话的完整参数"""

    batch_id: str
    prompt: str
    model: str
    out_dir: str
    dnt_dir: str
    glossary_path: str
    mcp_config_path: str
    timeout: float = SESSION_TIMEOUT
    claude_bin: str = CLAUDE_BIN


@dataclass
class SessionResult:
    """一次会话的结果"""

    batch_id: str
    translations: dict[str, str] = field(default_factory=dict)
    untranslatable: list[dict] = field(default_factory=list)
    ok: bool = False
    detail: str = ""


def build_batch_prompt(
    file_path: str, strings: dict[str, str], lang: str,
) -> str:
    """构建批次提示词。

    只列出待翻条目，不内联源码——agent 需要语境时自己用 read 工具去读，
    这比 Python 预先截断源码塞进 prompt 更省 token 也更准确。
    """
    entries = "\n".join(f"- {s}" for s in strings)
    return (
        f"把下面 {len(strings)} 条 Zed 编辑器界面字符串翻译成 {lang}。\n"
        f"它们来自源文件 {file_path}，需要判断语境时用 read 工具读取该文件。\n\n"
        f"要求：\n"
        f"1. **每翻译完一条立即调用 submit_translation 提交**，不要在正文输出译文。\n"
        f"2. original 必须与下面列出的原文逐字符一致。\n"
        f"3. 占位符（{{}}、{{0}}、%s 等）必须原样保留，数量与顺序都不能变。\n"
        f"4. 技术术语先调 glossary_lookup 查表。\n"
        f"5. 纯代码标识符用 mark_untranslatable 标记，不要翻译。\n\n"
        f"待翻译条目：\n{entries}\n"
    )


def write_mcp_config(path: str, mcp_script: str) -> None:
    """写 Claude Code 的 mcp.json。

    用当前解释器绝对路径启动 server，避免依赖 PATH 里的 python3
    与服务端环境不一致。
    """
    cfg = {
        "mcpServers": {
            "l10n": {
                "command": sys.executable,
                "args": [mcp_script],
            },
        },
    }
    Path(path).write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def collect_jsonl(path: str) -> dict[str, str]:
    """读 JSONL 收集译文，返回 {原文: 译文}。

    容错策略：坏行、空译文、字段缺失一律跳过——这些都等同"未翻译"，
    会在下次增量运行时作为缺口自动重翻。同一条重复提交取最后一次，
    因为 agent 修正译文时会重复提交，后者才是它想要的。
    """
    result: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return result
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        original = rec.get("original")
        translated = rec.get("translated")
        if not isinstance(original, str) or not original:
            continue
        if not isinstance(translated, str) or not translated.strip():
            continue
        result[original] = translated
    return result


def collect_untranslatable(path: str) -> list[dict]:
    """读禁翻标记 JSONL"""
    records: list[dict] = []
    p = Path(path)
    if not p.exists():
        return records
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec.get("original"), str) and rec["original"]:
            records.append(rec)
    return records


async def run_session(args: SessionArgs) -> SessionResult:
    """起一个 Claude Code 会话跑批次，返回收集到的译文。

    失败（非零退出 / 超时 / 空结果）不抛异常，返回 ok=False，
    由调用方决定如何记录缺口。
    """
    out_path = Path(args.out_dir) / f"{args.batch_id}.jsonl"
    dnt_path = Path(args.dnt_dir) / f"{args.batch_id}.jsonl"
    # 会话重跑时先清掉残留，避免旧结果混入
    for p in (out_path, dnt_path):
        p.unlink(missing_ok=True)

    cmd = [
        args.claude_bin, "-p", args.prompt,
        "--agent", "translator",
        "--model", args.model,
        "--output-format", "json",
        # 自动化场景无人审批；容器内运行，且 agent 工具白名单已限定
        "--permission-mode", "bypassPermissions",
        "--mcp-config", args.mcp_config_path,
        "--strict-mcp-config",
        # 只加载项目级配置，避免用户级 ~/.claude 影响结果可复现性
        "--setting-sources", "project",
    ]

    env = dict(os.environ)
    env.update({
        "L10N_OUT_DIR": args.out_dir,
        "L10N_DNT_DIR": args.dnt_dir,
        "L10N_BATCH_ID": args.batch_id,
        "L10N_GLOSSARY": args.glossary_path,
    })

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=args.timeout,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return SessionResult(
                args.batch_id, ok=False,
                detail=f"会话超时（{args.timeout:.0f}s）",
            )
    except Exception as e:
        return SessionResult(args.batch_id, ok=False, detail=f"启动失败: {e}")

    translations = collect_jsonl(str(out_path))
    untranslatable = collect_untranslatable(str(dnt_path))

    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace")[-500:]
        return SessionResult(
            args.batch_id, translations, untranslatable, ok=False,
            detail=f"退出码 {proc.returncode}: {tail}",
        )
    if not translations and not untranslatable:
        return SessionResult(
            args.batch_id, ok=False, detail="会话未产出任何结果",
        )
    return SessionResult(args.batch_id, translations, untranslatable, ok=True)
```

- [ ] **Step 4: 跑测试确认通过**

先补 import。在 `test_zedl10n.py` 的 import 区加入：

```python
sys.path.insert(0, str(Path(__file__).parent / "src"))

from zedl10n.oc_translate import (  # noqa: E402
    build_batch_prompt, collect_jsonl, write_mcp_config,
)
```

Run: `python3 test_zedl10n.py 2>&1 | tail -6`
Expected: 全部通过（35/35）

- [ ] **Step 5: 补 .gitignore**

会话产物（`i18n/.session/`）与冒烟产物（`i18n/.smoke/`）是运行时临时文件，
不应进仓库。在 `.gitignore` 末尾追加：

```
# 翻译会话临时产物（JSONL / mcp.json）
i18n/.session/
i18n/.smoke/
```

- [ ] **Step 6: 提交**

```bash
git add src/zedl10n/oc_translate.py test_zedl10n.py .gitignore
git commit -m "feat(l10n): 新增 Claude Code 会话驱动，批次交 agent 并收 JSONL"
```

---

### Task 4: 编排入口改造（translate.py）

把 `_translate_async` 从"并发调 API"改为"并发跑会话"，删除三级降级与旧调用路径。

**Files:**
- Modify: `src/zedl10n/translate.py`
- Modify: `src/zedl10n/untranslatable.py`（加 `DNT_PATH` 常量）
- Test: `test_zedl10n.py`

**Interfaces:**
- Consumes: Task 1 `group_batches`；Task 3 `SessionArgs` / `run_session` / `build_batch_prompt` /
  `write_mcp_config` / `collect_jsonl`
- Produces:
  - `_translate_async(all_strings, existing, mode, lang, glossary_path, ai_cfg, source_root="", skip_sets=None, time_limit=None) -> tuple[TranslationDict, bool]`
    — 签名与现状一致，内部改为会话驱动；返回 `(结果, 是否全部完成)`
  - `translate_all(...) -> bool` — 签名不变，新增 `mcp_config_path` 与 `claude_bin` 可选参数
  - `run(args)` — 保持退出码 75 语义

**关键设计决定：**
- 删除 `_call_ai`、`_fetch_translation` 与三级降级链，不做任何 API 兜底
- 占位符校验保留，但**不再调用 AI 修正**：校验失败的条目直接置空，
  留给下次增量重翻。理由是无降级通道后，再开一个会话修占位符等于又引入一层
  不可控；空值重翻是已有机制，代价可控
- `_ai_fix_consistency`（需要调 AI）删除；保留 `consistency.py` 里的
  **规则版** `fix_consistency`，它不依赖模型
- `mark_untranslatable` 的产出合并进 `do_not_translate.json` 禁翻清单

- [ ] **Step 1: 删除与本次改动冲突的旧测试**

`test_zedl10n.py` 中的 `test_translate_time_limit_keeps_finished_batches`
（约 200 行起）patch 了 `tr._translate_batch`，而该函数在本任务中被删除。
先整段删除它——它的覆盖范围由本任务新增的三个测试接替。

- [ ] **Step 2: 写失败测试**

追加到 `test_zedl10n.py`：

```python
def test_translate_async_uses_sessions_and_validates_placeholders() -> None:
    """会话产出的译文经占位符校验；不合格者置空留待重翻"""
    import asyncio
    from zedl10n.utils import AIConfig
    from zedl10n import translate as tr

    async def fake_run_session(args):
        from zedl10n.oc_translate import SessionResult
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        # 第一条占位符正确，第二条占位符被吃掉
        (out / f"{args.batch_id}.jsonl").write_text(
            '{"original": "Open {}", "translated": "打开 {}"}\n'
            '{"original": "Save {}", "translated": "保存"}\n',
            encoding="utf-8",
        )
        return SessionResult(
            args.batch_id,
            {"Open {}": "打开 {}", "Save {}": "保存"},
            ok=True,
        )

    orig = tr.run_session
    tr.run_session = fake_run_session
    try:
        result, complete = asyncio.run(tr._translate_async(
            {"a.rs": {"Open {}": "", "Save {}": ""}},
            {},
            "incremental", "zh-CN",
            "config/glossary.yaml",
            AIConfig(base_url="", api_key="", model="m", concurrency=2),
            time_limit=None,
        ))
    finally:
        tr.run_session = orig

    assert complete is True
    assert result["a.rs"]["Open {}"] == "打开 {}", result
    # 占位符丢失的那条被置空，下次增量会重翻
    assert result["a.rs"]["Save {}"] == "", result


def test_translate_async_marks_failed_batches_as_gaps() -> None:
    """会话失败时该批条目保持空值，不污染已有译文"""
    import asyncio
    from zedl10n.utils import AIConfig
    from zedl10n import translate as tr

    async def failing_session(args):
        from zedl10n.oc_translate import SessionResult
        return SessionResult(args.batch_id, ok=False, detail="模拟失败")

    orig = tr.run_session
    tr.run_session = failing_session
    try:
        result, complete = asyncio.run(tr._translate_async(
            {"a.rs": {"Save": ""}},
            {"a.rs": {"Old": "旧"}},
            "incremental", "zh-CN",
            "config/glossary.yaml",
            AIConfig(base_url="", api_key="", model="m", concurrency=2),
            time_limit=None,
        ))
    finally:
        tr.run_session = orig

    # 失败批次的条目保持缺失（等同空值），下次增量会作为缺口重翻
    assert result["a.rs"].get("Save", "") == ""
    assert result["a.rs"]["Old"] == "旧", "已有译文不得被失败批次清掉"


def test_translate_async_time_limit_keeps_finished_batches() -> None:
    """到时限：已完成批次保留、未完成取消，返回未完成标记"""
    import asyncio
    from zedl10n.utils import AIConfig
    from zedl10n import translate as tr

    async def slow_session(args):
        from zedl10n.oc_translate import SessionResult
        if "slow" in args.prompt:
            await asyncio.sleep(60)
            return SessionResult(args.batch_id, ok=True)
        return SessionResult(args.batch_id, {"Save": "保存"}, ok=True)

    orig = tr.run_session
    tr.run_session = slow_session
    try:
        result, complete = asyncio.run(tr._translate_async(
            {"slow.rs": {"Save": ""}, "fast.rs": {"Open": ""}},
            {},
            "incremental", "zh-CN",
            "config/glossary.yaml",
            AIConfig(base_url="", api_key="", model="m", concurrency=2),
            time_limit=0.0,
        ))
    finally:
        tr.run_session = orig

    assert complete is False
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python3 test_zedl10n.py 2>&1 | tail -8`
Expected: 报 `AttributeError: module 'zedl10n.translate' has no attribute 'run_session'`

- [ ] **Step 4: 改写 translate.py**

删除 `_call_ai`、`_fetch_translation`，把 `_translate_batch` 与 `_translate_async`
替换为下列实现；`_read_source_file` 与 `_ai_fix_consistency` 一并删除
（前者已由 agent 的 read 工具替代，后者依赖已删的 `_call_ai`）。

顶部的 import 改为：

```python
import argparse
import asyncio
import sys
from pathlib import Path

from .batch import group_batches
from .oc_translate import (
    SessionArgs,
    build_batch_prompt,
    run_session,
    write_mcp_config,
)
from .prompts import validate_placeholders
from .utils import (
    AIConfig,
    ProgressBar,
    TranslationDict,
    load_json,
    normalize_fullwidth,
    save_json,
)
```

替换 `_translate_async` 为：

```python
async def _translate_async(
    all_strings: TranslationDict,
    existing: TranslationDict,
    mode: str,
    lang: str,
    glossary_path: str,
    ai_cfg: AIConfig,
    source_root: str = "",
    skip_sets: tuple[set[tuple[str, str]], set[str]] | None = None,
    time_limit: float | None = None,
) -> tuple[TranslationDict, bool]:
    """并发跑 Claude Code 会话翻译，返回 (结果, 是否全部完成)"""
    result: TranslationDict = {fp: dict(v) for fp, v in existing.items()}
    skip_covered, skip_global = skip_sets or (set(), set())

    # 只把「需要翻」的条目交给会话：已有译文与禁翻条目不进批次
    pending: TranslationDict = {}
    skipped = 0
    for file_path, strings in all_strings.items():
        todo: dict[str, str] = {}
        for s in strings:
            if s in skip_global or (file_path, s) in skip_covered:
                skipped += 1
                continue
            if mode == "full" or not str(
                result.get(file_path, {}).get(s, "")
            ).strip():
                todo[s] = ""
        if todo:
            pending[file_path] = todo

    if skipped:
        log.info("按禁翻清单跳过 %d 条", skipped)

    batches = group_batches(pending)
    run_dir = Path("i18n/.session")
    out_dir = run_dir / "out"
    dnt_dir = run_dir / "dnt"
    out_dir.mkdir(parents=True, exist_ok=True)
    dnt_dir.mkdir(parents=True, exist_ok=True)

    # mcp.json 指向仓库内的 server 脚本，用绝对路径避免 cwd 依赖
    mcp_script = str(
        Path(__file__).resolve().parent / "mcp_server.py"
    )
    mcp_config = str(run_dir / "mcp.json")
    write_mcp_config(mcp_config, mcp_script)

    semaphore = asyncio.Semaphore(ai_cfg.concurrency)
    untranslatable: list[dict] = []

    async def do_batch(idx: int, batch: dict) -> None:
        batch_id = f"b{idx:04d}"
        # 一个批次可含多个文件，提示词里逐个文件分段列出
        prompt_parts = [
            build_batch_prompt(fp, entries, lang)
            for fp, entries in batch.items()
        ]
        args = SessionArgs(
            batch_id=batch_id,
            prompt="\n\n".join(prompt_parts),
            model=ai_cfg.model,
            out_dir=str(out_dir),
            dnt_dir=str(dnt_dir),
            glossary_path=glossary_path,
            mcp_config_path=mcp_config,
        )
        async with semaphore:
            res = await run_session(args)
        if not res.ok:
            log.warning("批次 %s 失败: %s", batch_id, res.detail)
            return
        untranslatable.extend(res.untranslatable)
        for fp, entries in batch.items():
            for original in entries:
                translated = res.translations.get(original, "")
                result.setdefault(fp, {})[original] = translated

    total = len(batches)
    log.info("共 %d 个翻译会话，并发数 %d", total, ai_cfg.concurrency)
    if total == 0:
        return result, True

    pbar = ProgressBar(total, desc="翻译")
    tasks = [
        asyncio.create_task(do_batch(i, b)) for i, b in enumerate(batches)
    ]
    complete = True
    try:
        for coro in asyncio.as_completed(tasks, timeout=time_limit):
            await coro
            pbar.update()
    except TimeoutError:
        complete = False
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    pbar.finish()

    # 占位符校验：不合格条目置空留待下次重翻（无降级通道，不做 AI 修正）
    for fp in result:
        errors = validate_placeholders(result[fp])
        for original, (src_ph, dst_ph) in errors.items():
            log.warning(
                "占位符校验失败，丢弃译文: %r (原文=%s, 译文=%s) [%s]",
                original, src_ph, dst_ph, fp,
            )
            result[fp][original] = ""

    if untranslatable:
        _merge_untranslatable(untranslatable)

    if not complete:
        log.warning(
            "达到翻译时限 %.0fs，未完成的批次留待下次继续", time_limit,
        )
    return result, complete


def _merge_untranslatable(records: list[dict]) -> None:
    """把 agent 标记的禁翻条目并入 do_not_translate.json。

    合并而非覆盖：清单由 05 流程维护，这里只做增量补充。
    清单结构为 {global_entries: [{original, reason, ...}], entries: [...]}，
    全局条目写 global_entries，与 05 流程产出的形状保持一致。
    """
    from .untranslatable import _load_registry, DNT_PATH

    data, _, covered_global = _load_registry(DNT_PATH)
    for rec in records:
        original = rec.get("original")
        if not original or original in covered_global:
            continue
        data["global_entries"].append({
            "original": original,
            "reason": f"agent: {rec.get('reason', 'unknown')}",
        })
        covered_global.add(original)
    save_json(data, DNT_PATH)
```

**注意**：上面的 `DNT_PATH` 是新增常量。在 `src/zedl10n/untranslatable.py`
顶部（`log = logging.getLogger(__name__)` 之后）加入：

```python
# 禁翻清单默认路径。05 流程与翻译流程共用，集中在此便于对齐。
DNT_PATH = "config/do_not_translate.json"
```

同时把 `mark_untranslatable` 与 `run` 中原先硬编码的
`"config/do_not_translate.json"` 默认值改用该常量。

`_merge_untranslatable` 里对 `_load_registry` 的调用需要 `data["global_entries"]`
存在——`_load_registry` 已用 `setdefault` 保证该键存在，无需额外处理。
```

`translate_all` 的签名与末尾保存逻辑保持不变（含 `return complete`），
仅把 `asyncio.run(_translate_async(...))` 的返回值解包为
`result, complete`（现状已是如此，无需改动）。

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 test_zedl10n.py 2>&1 | tail -6`
Expected: 全部通过（37/37）

- [ ] **Step 6: 静态检查无残留引用**

Run: `grep -n "_call_ai\|_fetch_translation\|_ai_fix_consistency\|XML_FALLBACK" src/zedl10n/*.py`

Expected: 在 `translate.py` 中无输出（`prompts.py` 中的定义在 Task 7 处理）

- [ ] **Step 7: 提交**

```bash
git add src/zedl10n/translate.py src/zedl10n/untranslatable.py test_zedl10n.py
git commit -m "refactor(l10n): 翻译编排改为 Claude Code 会话驱动，删除三级降级链"
```

---

### Task 5: 翻译 agent 定义与 CI 接线

新增 agent 定义，并把 01 工作流从"装 openai 依赖"改为"装 Claude Code + 设环境变量"。

**Files:**
- Create: `.claude/agents/translator.md`
- Modify: `.github/workflows/01-translate.yml`

**Interfaces:**
- Consumes: Task 3 `SessionArgs.claude_bin`、`.claude/agents/translator.md` 被 `--agent translator` 引用
- Produces: 无（CI 接线为终端环节）

**关键设计决定：**
- 环境变量在**工作流 step 级**设置，不用 job 级 `env:`——避免泄漏到构建步骤
- 九个模型覆盖变量一个都不能少。漏掉 `ANTHROPIC_SMALL_FAST_MODEL` 或
  `CLAUDE_CODE_SUBAGENT_MODEL` 会导致后台任务/子 agent 请求未指定模型
- Claude Code 用 npm 固定版本安装，禁用 `@latest`

- [ ] **Step 1: 创建翻译 agent 定义**

创建 `.claude/agents/translator.md`：

```markdown
---
name: translator
description: zed-globalization 翻译执行器：把批次里的英文界面字符串译成目标语言，逐条通过 MCP tool 提交
tools: Read, mcp__l10n__glossary_lookup, mcp__l10n__submit_translation, mcp__l10n__mark_untranslatable
---

你是 Zed 编辑器汉化项目的翻译执行器。任务是把指派批次里的英文界面字符串翻译成目标语言，
并**通过 MCP tool 逐条提交**。

## 硬性规则

1. **必须用 `submit_translation` 提交每条译文。** 不要在回复正文输出译文，
   也不要输出 JSON 块——译文只通过 tool 落盘。
2. **`original` 必须与待翻译列表里的原文逐字符一致**，包括标点与首尾空格。
   任何差异都会导致该条译文被丢弃。
3. 每条翻译完立即提交，不要攒到最后。会话可能中途结束，已提交的保留，
   未提交的留到下次。
4. 纯代码标识符（变量名、key 名、枚举值、类型名）用 `mark_untranslatable` 标记，
   **不要**翻译。正常界面文案、菜单项、提示语、按钮文字**必须**翻译。
5. 技术术语先调 `glossary_lookup`；表内有约定译法时必须采用。
6. 需要判断语境时用 `Read` 读原始 `.rs` 文件，看字符串出现在什么位置。
   不要凭空猜测——同一个词在不同语境下译法可能不同。

## 翻译要求

- **占位符必须原样保留**：`{}`、`{0}`、`%s`、`%1$s`、`{name}` 等，
  数量与顺序都不能变。改动占位符会导致该条被校验丢弃。
- 转义序列原样保留：`\n`、`\t`、`\\`、`\"`。
- 术语统一：同类概念全批用同一译法。
- 简洁：界面文案避免比原文长很多，防止界面溢出。
- 中文标点按中文习惯（，。！？），代码片段保持半角。
- 不添加原文没有的内容，不"优化"原文表达。
```

- [ ] **Step 2: 改 01 工作流的依赖安装**

把 `.github/workflows/01-translate.yml` 中 `translate` job 的安装步骤：

```yaml
      - name: 安装依赖
        run: pip install ".[ai]"
```

改为：

```yaml
      - name: 安装依赖
        run: pip install ".[l10n]"

      - name: 安装 Claude Code（版本固定，升级前需重跑冒烟脚本）
        run: npm install -g @anthropic-ai/claude-code@2.1.280

      - name: 校验 Claude Code 版本
        run: |
          INSTALLED=$(claude --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
          echo "已安装 Claude Code: $INSTALLED"
          if [ "$INSTALLED" != "2.1.280" ]; then
            echo "::error::Claude Code 版本不符，期望 2.1.280，实际 $INSTALLED"
            exit 1
          fi
```

- [ ] **Step 3: 给 AI 翻译步骤设置模型覆盖环境变量**

把该 job 中 `AI 翻译（时限：任务开始后 5h50m）` 步骤的 `env:` 段整体替换为：

```yaml
        env:
          # ── AI 服务接入 ──────────────────────────────────────────
          AI_BASE_URL: ${{ secrets.AI_BASE_URL }}
          AI_API_KEY: ${{ secrets.AI_API_KEY }}
          AI_MODEL: ${{ secrets.AI_MODEL }}
          AI_CONCURRENCY: ${{ vars.AI_CONCURRENCY || '10' }}
          VERSION: ${{ steps.params.outputs.VERSION }}
          TARGET_LANG: ${{ steps.params.outputs.TARGET_LANG }}
          EXTRACT_MODE: ${{ steps.params.outputs.EXTRACT_MODE }}
          # ── Claude Code 接入 ─────────────────────────────────────
          # baseURL 需要带协议头；provider 的 Anthropic 端点无 /v1 后缀
          ANTHROPIC_BASE_URL: ${{ secrets.AI_BASE_URL }}
          ANTHROPIC_API_KEY: ${{ secrets.AI_API_KEY }}
          # 九个变量强制覆盖全部模型槽位，避免任何内置模型兜底。
          # SMALL_FAST 与 SUBAGENT 尤其关键：不覆盖会偷偷请求未指定模型。
          ANTHROPIC_MODEL: ${{ secrets.AI_MODEL }}
          ANTHROPIC_DEFAULT_MODEL: ${{ secrets.AI_MODEL }}
          ANTHROPIC_DEFAULT_HAIKU_MODEL: ${{ secrets.AI_MODEL }}
          ANTHROPIC_DEFAULT_SONNET_MODEL: ${{ secrets.AI_MODEL }}
          ANTHROPIC_DEFAULT_OPUS_MODEL: ${{ secrets.AI_MODEL }}
          ANTHROPIC_DEFAULT_FABLE_MODEL: ${{ secrets.AI_MODEL }}
          ANTHROPIC_SMALL_FAST_MODEL: ${{ secrets.AI_MODEL }}
          CLAUDE_CODE_SUBAGENT_MODEL: ${{ secrets.AI_MODEL }}
          CLAUDE_CODE_NO_MODEL_FALLBACK: "1"
```

- [ ] **Step 4: 让翻译步骤不再依赖 base_url 里的 /v1 后缀**

`ANTHROPIC_BASE_URL` 不能带 `/v1`（Claude Code 自行拼接 `/v1/messages`），
而 OpenAI 兼容路径需要 `/v1`。在 `AI 翻译` 步骤的 `run:` 开头加一段归一化：

```yaml
        run: |
          BUDGET=$(( JOB_START + 21000 - $(date +%s) ))
          echo "翻译剩余时限: ${BUDGET}s"

          # AI_BASE_URL 面向 OpenAI 兼容协议（含 /v1），Claude Code 需要裸域名
          export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL%/v1}"
          echo "Anthropic base: $ANTHROPIC_BASE_URL"

          set +e
          zedl10n translate \
```

- [ ] **Step 5: 本地校验工作流语法**

Run:
```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/01-translate.yml')); print('YAML 合法')"
```

Expected: 打印 `YAML 合法`

- [ ] **Step 6: 安装 Claude Code 2.1.280 并验证 agent 可被识别**

Run:
```bash
npm install -g @anthropic-ai/claude-code@2.1.280 2>&1 | tail -3
claude --version
```

Expected: 打印 `2.1.280 (Claude Code)`

- [ ] **Step 7: 提交**

```bash
git add .claude/agents/translator.md .github/workflows/01-translate.yml
git commit -m "feat(ci): 01 工作流接入 Claude Code，固定 2.1.280 并强制模型覆盖"
```

---

### Task 6: 清理死代码

删除已无调用方的解析函数与降级指令。放在翻译链路改造之后做，
确保删除时已无任何引用。

**Files:**
- Modify: `src/zedl10n/utils.py`
- Modify: `src/zedl10n/prompts.py`
- Modify: `test_zedl10n.py`

**Interfaces:**
- Consumes: Task 4（已删除调用方）
- Produces: 无

**注意**：只删确认无引用的符号。`parse_json_response` 仍被
`fix_placeholders.py` 使用，**保留**。

- [ ] **Step 1: 确认待删符号无引用**

Run:
```bash
grep -rn "parse_xml_response\|parse_numbered_response\|XML_FALLBACK_INSTRUCTION\|build_numbered_instruction" src/ scripts/ test_zedl10n.py
```

Expected: 仅出现在 `utils.py` / `prompts.py` 的**定义处**，
以及 `test_zedl10n.py` 中引用它们的测试。若 `src/zedl10n/translate.py`
仍有引用，说明 Task 4 未完成，先回去补齐。

- [ ] **Step 2: 删除测试**

`test_zedl10n.py` 中删除针对上述函数的测试（如
`test_bad_response_no_longer_crashes_validation` 若仅覆盖 XML 解析路径则一并删除；
若覆盖 `parse_json_response` 则保留）。

Run: `python3 test_zedl10n.py 2>&1 | tail -6`
Expected: 全部通过（数量视删除情况，应无 FAIL）

- [ ] **Step 3: 删除 utils.py 中的解析函数**

删除 `src/zedl10n/utils.py` 中的 `parse_xml_response` 与
`parse_numbered_response` 两个函数及其文档字符串。

- [ ] **Step 4: 删除 prompts.py 中的降级指令**

删除 `src/zedl10n/prompts.py` 中的 `XML_FALLBACK_INSTRUCTION` 常量与
`build_numbered_instruction` 函数。

- [ ] **Step 5: 验证无残留引用**

Run:
```bash
grep -rn "parse_xml_response\|parse_numbered_response\|XML_FALLBACK_INSTRUCTION\|build_numbered_instruction" src/ scripts/ test_zedl10n.py || echo "无残留引用 ✅"
python3 -c "import sys; sys.path.insert(0,'src'); import zedl10n.translate, zedl10n.cli, zedl10n.fix_placeholders; print('模块导入正常')"
```

Expected: 打印 `无残留引用 ✅` 与 `模块导入正常`

- [ ] **Step 6: 跑全量测试**

Run: `python3 test_zedl10n.py 2>&1 | tail -6`
Expected: 无 FAIL

- [ ] **Step 7: 提交**

```bash
git add src/zedl10n/utils.py src/zedl10n/prompts.py test_zedl10n.py
git commit -m "chore(l10n): 删除 XML/编号降级解析与其指令，翻译不再解析自由文本"
```

---

### Task 7: 冒烟脚本（不进 CI）

手工验证脚本：取少量真实批次跑通，观察质量、耗时与 tool 调用可靠性。
用于升级 Claude Code 或更换模型后的回归验证。

**Files:**
- Create: `scripts/smoke_translate.py`

**Interfaces:**
- Consumes: Task 1 `group_batches`；Task 3 `run_session` / `SessionArgs` /
  `write_mcp_config` / `build_batch_prompt`
- Produces: 无（手工工具）

- [ ] **Step 1: 写脚本**

创建 `scripts/smoke_translate.py`：

```python
#!/usr/bin/env python3
"""翻译链路冒烟脚本（手工运行，不进 CI）。

取真实输入的少量批次跑一遍，报告耗时、成功率与 tool 调用情况。
用于升级 Claude Code 版本或更换模型后的回归验证。

用法：
    AI_API_KEY=... AI_MODEL=... python3 scripts/smoke_translate.py [批次数]

前置：string.json 已由提取步骤产出。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from zedl10n.batch import group_batches  # noqa: E402
from zedl10n.oc_translate import (  # noqa: E402
    SessionArgs, build_batch_prompt, run_session, write_mcp_config,
)
from zedl10n.prompts import validate_placeholders  # noqa: E402


async def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    strings_path = Path("string.json")
    if not strings_path.exists():
        print("缺少 string.json，请先运行提取步骤", file=sys.stderr)
        return 1

    all_strings = json.loads(strings_path.read_text(encoding="utf-8"))
    batches = group_batches(all_strings)[:limit]
    print(f"取 {len(batches)} 个批次冒烟，共 "
          f"{sum(len(e) for b in batches for e in b.values())} 条")

    run_dir = Path("i18n/.smoke")
    (run_dir / "out").mkdir(parents=True, exist_ok=True)
    (run_dir / "dnt").mkdir(parents=True, exist_ok=True)
    mcp_config = str(run_dir / "mcp.json")
    write_mcp_config(
        mcp_config,
        str(Path(__file__).resolve().parent.parent
            / "src" / "zedl10n" / "mcp_server.py"),
    )

    model = os.environ.get("AI_MODEL", "")
    if not model:
        print("未设置 AI_MODEL", file=sys.stderr)
        return 1

    total_expected = 0
    total_got = 0
    failures = 0
    start = time.time()

    for i, batch in enumerate(batches):
        bid = f"s{i:02d}"
        expected = sum(len(e) for e in batch.values())
        total_expected += expected
        prompt = "\n\n".join(
            build_batch_prompt(fp, entries, "zh-CN")
            for fp, entries in batch.items()
        )
        t0 = time.time()
        res = await run_session(SessionArgs(
            batch_id=bid, prompt=prompt, model=model,
            out_dir=str(run_dir / "out"), dnt_dir=str(run_dir / "dnt"),
            glossary_path="config/glossary.yaml",
            mcp_config_path=mcp_config,
        ))
        elapsed = time.time() - t0
        got = len(res.translations)
        total_got += got
        if not res.ok:
            failures += 1

        # 占位符校验：冒烟阶段就暴露格式问题
        bad = validate_placeholders(res.translations)
        status = "OK " if res.ok else "FAIL"
        print(f"  [{status}] {bid}: {got}/{expected} 条, "
              f"{elapsed:.0f}s, 占位符问题 {len(bad)} 条")
        if not res.ok:
            print(f"        详情: {res.detail[:200]}")

    total_elapsed = time.time() - start
    print(f"\n汇总: {total_got}/{total_expected} 条, "
          f"失败会话 {failures}/{len(batches)}, "
          f"总耗时 {total_elapsed:.0f}s "
          f"(均值 {total_elapsed / max(len(batches), 1):.0f}s/会话)")
    # 按均值外推全量成本，供调整并发与批次粒度参考
    if batches:
        all_batches = len(group_batches(all_strings))
        avg = total_elapsed / len(batches)
        for conc in (5, 10, 20):
            print(f"  全量 {all_batches} 会话、并发 {conc} 预计: "
                  f"{all_batches * avg / conc / 60:.0f} 分钟")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: 用真实凭据跑一次**

Run:
```bash
AI_BASE_URL="<provider>/v1" AI_API_KEY="<key>" AI_MODEL="<model>" \
  ANTHROPIC_BASE_URL="<provider>" ANTHROPIC_API_KEY="<key>" \
  python3 scripts/smoke_translate.py 3
```

Expected: 打印每批的 `[OK]` 与条数、耗时，最后给出汇总与全量外推。
若有 `[FAIL]`，看详情排查（常见原因：可用额度、模型不支持 tool 调用）。

- [ ] **Step 3: 记录实测数据到 spec**

把实测的均值耗时与会话成功率补进
`docs/superpowers/specs/2026-11-06-claude-code-translation-design.md`
的"会话预算"一节，替换掉当前基于单次的估算。

- [ ] **Step 4: 提交**

```bash
git add scripts/smoke_translate.py docs/superpowers/specs/2026-11-06-claude-code-translation-design.md
git commit -m "test(l10n): 新增翻译链路冒烟脚本，实测数据回填设计文档"
```

---

## 后续（不在本计划范围）

- `02-build.yml` 及各构建 job 未涉及翻译链路，本次不改。
- 05 工作流（标记无需翻译）仍用旧的 OpenAI 兼容调用，本次不改；
  若后续要统一，需另开计划。
