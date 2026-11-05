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


def record_translations(
    out_dir: str, batch_id: str, entries: list[dict],
) -> int:
    """批量记录译文，返回实际写入条数。

    逐条提交时每条都要一轮模型往返（实测约 3 秒/条），200 条的批次
    光往返就 10 分钟，是吞吐的主要瓶颈。批量提交把往返次数压到
    「批次 / 单次提交量」。

    残缺条目跳过而非整批失败：一条坏数据不该让其余译文一起丢掉。
    同时接受 `translated` 与 `translation` 两种键名——模型两种都会用，
    只认一种会静默丢掉大半个批次（实测 30 条只成 17 条）。
    """
    valid: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        original = e.get("original")
        translated = e.get("translated")
        if translated is None:
            translated = e.get("translation")
        if not isinstance(original, str) or not original:
            continue
        if not isinstance(translated, str) or not translated.strip():
            continue
        valid.append({"original": original, "translated": translated})
    if not valid:
        return 0

    if not out_dir:
        raise RuntimeError("未配置输出目录（L10N_OUT_DIR）")
    target = Path(out_dir) / f"{batch_id}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        for rec in valid:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(valid)


def record_untranslatable(
    dnt_dir: str, batch_id: str, original: str, reason: str,
) -> str:
    """记录一条无需翻译的条目"""
    _append_jsonl(dnt_dir, batch_id, {"original": original, "reason": reason})
    return f"已标记无需翻译: {original} ({reason})"


def record_untranslatables(
    dnt_dir: str, batch_id: str, entries: list[dict],
) -> int:
    """批量记录无需翻译的条目，返回实际写入条数。

    逐条标记同样是每条一轮模型往返，批量可把往返压到 1 次。
    """
    valid: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        original = e.get("original")
        if not isinstance(original, str) or not original:
            continue
        reason = e.get("reason") or "unknown"
        valid.append({"original": original, "reason": str(reason)})
    if not valid:
        return 0

    if not dnt_dir:
        raise RuntimeError("未配置输出目录（L10N_DNT_DIR）")
    target = Path(dnt_dir) / f"{batch_id}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        for rec in valid:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(valid)


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
            "批量提交译文。**每翻完一批（建议 20~50 条）就调用一次本工具**，"
            "不要在回复正文里输出译文。original 必须与原文完全一致。"
            "批量提交远比逐条提交快：每次调用都要一轮模型往返。"
        ),
    )
    def submit_translations(entries: list[dict]) -> str:
        """批量提交译文，返回写入条数"""
        n = record_translations(out_dir, batch_id, entries)
        return f"已记录 {n} 条译文"

    @server.tool(
        description=(
            "提交单条译文。优先用 submit_translations 批量提交；"
            "本工具仅在需要单条补交时使用。"
        ),
    )
    def submit_translation(original: str, translated: str) -> str:
        """提交一条译文，追加写入 JSONL"""
        return record_translation(out_dir, batch_id, original, translated)

    @server.tool(
        description=(
            "批量标记无需翻译的字符串（纯代码标识符、变量名、key 名等）。"
            "正常界面文案必须翻译，只有确定是标识符时才用本工具。"
            "**一次性传入整批**，逐条标记会让整批慢十几倍。"
        ),
    )
    def mark_untranslatables(entries: list[dict]) -> str:
        """批量标记无需翻译的条目，返回写入条数"""
        n = record_untranslatables(dnt_dir, batch_id, entries)
        return f"已标记 {n} 条无需翻译"

    @server.tool(
        description=(
            "标记单条字符串无需翻译。优先用 mark_untranslatables 批量标记。"
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
