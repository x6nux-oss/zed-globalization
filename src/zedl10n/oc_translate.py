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

# Claude Code 可执行文件。CI 里固定版本安装，本地可用环境变量覆盖。
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
    # 会话使用的 agent 定义：翻译用 translator，禁翻判定用 classifier
    agent: str = "translator"


@dataclass
class SessionResult:
    """一次会话的结果"""

    batch_id: str
    translations: dict[str, str] = field(default_factory=dict)
    untranslatable: list[dict] = field(default_factory=list)
    ok: bool = False
    detail: str = ""
    # 判定会话专用：agent 是否明确报告了结论（哪怕一条都不标记）。
    # 用于区分「整批都该翻译」与「会话异常没跑完」。
    judged: bool = False


def load_glossary_inline(glossary_path: str) -> str:
    """把术语表内联成提示词片段。

    术语表只有几十条，内联进提示词比让 agent 逐词调 glossary_lookup
    便宜得多——每次查表都是一轮模型往返，实测占掉大半时间。
    文件缺失或解析失败时返回空串，翻译照常进行。
    """
    path = Path(glossary_path) if glossary_path else None
    if not path or not path.exists():
        return ""
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return ""
    terms = data.get("terms") or {}
    keep = data.get("keep_original") or []
    if not terms and not keep:
        return ""

    lines = ["\n## 术语表（必须遵守）\n"]
    if terms:
        lines.append("约定译法：")
        lines.extend(f"- {k} => {v}" for k, v in terms.items())
    if keep:
        lines.append("\n保持原文不译（专有名词）：")
        lines.append("- " + "、".join(str(k) for k in keep))
    return "\n".join(lines) + "\n"


def build_batch_prompt(
    file_path: str, strings: dict[str, str], lang: str,
    glossary_text: str = "", source_snippet: str = "",
) -> str:
    """构建批次提示词。

    术语表与源码上下文都内联：每次 Read/glossary_lookup 都是一轮完整的
    模型往返（实测约 4 秒），而 agent 一旦被允许读文件就会反复读
    （实测 30 条读了 9 次、占掉 39 秒里的绝大部分）。把上下文直接给它，
    往返次数才能压到 1~2 次。
    """
    entries = "\n".join(f"- {s}" for s in strings)
    source_block = (
        f"\n## 源码上下文（这些字符串所在的文件）\n"
        f"```rust\n{source_snippet}\n```\n"
        if source_snippet else ""
    )
    return (
        f"把下面 {len(strings)} 条 Zed 编辑器界面字符串翻译成 {lang}。\n"
        f"它们来自源文件 {file_path}。\n"
        f"{glossary_text}"
        f"{source_block}\n"
        f"## 工作方式（重要）\n"
        f"**每次 tool 调用都是一轮模型往返，往返次数决定整批速度。**\n"
        f"上下文已经在上面给全了，**不要调用 Read**。工作方式是：\n"
        f"1. 通读整批，一次性想好所有译文。\n"
        f"2. 调 **一次** `submit_translations`，传入全部译文（数组）。\n"
        f"3. 若有代码标识符，调 **一次** `mark_untranslatables` 批量标记。\n"
        f"理想情况 tool 调用总数是 1~2 次。\n\n"
        f"## 质量要求\n"
        f"- original 必须与下面列出的原文逐字符一致。\n"
        f"- 占位符（{{}}、{{0}}、%s 等）必须原样保留，数量与顺序都不能变。\n"
        f"- 正常界面文案必须翻译；纯代码标识符才标记为无需翻译。\n"
        f"- 条目键名用 `original` / `translated`。\n\n"
        f"## 待翻译条目（共 {len(strings)} 条）\n{entries}\n"
    )


def load_source_snippet(source_root: str, file_path: str, max_chars: int = 12000) -> str:
    """读取源文件内容作为上下文，超长则截断。

    截断而非放弃：开头部分通常已包含类型定义与主要字符串，
    足够判断语境；agent 本来也只能从片段里获得有限帮助。
    """
    if not source_root:
        return ""
    root = Path(source_root)
    for candidate in (root / file_path, root / file_path.removeprefix("zed/")):
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8")
            except Exception:
                return ""
            if len(text) <= max_chars:
                return text
            return text[:max_chars] + "\n// ...（文件过长，已截断）"
    return ""


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

    容错策略：坏行、空译文、字段缺失一律跳过——这些都等同「未翻译」，
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
    """读禁翻标记 JSONL，跳过判定结论记录行"""
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
        if rec.get("_judgement"):
            continue  # 结论行，不是条目
        if isinstance(rec.get("original"), str) and rec["original"]:
            records.append(rec)
    return records


def has_judgement(path: str) -> bool:
    """判定会话是否明确报告了结论（含「一条都不用标记」的情况）"""
    p = Path(path)
    if not p.exists():
        return False
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("_judgement"):
            return True
    return False


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

    # 提示词走 stdin，不走命令行参数：Linux 单参数上限 128KB
    # （MAX_ARG_STRLEN），一批多文件的源码上下文 + 字符串条目很容易超过，
    # 实测报 [Errno 7] Argument list too long。
    cmd = [
        args.claude_bin, "-p",
        "--agent", args.agent,
        "--model", args.model,
        "--output-format", "json",
        # 自动化场景无人审批；agent 工具白名单已限定可做的事
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
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return SessionResult(args.batch_id, ok=False, detail=f"启动失败: {e}")

    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(args.prompt.encode("utf-8")), timeout=args.timeout,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        # 超时前 agent 可能已提交部分译文：收回来，别丢掉。
        # 这批已写入的条目照样算数，缺口留给下次增量补。
        translations = collect_jsonl(str(out_path))
        untranslatable = collect_untranslatable(str(dnt_path))
        log.warning(
            "会话 %s 超时（%.0fs），已收回到期前提交的 %d 条译文",
            args.batch_id, args.timeout, len(translations),
        )
        return SessionResult(
            args.batch_id, translations, untranslatable, ok=False,
            detail=f"会话超时（{args.timeout:.0f}s）",
        )
    except asyncio.CancelledError:
        # 全局时限到达时上层会 cancel 本任务。必须在这里杀掉子进程，
        # 否则 claude 会变成孤儿继续跑，白白占用模型配额与 CPU。
        proc.kill()
        await proc.wait()
        raise

    translations = collect_jsonl(str(out_path))
    untranslatable = collect_untranslatable(str(dnt_path))
    judged = has_judgement(str(dnt_path))

    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace")[-500:]
        return SessionResult(
            args.batch_id, translations, untranslatable, ok=False,
            detail=f"退出码 {proc.returncode}: {tail}", judged=judged,
        )
    # 判定会话可能「一条都不标记」——整批都该翻译是合法结论。
    # 这种情况靠 report_judgement 的结论行区分，不能当成失败。
    if not translations and not untranslatable and not judged:
        return SessionResult(
            args.batch_id, ok=False, detail="会话未产出任何结果",
        )
    return SessionResult(
        args.batch_id, translations, untranslatable, ok=True, judged=judged,
    )
