#!/usr/bin/env python3
"""翻译链路冒烟脚本（手工运行，不进 CI）。

取真实输入的少量批次跑一遍，报告耗时、成功率与 tool 调用情况。
用于升级 Claude Code 版本或更换模型后的回归验证。

用法：
    AI_BASE_URL=... AI_API_KEY=... AI_MODEL=... \
      ANTHROPIC_BASE_URL=... ANTHROPIC_API_KEY=... \
      python3 scripts/smoke_translate.py [批次数]

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
    all_batches = group_batches(all_strings)
    batches = all_batches[:limit]
    print(f"取 {len(batches)}/{len(all_batches)} 个批次冒烟，共 "
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
        avg = total_elapsed / len(batches)
        for conc in (5, 10, 20):
            print(f"  全量 {len(all_batches)} 会话、并发 {conc} 预计: "
                  f"{len(all_batches) * avg / conc / 60:.0f} 分钟")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
