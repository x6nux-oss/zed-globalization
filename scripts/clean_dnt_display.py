#!/usr/bin/env python3
"""清理禁翻清单里误判的用户可见字符串（手工运行，不进 CI）。

判定标准：字符串出现在 `impl Display for X` 块内，即经 write!/format!
输出给用户看。这类条目必须从禁翻清单剔除，否则界面会残留英文。

用法：
    python3 scripts/clean_dnt_display.py config/do_not_translate.json --source-root zed
    python3 scripts/clean_dnt_display.py ... --dry-run   # 只看不改

清理后这些条目会在下次增量翻译时重新翻译。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from zedl10n.untranslatable import is_user_visible  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("registry", help="禁翻清单 JSON 路径")
    ap.add_argument("--source-root", required=True, help="Zed 源码根目录")
    ap.add_argument("--dry-run", action="store_true", help="只报告不修改")
    args = ap.parse_args()

    path = Path(args.registry)
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries", [])

    kept: list[dict] = []
    removed: list[dict] = []
    for e in entries:
        if is_user_visible(e.get("original", ""), e.get("file", ""),
                           args.source_root):
            removed.append(e)
        else:
            kept.append(e)

    print(f"清单共 {len(entries)} 条，剔除 Display 内误判 {len(removed)} 条")
    for e in removed:
        print(f"  - [{e.get('reason', '?')}] {e.get('original')!r}")
        print(f"      {e.get('file')}")

    if args.dry_run:
        print("\n--dry-run，未写入")
        return 0
    if not removed:
        print("无需修改")
        return 0

    data["entries"] = kept
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n已写回 {path}（剩 {len(kept)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
