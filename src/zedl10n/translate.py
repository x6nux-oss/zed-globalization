"""AI 并发翻译，内置智能过滤"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from pathlib import Path

from .batch import group_batches
from .oc_translate import (
    SessionArgs,
    build_batch_prompt,
    load_glossary_inline,
    load_source_snippet,
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

log = logging.getLogger(__name__)


# 达到 --time-limit 时的退出码（EX_TEMPFAIL）：已完成部分已保存，
# 工作流据此跳过构建，留待下次运行增量续翻
EXIT_INCOMPLETE = 75


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
            # 已判定「确定无需翻译」的条目跳过，否则每轮增量都要把
            # 几万条代码标识符重新问一遍 AI。
            if s in skip_global or (file_path, s) in skip_covered:
                skipped += 1
                continue
            if mode == "full" or not str(
                result.get(file_path, {}).get(s, "")
            ).strip():
                # 增量模式也补翻空值：AI 偶尔返回空串，而只判断键存在
                # 会让空值永远跳过重翻，缺口越积越多。
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
    mcp_script = str(Path(__file__).resolve().parent / "mcp_server.py")
    mcp_config = str(run_dir / "mcp.json")
    write_mcp_config(mcp_config, mcp_script)

    semaphore = asyncio.Semaphore(ai_cfg.concurrency)
    untranslatable: list[dict] = []
    # 术语表内联进提示词：逐词查表要花一轮往返，是吞吐瓶颈之一
    glossary_text = load_glossary_inline(glossary_path)

    async def do_batch(idx: int, batch: dict) -> None:
        batch_id = f"b{idx:04d}"
        # 一个批次可含多个文件，提示词里逐个文件分段列出，
        # 每个文件都带上术语表与源码上下文（agent 读了就会反复读，
        # 实测 30 条读 9 次占掉大半时间）
        prompt_parts = [
            build_batch_prompt(
                fp, entries, lang, glossary_text,
                load_source_snippet(source_root, fp),
            )
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
            # 失败不中断：超时批次可能已提交部分译文，照样收下；
            # 未覆盖的条目保持缺失，下次增量自动补翻。
            log.warning(
                "批次 %s 失败（%s），已收到 %d 条译文",
                batch_id, res.detail, len(res.translations),
            )
        if not res.translations and not res.untranslatable:
            return
        untranslatable.extend(res.untranslatable)
        for fp, entries in batch.items():
            for original in entries:
                translated = res.translations.get(original, "")
                if not translated:
                    # 本次没翻出来：full 模式下这条是「待重翻」，
                    # 直接用空值会把已有的好译文抹掉，故保留旧值。
                    # 旧值本身是空的就仍然留空，缺口照常传给下次增量。
                    result.setdefault(fp, {}).setdefault(original, "")
                    continue
                result.setdefault(fp, {})[original] = translated

    total = len(batches)
    if total == 0:
        return result, True
    log.info("共 %d 个翻译会话，并发数 %d", total, ai_cfg.concurrency)

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
        # 到时限：放弃未完成会话，已完成部分照常保存，缺口由下次增量补翻
        complete = False
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    pbar.finish()

    # 占位符校验：不合格条目置空留待下次重翻。
    # 无降级通道后再开会话修占位符等于引入新一层不可控，
    # 空值重翻是已有机制，代价可控。
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
    清单结构为 {global_entries: [{original, reason}], entries: [...]}，
    全局条目写 global_entries，与 05 流程产出的形状保持一致。

    **写进清单是不可逆的**——条目一旦入清单，后续所有增量轮次都不再翻译它。
    agent 误判（把正常界面文案标成标识符）会让该条永久得不到翻译，
    因此这里只用 05 流程那套保守规则复核：规则认不出的，不进清单，
    留给下次翻译重试。宁可多问一遍 AI，也不可永久丢弃文案。
    """
    from . import untranslatable as un

    data, _, covered_global = un._load_registry(un.DNT_PATH)
    added = 0
    rejected: list[str] = []
    for rec in records:
        original = rec.get("original")
        if not original or original in covered_global:
            continue
        if un.rule_verdict(original) is None:
            # 规则认为这是可翻译文案，不采信 agent 的判定
            rejected.append(original)
            continue
        data["global_entries"].append({
            "original": original,
            "reason": f"agent: {rec.get('reason', 'unknown')}",
        })
        covered_global.add(original)
        added += 1

    if rejected:
        log.info(
            "拒绝 %d 条 agent 误判的禁翻标记（规则判定为可翻译）",
            len(rejected),
        )
    if added:
        save_json(data, un.DNT_PATH)
        log.info("禁翻清单新增 %d 条", added)


def translate_all(
    strings_path: str,
    output_path: str,
    context_path: str = "",
    glossary_path: str = "config/glossary.yaml",
    mode: str = "incremental",
    lang: str = "zh-CN",
    ai_cfg: AIConfig | None = None,
    source_root: str = "",
    do_not_translate: str = "",
    time_limit: float | None = None,
) -> bool:
    """同步入口，返回是否全部完成（False 表示达到 time_limit 中途停止）"""
    if ai_cfg is None:
        ai_cfg = AIConfig()
    ai_cfg.validate()

    all_strings: TranslationDict = load_json(strings_path)
    existing = load_json(output_path) if Path(output_path).exists() else {}

    from .untranslatable import load_skip_sets

    skip_sets = load_skip_sets(do_not_translate)
    if skip_sets[0] or skip_sets[1]:
        log.info(
            "已加载禁翻清单: %d 条文件级 + %d 条全局",
            len(skip_sets[0]), len(skip_sets[1]),
        )

    result, complete = asyncio.run(
        _translate_async(
            all_strings, existing, mode, lang,
            glossary_path, ai_cfg, source_root, skip_sets, time_limit,
        )
    )
    # 全角 ASCII 符号统一转半角，避免破坏 Rust 源码语法
    for fp in result:
        for s, t in result[fp].items():
            if t:
                result[fp][s] = normalize_fullwidth(t)

    # 规则兜底：AI 修复后仍有问题的，用规则强制统一
    from .consistency import fix_consistency

    result, fix_log = fix_consistency(result, glossary_path)
    for msg in fix_log:
        log.info("规则兜底修复: %s", msg)

    save_json(result, output_path)
    log.info("翻译结果已保存: %s", output_path)
    return complete


def run(args: argparse.Namespace) -> None:
    """CLI 入口"""
    ai_cfg = AIConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        model=args.model,
        concurrency=args.concurrency,
    )
    complete = translate_all(
        args.input,
        args.output,
        args.context,
        args.glossary,
        args.mode,
        args.lang,
        ai_cfg,
        source_root=getattr(args, "source_root", ""),
        do_not_translate=getattr(args, "do_not_translate", ""),
        time_limit=getattr(args, "time_limit", None),
    )
    if not complete:
        sys.exit(EXIT_INCOMPLETE)
