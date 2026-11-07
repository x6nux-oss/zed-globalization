"""翻译结果清理：剔除失效条目。

改造后占位符校验已内联在 translate.py 的会话流程里（不合格条目直接置空、
留给下次增量重翻），此处不再调用 AI 修复——那既与翻译流程重复，又会在
修复失败时直接删条目。本模块只做不需要模型的收尾清理：
源文件已被删除的条目、以及不合格的空值。
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .replace import _check_placeholders  # noqa: F401
from .utils import (
    AIConfig, TranslationDict, extract_placeholders, load_json, save_json,
)

log = logging.getLogger(__name__)


def fix_translation_json(
    trans_path: str, source_root: str,
) -> tuple[int, int]:
    """扫描翻译 JSON，清空占位符不匹配的条目，删除源文件已消失的条目。

    占位符不匹配的条目置空而非删除：空值在下次增量里会被当作缺口重翻，
    而删除会让该条彻底消失、再也不会被翻译。

    Returns: (清空条数, 删除文件数)
    """
    data: TranslationDict = load_json(trans_path)
    root = Path(source_root)

    cleared = 0
    removed_files: list[str] = []

    for file_path in list(data.keys()):
        # 源文件不存在：整个文件条目作废
        rel = file_path[4:] if file_path.startswith("zed/") else file_path
        if not (root / rel).exists():
            removed_files.append(file_path)
            continue

        entries = data[file_path]
        for original, translated in list(entries.items()):
            if not translated:
                continue
            src_ph = extract_placeholders(original)
            dst_ph = extract_placeholders(translated)
            if _check_placeholders(src_ph, dst_ph):
                continue
            log.info(
                "占位符不匹配，置空待重翻: %r → %r (原文=%s, 译文=%s) [%s]",
                original, translated, src_ph, dst_ph, file_path,
            )
            entries[original] = ""
            cleared += 1

    for fp in removed_files:
        del data[fp]

    save_json(data, trans_path)

    if removed_files:
        log.info("已清理 %d 个不存在的文件条目:", len(removed_files))
        for f in removed_files[:20]:
            log.info("  - %s", f)
    log.info(
        "清理完成: 置空 %d 条, 清理文件 %d 个",
        cleared, len(removed_files),
    )
    return cleared, len(removed_files)


def run(args: argparse.Namespace) -> None:
    """CLI 入口"""
    # AIConfig 仅用于兼容既有 CLI 参数；本流程不需要任何模型调用
    AIConfig()
    fix_translation_json(args.input, args.source_root)
