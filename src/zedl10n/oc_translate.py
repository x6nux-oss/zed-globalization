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
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return SessionResult(args.batch_id, ok=False, detail=f"启动失败: {e}")

    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=args.timeout,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return SessionResult(
            args.batch_id, ok=False,
            detail=f"会话超时（{args.timeout:.0f}s）",
        )

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
