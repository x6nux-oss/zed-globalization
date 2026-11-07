#!/usr/bin/env python3
"""替换保护规则 + AI 响应解析的回归测试

直接运行: python3 test_zedl10n.py
"""

import asyncio
import logging
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from zedl10n import translate as tr  # noqa: E402
from zedl10n.batch import group_batches  # noqa: E402
from zedl10n.oc_translate import (  # noqa: E402
    build_batch_prompt, collect_jsonl, write_mcp_config,
)
from zedl10n.prompts import validate_placeholders  # noqa: E402
from zedl10n.replace import (  # noqa: E402
    _escape_for_rust_source, _find_protected_ranges, _replace_skip_protected,
)
from zedl10n.untranslatable import (  # noqa: E402
    collect_candidates, load_skip_sets, rule_verdict,
)
from zedl10n.utils import AIConfig, parse_json_response  # noqa: E402


def translate(content: str, original: str, translated: str) -> str:
    """模拟 replace_in_source 对单条翻译的处理"""
    protected = _find_protected_ranges(content)
    safe = _escape_for_rust_source(translated)
    new_content, _ = _replace_skip_protected(
        content, f'"{original}"', f'"{safe}"', protected,
    )
    return new_content


def test_raw_string_regex_untouched() -> None:
    """issue #65: 原始字符串里的正则不能被转义写回破坏

    r"fn (.+?)\\(" 按普通字符串写回会变成 r"fn (.+?)\\\\("，
    正则引擎把 \\\\ 当作转义反斜杠，后面的 ( 成为未闭合分组 → 编译 panic。
    """
    src = 'static RE: LazyLock<Regex> =\n' \
          '    LazyLock::new(|| Regex::new(r"fn (.+?)\\(").expect("失败"));'
    out = translate(src, r'fn (.+?)\(', r'fn (.+?)\(')
    assert r'r"fn (.+?)\("' in out, out
    assert r'\\(' not in out, "原始字符串里的反斜杠被加倍了"


def test_raw_hash_string_untouched() -> None:
    """r#"..."# 形式同样受保护"""
    src = 'let re = Regex::new(r#"\\d+\\s*"#).unwrap();'
    out = translate(src, r'\d+\s*', '数字')
    assert r'r#"\d+\s*"#' in out, out


def test_key_context_untouched() -> None:
    """issue #66: key context 是代码标识符，译成中文后 keymap 匹配不上"""
    src = 'dispatch_context.add("Terminal");\n' \
          'key_context.set("mode", "full");'
    out = translate(src, "Terminal", "终端")
    out = translate(out, "full", "完整")
    assert 'dispatch_context.add("Terminal")' in out, out
    assert 'key_context.set("mode", "full")' in out, out


def test_key_context_parse_untouched() -> None:
    """KeyContext::parse("editor mode=full") 同样是标识符表达式"""
    src = 'let ctx = KeyContext::parse("editor mode=full").unwrap();'
    out = translate(src, "editor mode=full", "编辑器 模式=完整")
    assert 'KeyContext::parse("editor mode=full")' in out, out


def test_serialized_item_kind_untouched() -> None:
    """issue #66: 序列化标识符被翻译会破坏工作区持久化"""
    src = 'impl SerializableItem for TerminalView {\n' \
          '    fn serialized_item_kind() -> &\'static str {\n' \
          '        "Terminal"\n' \
          '    }\n}'
    out = translate(src, "Terminal", "终端")
    assert '"Terminal"' in out, out
    assert "终端" not in out, out


def test_ui_text_still_translated() -> None:
    """防过度保护：保护区之外的同名字符串仍要翻译"""
    src = 'dispatch_context.add("Terminal");\n' \
          'let label = "Terminal";'
    out = translate(src, "Terminal", "终端")
    assert 'dispatch_context.add("Terminal")' in out, out
    assert 'let label = "终端";' in out, out


def test_existing_protections_intact() -> None:
    """原有规则不回归：字节串与属性宏仍受保护，#[error] 仍可翻译"""
    src = 'const MAGIC: &[u8] = b"Terminal";\n' \
          '#[serde(rename = "Terminal")]\n' \
          '#[error("Terminal")]'
    out = translate(src, "Terminal", "终端")
    assert 'b"Terminal"' in out, out
    assert '#[serde(rename = "Terminal")]' in out, out
    assert '#[error("终端")]' in out, "用户可见的错误消息应该被翻译"


def test_escape_still_works_for_normal_strings() -> None:
    """普通字符串的转义逻辑保持不变"""
    assert _escape_for_rust_source('行1\n行2') == '行1\\n行2'
    assert _escape_for_rust_source('说"你好"') == '说\\"你好\\"'
    assert _escape_for_rust_source(r'路径\n换行') == r'路径\n换行'


def test_nested_value_dropped() -> None:
    """模型把译文包成对象时必须视同解析失败，交给降级链重试

    放行的话下游 extract_placeholders 会对 dict 调 .replace()，
    异常在 try 之外冒泡，整个 workflow 挂掉。
    """
    raw = '{"Hello": {"translation": "你好"}}'
    assert parse_json_response(raw) == {}, "嵌套对象应被丢弃以触发降级"


def test_partial_nested_keeps_valid() -> None:
    """部分条目异常时保留合法的，异常条目视同未翻译"""
    raw = '{"Hello": "你好", "Bye": {"t": "再见"}, "N": 42, "X": null}'
    assert parse_json_response(raw) == {"Hello": "你好"}


def test_non_object_toplevel() -> None:
    """顶层不是对象时不能当成翻译结果"""
    assert parse_json_response("[1, 2, 3]") == {}
    assert parse_json_response('"just a string"') == {}


def test_normal_json_unaffected() -> None:
    """正常响应与 markdown 包裹的解析行为不变"""
    assert parse_json_response('{"Hello": "你好"}') == {"Hello": "你好"}
    fenced = '```json\n{"Hello": "你好", "Bye": ""}\n```'
    assert parse_json_response(fenced) == {"Hello": "你好", "Bye": ""}


def test_bad_response_no_longer_crashes_validation() -> None:
    """回归 CI 崩溃：解析结果必须能安全喂给占位符校验"""
    raw = '{"a {} b": {"translation": "甲 {} 乙"}, "c {}": "丙 {}"}'
    result = parse_json_response(raw)
    validate_placeholders(result)  # 修复前这里 AttributeError
    assert result == {"c {}": "丙 {}"}


def test_rules_never_claim_ui_text() -> None:
    """禁翻规则必须保守：误判会让界面文案永久得不到翻译

    这几条都曾被早期规则误判——"New:" 被当成 URI scheme，
    "Follow-up"/"Read/Write" 被当成带分隔符的标识符，"AI"/"OK"
    被当成裸标识符。
    """
    ui_text = [
        "New:", "From:", "Debugger:", "Searching:", "Input:", "WSL:",
        "Follow-up", "Read/Write", "AI", "OK", "Save", "Cancel",
        "Failed to open file", "Rename symbol",
    ]
    for s in ui_text:
        assert rule_verdict(s) is None, f"界面文案被误判: {s!r}"


def test_rules_catch_obvious_identifiers() -> None:
    """明确不该翻译的形态必须被规则拦下，否则白白送 AI"""
    expected = {
        "tool_name": "rule:identifier",
        "output_before_kill": "rule:identifier",
        "/tmp/foo": "rule:path",
        "main.rs": "rule:path",
        "theme.mode": "rule:path",
        "https://zed.dev/docs": "rule:uri",
        "mailto:user@example.com": "rule:uri",
        "  ": "rule:punctuation",
        "42": "rule:numeric",
        "100%": "rule:numeric",
        "": "rule:empty_string",
    }
    for s, want in expected.items():
        assert rule_verdict(s) == want, f"{s!r} -> {rule_verdict(s)}, 期望 {want}"


def test_skip_sets_missing_file() -> None:
    """清单缺失不是错误，返回空集让流程照常全量翻译"""
    assert load_skip_sets("") == (set(), set())
    assert load_skip_sets("/nonexistent/do_not_translate.json") == (set(), set())


def test_collect_candidates_only_empty_and_uncovered() -> None:
    """只收集译文为空、且尚未进清单的条目"""
    translations = {
        "a.rs": {"Save": "保存", "tool_name": "", "already": ""},
        "b.rs": {"globally_skipped": ""},
    }
    covered = {("a.rs", "already")}
    covered_global = {"globally_skipped"}
    got = collect_candidates(translations, covered, covered_global)
    assert got == [("a.rs", "tool_name")], got


def test_translate_async_uses_sessions_and_validates_placeholders() -> None:
    """会话产出的译文经占位符校验；不合格者置空留待重翻"""
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
            str(Path(__file__).parent / "config" / "glossary.yaml"),
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
    """会话失败时该批条目保持缺失（等同空值），不污染已有译文"""
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
            str(Path(__file__).parent / "config" / "glossary.yaml"),
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
            str(Path(__file__).parent / "config" / "glossary.yaml"),
            AIConfig(base_url="", api_key="", model="m", concurrency=2),
            time_limit=0.0,
        ))
    finally:
        tr.run_session = orig

    assert complete is False


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


def test_mcp_record_translation_appends_jsonl() -> None:
    """每次提交追加一行 JSONL，不覆盖已有内容"""
    out = Path(tempfile.mkdtemp()) / "out"
    from zedl10n.mcp_server import record_translation
    record_translation(str(out), "b1", "Save", "保存")
    record_translation(str(out), "b1", "Open", "打开")

    import json
    lines = (out / "b1.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2, lines
    assert json.loads(lines[0]) == {"original": "Save", "translated": "保存"}
    assert json.loads(lines[1]) == {"original": "Open", "translated": "打开"}


def test_mcp_record_translation_survives_interrupt() -> None:
    """会话中断后已写条目仍在：追加写入而非整文件重写"""
    out = Path(tempfile.mkdtemp()) / "out"
    from zedl10n.mcp_server import record_translation
    record_translation(str(out), "b2", "A", "甲")
    # 模拟中断：进程消失，但文件内容已在磁盘
    content = (out / "b2.jsonl").read_text(encoding="utf-8")
    assert "甲" in content


def test_mcp_lookup_term_terms_and_keep_original() -> None:
    """terms 返回约定译法；keep_original 返回保持原文"""
    g = Path(tempfile.mkdtemp()) / "glossary.yaml"
    g.write_text(
        "terms:\n  workspace: 工作区\n"
        "keep_original:\n  - Zed\n  - GitHub\n",
        encoding="utf-8",
    )
    from zedl10n.mcp_server import lookup_term
    assert "工作区" in lookup_term(str(g), "workspace")
    assert "保持原文" in lookup_term(str(g), "Zed")
    assert "未收录" in lookup_term(str(g), "nonexistent_term")


def test_mcp_lookup_term_missing_file_is_not_fatal() -> None:
    """词表缺失时返回未收录，而非抛异常中断翻译"""
    from zedl10n.mcp_server import lookup_term
    missing = Path(tempfile.mkdtemp()) / "nope.yaml"
    assert "未收录" in lookup_term(str(missing), "workspace")


def test_mcp_record_untranslatable() -> None:
    """标记禁翻写入独立文件，与译文分开"""
    dnt = Path(tempfile.mkdtemp()) / "dnt"
    from zedl10n.mcp_server import record_untranslatable
    record_untranslatable(str(dnt), "b3", "tool_name", "identifier")

    import json
    rec = json.loads((dnt / "b3.jsonl").read_text(encoding="utf-8").strip())
    assert rec == {"original": "tool_name", "reason": "identifier"}


def test_mcp_server_exposes_three_tools() -> None:
    """server 必须暴露全部 tool，名称与 agent 白名单一致"""
    import asyncio
    from zedl10n.mcp_server import build_server
    # 用公共 list_tools，避免依赖 SDK 私有属性
    tools = asyncio.run(build_server().list_tools())
    names = {t.name for t in tools}
    assert names == {
        "glossary_lookup", "submit_translation", "submit_translations",
        "mark_untranslatable", "mark_untranslatables",
    }, names


def test_collect_jsonl_normal() -> None:
    """正常 JSONL 解析为 原文→译文 映射"""
    p = Path(tempfile.mkdtemp()) / "b.jsonl"
    p.write_text(
        '{"original": "Save", "translated": "保存"}\n'
        '{"original": "Open", "translated": "打开"}\n',
        encoding="utf-8",
    )
    assert collect_jsonl(str(p)) == {"Save": "保存", "Open": "打开"}


def test_collect_jsonl_skips_bad_lines() -> None:
    """坏行（截断的 JSON）跳过，不影响其余条目"""
    p = Path(tempfile.mkdtemp()) / "b.jsonl"
    p.write_text(
        '{"original": "Save", "translated": "保存"}\n'
        '{"original": "Open", "translat\n'
        '{"original": "Quit", "translated": "退出"}\n',
        encoding="utf-8",
    )
    assert collect_jsonl(str(p)) == {"Save": "保存", "Quit": "退出"}


def test_collect_jsonl_skips_empty_and_missing_fields() -> None:
    """空译文与字段缺失的条目跳过：它们等同未翻译，留给下次"""
    p = Path(tempfile.mkdtemp()) / "b.jsonl"
    p.write_text(
        '{"original": "Save", "translated": ""}\n'
        '{"original": "Open"}\n'
        '{"translated": "无原文"}\n'
        '{"original": "Quit", "translated": "退出"}\n',
        encoding="utf-8",
    )
    assert collect_jsonl(str(p)) == {"Quit": "退出"}


def test_collect_jsonl_duplicate_last_wins() -> None:
    """同一条重复提交时取最后一次：agent 修正是正常行为"""
    p = Path(tempfile.mkdtemp()) / "b.jsonl"
    p.write_text(
        '{"original": "Save", "translated": "保存"}\n'
        '{"original": "Save", "translated": "存储"}\n',
        encoding="utf-8",
    )
    assert collect_jsonl(str(p)) == {"Save": "存储"}


def test_collect_jsonl_missing_file() -> None:
    """文件不存在返回空映射：会话失败时即此情形"""
    assert collect_jsonl(str(Path(tempfile.mkdtemp()) / "nope.jsonl")) == {}


def test_build_batch_prompt_lists_entries() -> None:
    """提示词必须逐条列出待翻译原文，并指明目标语言"""
    prompt = build_batch_prompt("a.rs", {"Save": "", "Open": ""}, "zh-CN")
    assert "Save" in prompt and "Open" in prompt
    assert "zh-CN" in prompt
    assert "submit_translation" in prompt


def test_write_mcp_config() -> None:
    """mcp.json 结构必须能被 Claude Code 识别"""
    import json
    p = Path(tempfile.mkdtemp()) / "mcp.json"
    write_mcp_config(str(p), "/abs/path/zedl10n/mcp_server.py")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    entry = cfg["mcpServers"]["l10n"]
    assert entry["command"]
    assert "mcp_server.py" in " ".join(entry["args"])


def test_group_batches_splits_oversize_file_into_chunks() -> None:
    """超大文件单独成批后仍按 max_entries 再切：单个会话工作量必须有上界"""
    strings = {"huge.rs": {f"h{i}": "" for i in range(1471)}}
    batches = group_batches(strings, target_size=200, oversize=300, max_entries=250)
    sizes = [len(b["huge.rs"]) for b in batches]
    assert len(batches) == 6, sizes
    assert max(sizes) <= 250, sizes
    # 不得丢条目
    assert sum(sizes) == 1471, sizes


def test_mcp_record_translations_batch() -> None:
    """批量提交：一次 tool 调用写多条，把模型往返次数从 N 降到 N/批。

    逐条提交时每条都要一轮 LLM 往返（实测约 3 秒/条），200 条的批次
    光往返就 10 分钟。批量提交是吞吐的关键。
    """
    out = Path(tempfile.mkdtemp()) / "out"
    from zedl10n.mcp_server import record_translations
    n = record_translations(str(out), "b9", [
        {"original": "Save", "translated": "保存"},
        {"original": "Open", "translated": "打开"},
        {"original": "Quit", "translated": "退出"},
    ])
    assert n == 3

    import json
    lines = (out / "b9.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3, lines
    assert json.loads(lines[1]) == {"original": "Open", "translated": "打开"}


def test_mcp_record_translations_skips_invalid_entries() -> None:
    """批量里的残缺条目跳过，不因一条坏数据丢掉整批"""
    out = Path(tempfile.mkdtemp()) / "out"
    from zedl10n.mcp_server import record_translations
    n = record_translations(str(out), "b10", [
        {"original": "Save", "translated": "保存"},
        {"original": "", "translated": "无原文"},
        {"original": "Empty", "translated": "  "},
        {"original": "Open"},  # 缺 translated
    ])
    assert n == 1, n


def test_mcp_record_translations_empty_list() -> None:
    """空列表是合法输入（agent 可能提交空批），返回 0 且不建文件"""
    out = Path(tempfile.mkdtemp()) / "out"
    from zedl10n.mcp_server import record_translations
    assert record_translations(str(out), "b11", []) == 0


def test_translate_async_keeps_partial_results_from_failed_session() -> None:
    """失败的会话若有部分译文，必须收下——超时前写的不能白写"""
    async def partial_session(args):
        from zedl10n.oc_translate import SessionResult
        return SessionResult(
            args.batch_id, {"Save": "保存"}, ok=False,
            detail="会话超时（300s）",
        )

    orig = tr.run_session
    tr.run_session = partial_session
    try:
        result, complete = asyncio.run(tr._translate_async(
            {"a.rs": {"Save": "", "Open": ""}},
            {},
            "incremental", "zh-CN",
            str(Path(__file__).parent / "config" / "glossary.yaml"),
            AIConfig(base_url="", api_key="", model="m", concurrency=2),
            time_limit=None,
        ))
    finally:
        tr.run_session = orig

    assert result["a.rs"]["Save"] == "保存", "超时前提交的译文被丢弃了"
    assert result["a.rs"]["Open"] == "", "未覆盖的条目应为空，留给下次"


def test_merge_untranslatable_rejects_agent_misjudgement() -> None:
    """agent 把可翻译文案标成禁翻时，规则复核必须拦住

    清单是不可逆的：误判入清单后该条永久不再翻译。
    """
    import tempfile as _tf
    from zedl10n import translate as _tr
    from zedl10n.untranslatable import _load_registry

    td = Path(_tf.mkdtemp())
    dnt = td / "dnt.json"
    orig_path = _tr.__dict__.get("_DNT_PATH_OVERRIDE")

    # 临时把 DNT_PATH 指到测试目录
    import zedl10n.untranslatable as un
    saved = un.DNT_PATH
    un.DNT_PATH = str(dnt)
    try:
        _tr._merge_untranslatable([
            {"original": "Save", "reason": "误判为标识符"},        # 界面文案，应拒绝
            {"original": "tool_name", "reason": "identifier"},      # 真标识符，应接受
        ])
        data, _, covered = _load_registry(str(dnt))
        assert "tool_name" in covered, "真标识符应入清单"
        assert "Save" not in covered, "界面文案被误判入清单，会永久失去翻译"
    finally:
        un.DNT_PATH = saved


def test_full_mode_does_not_wipe_existing_translations() -> None:
    """full 模式重翻时，本次没翻出来的条目保留旧译文而非置空"""
    async def partial_session(args):
        from zedl10n.oc_translate import SessionResult
        return SessionResult(args.batch_id, {"Save": "保存"}, ok=True)

    orig = tr.run_session
    tr.run_session = partial_session
    try:
        result, _ = asyncio.run(tr._translate_async(
            {"a.rs": {"Save": "", "Open": ""}},
            {"a.rs": {"Save": "旧保存", "Open": "打开"}},
            "full", "zh-CN",
            str(Path(__file__).parent / "config" / "glossary.yaml"),
            AIConfig(base_url="", api_key="", model="m", concurrency=2),
            time_limit=None,
        ))
    finally:
        tr.run_session = orig

    assert result["a.rs"]["Save"] == "保存", "成功翻出的应覆盖旧值"
    assert result["a.rs"]["Open"] == "打开", "没翻出的条目被清空了，好译文丢失"


def test_fix_placeholders_clears_bad_keeps_good() -> None:
    """占位符不匹配的条目置空（留待重翻），正常的原样保留"""
    import json
    td = Path(tempfile.mkdtemp())
    src = td / "src"
    (src / "crate" / "src").mkdir(parents=True)
    (src / "crate" / "src" / "a.rs").write_text("fn main() {}", encoding="utf-8")

    trans = td / "zh.json"
    trans.write_text(json.dumps({
        "crate/src/a.rs": {
            "Open {}": "打开 {}",       # 占位符正确
            "Save {}": "保存",          # 丢了占位符
        },
    }, ensure_ascii=False), encoding="utf-8")

    from zedl10n.fix_placeholders import fix_translation_json
    cleared, removed = fix_translation_json(str(trans), str(src))

    data = json.loads(trans.read_text(encoding="utf-8"))
    assert cleared == 1, cleared
    assert removed == 0, removed
    assert data["crate/src/a.rs"]["Open {}"] == "打开 {}"
    assert data["crate/src/a.rs"]["Save {}"] == "", "应为空值重翻，而非删除条目"
    assert "Save {}" in data["crate/src/a.rs"], "条目被删除了，将来永远不会再翻"


def test_fix_placeholders_removes_vanished_source_files() -> None:
    """源文件已删除的条目整组移除"""
    import json
    td = Path(tempfile.mkdtemp())
    src = td / "src"
    (src / "crate").mkdir(parents=True)
    (src / "crate" / "exists.rs").write_text("fn a() {}", encoding="utf-8")

    trans = td / "zh.json"
    trans.write_text(json.dumps({
        "crate/exists.rs": {"Save": "保存"},
        "crate/gone.rs": {"Open": "打开"},
    }, ensure_ascii=False), encoding="utf-8")

    from zedl10n.fix_placeholders import fix_translation_json
    cleared, removed = fix_translation_json(str(trans), str(src))

    data = json.loads(trans.read_text(encoding="utf-8"))
    assert removed == 1, removed
    assert cleared == 0, cleared
    assert "crate/exists.rs" in data
    assert "crate/gone.rs" not in data, "源文件已消失的条目应被清理"


def test_run_session_passes_prompt_via_stdin() -> None:
    """提示词必须走 stdin：Linux 单参数上限 128KB，走命令行会 E2BIG

    实测批次提示词可达 96KB 以上（多文件源码上下文 + 数百条字符串），
    作为命令行参数报 [Errno 7] Argument list too long。
    """
    import tempfile as _tf
    from zedl10n.oc_translate import SessionArgs, run_session

    td = Path(_tf.mkdtemp())
    # 构造一个明显超过 128KB 的提示词
    huge_prompt = "翻译以下内容\n" + ("x" * 200_000)

    import shutil
    true_bin = shutil.which("true") or "/usr/bin/true"
    args = SessionArgs(
        batch_id="big",
        prompt=huge_prompt,
        model="m",
        out_dir=str(td / "out"),
        dnt_dir=str(td / "dnt"),
        glossary_path="",
        mcp_config_path=str(td / "mcp.json"),
        # 用一个假的可执行文件，只验证参数长度不会触发 E2BIG
        claude_bin=true_bin,
        timeout=10,
    )
    import asyncio
    res = asyncio.run(run_session(args))

    # /bin/true 不产出任何条目，故 ok=False；关键是没有因参数过长而启动失败
    assert "Argument list too long" not in res.detail, res.detail
    assert "启动失败" not in res.detail, res.detail


def test_replace_judges_protection_on_original_content() -> None:
    """受保护判定必须基于原始内容，不能受先前替换造成的偏移影响

    旧实现每替换一条就重扫受保护区，位置随之前替换而漂移，导致部分
    本该保护的字符串（如 menu.action("Selected")）被错误翻译。
    """
    from zedl10n.replace import (
        _apply_all_replacements, _filter_replacements,
    )

    # 前面有两条可翻译条目（长度变化会推动后续位置），
    # 后面是一条落在 key_context 保护区里的标识符
    src = (
        'let a = "First Button";\n'
        'let b = "Second Button";\n'
        'key_context.add("Selected");\n'
    )
    reps = _filter_replacements(
        {"First Button": "第一个按钮", "Second Button": "第二个按钮",
         "Selected": "已选中"},
        "x.rs",
    )
    out, _ = _apply_all_replacements(src, reps)

    assert '"第一个按钮"' in out, out
    assert '"第二个按钮"' in out, out
    # key_context.add(...) 属于受保护模式，不得被翻译
    assert 'key_context.add("Selected")' in out, out
    assert '"已选中"' not in out, "受保护的标识符被错误翻译"


def main() -> int:
    logging.disable(logging.WARNING)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
