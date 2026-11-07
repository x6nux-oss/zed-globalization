"""扫描翻译结果中的空值，判定哪些「确定无需翻译」并写入禁翻清单。

背景：翻译结果里空字符串承载了两种互斥语义——「AI 调用失败，该重试」
和「这就是个代码标识符，永远不必翻译」。两者分不开，所以增量模式只能
把全部空值重翻一遍（translate.py 的补翻空值分支）。实测 v1.20.1-pre
的 zh-CN.json 里 93993 条中有 62394 条是空值，每轮增量都在重翻它们。

本模块把第二种语义固化到 config/do_not_translate.json：先用保守规则
判定标识符/路径/URI 等确定无疑的，剩余语义模糊的交给 AI。清单一旦写
入，translate.py 会在增量判断时直接跳过，不再重翻。

判定务必保守：漏判只是少省一点 token，误判会让真正的界面文案永久得
不到翻译。因此规则只覆盖「绝无可能是用户可见文案」的形态。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import signal
import sys
from pathlib import Path

from .utils import (
    AIConfig, ProgressBar, TranslationDict, load_json, save_json,
)

log = logging.getLogger(__name__)

# 禁翻清单默认路径。05 流程与翻译流程共用，集中在此便于对齐。
DNT_PATH = "config/do_not_translate.json"

# 全局状态：用于信号处理
_shutdown_requested = False
_current_data = None
_current_dnt_path = None


def _signal_handler(signum, frame):
    """处理 SIGTERM/SIGINT 信号，保存当前进度后退出。"""
    global _shutdown_requested, _current_data, _current_dnt_path
    log.warning("收到信号 %d，准备优雅退出", signum)
    _shutdown_requested = True

    # 立即保存当前进度
    if _current_data is not None and _current_dnt_path is not None:
        try:
            save_json(_current_data, _current_dnt_path)
            log.info("已保存当前进度到 %s", _current_dnt_path)
        except Exception as e:
            log.error("保存进度失败: %s", e)

    sys.exit(0)

# ---------------------------------------------------------------------------
# 保守规则：命中即判定为无需翻译，reason 统一加 rule: 前缀以便追溯来源
# ---------------------------------------------------------------------------

# 纯标点/空白（含常见全角标点），翻译它们会破坏语法
_PUNCT = re.compile(r'^[\s\x20-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e…·—–«»“”‘’]+$')

# 只由转义序列组成：\n、\\n、\r\n 等
_ESCAPE_ONLY = re.compile(r'^(?:\\+[nrt0]|\s)+$')

# 只由格式占位符和分隔符组成：{}、{:?}、%s、", " 等
_FMT_ONLY = re.compile(r'^(?:\{[^{}]*\}|%\w|\s|[.,:;/-])+$')

# 数字、版本号、带单位的量：42、0.8、16px、100%
_NUMERIC = re.compile(r'^[+-]?\d+(?:\.\d+)?(?:[a-zA-Z%]{0,3})$')

# base64 / 十六进制长串：图片数据、哈希
_B64HEX = re.compile(r'^[A-Za-z0-9+/=]{40,}$|^[0-9a-fA-F]{16,}$')

# 绝对路径与家目录路径：/tmp/foo、~/.config/zed
_ABS_PATH = re.compile(r'^~?/[A-Za-z0-9_./-]*$')

# 裸文件名：foo.rs、settings.json
_FILE_NAME = re.compile(r'^[A-Za-z0-9_-]+\.[A-Za-z0-9]{1,5}$')

# URI：只认已知 scheme。若放宽成「任意字母串 + 冒号」，界面上的
# "New:"、"Debugger:"、"Input:" 会被整片误判。
_URI = re.compile(
    r'^(?:https?|ftp|file|mailto|data|ssh|git|ws|wss|socks5h?|chrome|zed)'
    r':(?://)?[^\s{}]{2,}$',
    re.I,
)

# 转义换行分隔的小写单词，典型的测试夹具：one\ntwo\nthree\n
_WORDS_NL = re.compile(r'^(?:[a-z0-9_-]+(?:\\+n|\n))+[a-z0-9_-]*$')

# 单个标识符：snake_case、camelCase、CONST_NAME
_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

# 标识符必须带下划线或数字才由规则拍板。纯字母的词（AI、OK、Save、
# EchoEcho）既可能是类型名也可能是按钮文案，规则无从分辨，交给 AI。
# 这一条把规则覆盖率从 59% 压到 37%，但换来的是误判基本归零——漏判
# 只是少省点 token，误判会让界面文案永久得不到翻译。
_IDENT_STRONG = re.compile(r'[_0-9]')

# 带分隔符的标识符/模块路径：foo.bar、a::b、lsp/rust-analyzer。
# 首段要求全小写，否则 "Follow-up"、"Read/Write" 这类复合词会被误判。
_DOTTED = re.compile(
    r'^[a-z_][a-z0-9_]*(?:[./:-][A-Za-z0-9_]+)+$'
    r'|^[a-z0-9_-]+(?:/[a-z0-9_.-]+)+/?$'
)


def rule_verdict(s: str) -> str | None:
    """用保守规则判定，返回 reason；无法确定时返回 None 交给 AI。"""
    if s == "":
        return "rule:empty_string"
    if _PUNCT.match(s):
        return "rule:punctuation"
    if _ESCAPE_ONLY.match(s):
        return "rule:escape_sequence"
    if _FMT_ONLY.match(s):
        return "rule:format_only"
    if _NUMERIC.match(s):
        return "rule:numeric"
    if _B64HEX.match(s):
        return "rule:binary_data"
    if _ABS_PATH.match(s) or _FILE_NAME.match(s):
        return "rule:path"
    if _URI.match(s) and " " not in s:
        return "rule:uri"
    if _WORDS_NL.match(s):
        return "rule:test_fixture"
    if _IDENT.match(s) and _IDENT_STRONG.search(s):
        return "rule:identifier"
    if _DOTTED.match(s):
        return "rule:identifier"
    return None


# ---------------------------------------------------------------------------
# AI 判定：规则拿不准的交给模型，reason 沿用清单里既有的分类词汇
# ---------------------------------------------------------------------------

# 单批送审的字符预算，判定任务比翻译轻，可以放得比翻译批次大
_BATCH_CHARS = 100000


def _build_batches(
    items: list[tuple[str, str]],
) -> list[tuple[str, list[str]]]:
    """按文件分组并切分批次，返回 [(file_path, [原文...]), ...]。

    带上文件路径是因为同一个字符串在测试文件和界面文件里的性质完全不同。
    """
    by_file: dict[str, list[str]] = {}
    for file_path, s in items:
        by_file.setdefault(file_path, []).append(s)

    batches: list[tuple[str, list[str]]] = []
    for file_path, strings in by_file.items():
        cur: list[str] = []
        size = 0
        for s in strings:
            if cur and size + len(s) > _BATCH_CHARS:
                batches.append((file_path, cur))
                cur, size = [], 0
            cur.append(s)
            size += len(s)
        if cur:
            batches.append((file_path, cur))
    return batches


def build_judge_prompt(
    file_path: str, strings: list[str], source_snippet: str = "",
) -> str:
    """构建判定提示词。

    与翻译一致：上下文内联、要求一次 tool 调用交齐，避免往返开销。
    """
    entries = "\n".join(f"- {s}" for s in strings)
    source_block = (
        f"\n## 源码上下文\n```rust\n{source_snippet}\n```\n"
        if source_snippet else ""
    )
    return (
        f"判断下面 {len(strings)} 条从 Zed 源码提取的字符串里，"
        f"哪些**永远不需要翻译**（纯代码标识符）。\n"
        f"它们来自 {file_path}。\n"
        f"{source_block}\n"
        f"## 工作方式（重要）\n"
        f"每次 tool 调用都是一轮模型往返。步骤如下：\n"
        f"1. 通读整批，判断每条是否需要翻译。\n"
        f"2. 调 **一次** `mark_untranslatables`，把判定为无需翻译的条目传入。\n"
        f"   若一条都不需要翻译，跳过这步。\n"
        f"3. **必须**再调一次 `report_judgement` 报告结论——"
        f"即使一条都没标记也要调，否则系统无法判断是判定完成还是会话中断。\n"
        f"需要翻译的条目**不要提交**。拿不准的也不要提交。\n\n"
        f"## 待判定条目（共 {len(strings)} 条）\n{entries}\n"
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _load_registry(dnt_path: str) -> tuple[dict, set[tuple[str, str]], set[str]]:
    """加载现有禁翻清单，返回 (原始数据, 文件级已覆盖, 全局已覆盖)。"""
    p = Path(dnt_path)
    if not p.exists():
        log.info("禁翻清单不存在，将新建: %s", dnt_path)
        data = {
            "description": "Strings that must never be translated - they are"
                           " used as program identifiers, not user-facing text",
            "global_entries": [],
            "entries": [],
        }
    else:
        data = load_json(dnt_path)
    data.setdefault("entries", [])
    data.setdefault("global_entries", [])
    covered = {(e["file"], e["original"]) for e in data["entries"]}
    covered_global = {e["original"] for e in data["global_entries"]}
    log.info(
        "现有清单: %d 条文件级 + %d 条全局",
        len(covered), len(covered_global),
    )
    return data, covered, covered_global


def load_skip_sets(path: str) -> tuple[set[tuple[str, str]], set[str]]:
    """加载禁翻清单供翻译阶段跳过用，返回 (文件级集合, 全局集合)。

    清单缺失不是错误——首次运行或未接入该流程时返回空集，照常全量翻译。
    """
    if not path or not Path(path).exists():
        return set(), set()
    try:
        data = load_json(path)
    except Exception as e:
        log.warning("禁翻清单读取失败，忽略: %s (%s)", path, e)
        return set(), set()
    covered = {
        (e["file"], e["original"])
        for e in data.get("entries", [])
        if "file" in e and "original" in e
    }
    covered_global = {
        e["original"] for e in data.get("global_entries", []) if "original" in e
    }
    return covered, covered_global


def collect_candidates(
    translations: TranslationDict,
    covered: set[tuple[str, str]],
    covered_global: set[str],
) -> list[tuple[str, str]]:
    """收集译文为空且尚未进入清单的条目。"""
    out: list[tuple[str, str]] = []
    for file_path, pairs in translations.items():
        for original, translated in pairs.items():
            if str(translated).strip():
                continue  # 已有译文，与本模块无关
            if original in covered_global or (file_path, original) in covered:
                continue  # 已在清单里
            out.append((file_path, original))
    return out


# ---------------------------------------------------------------------------
# 用户可见性校验：Display 实现里的字符串一律可翻译
# ---------------------------------------------------------------------------

# `impl Display for X { ... }` 块内的字符串会经 write!/format! 输出，
# 最终展示给用户。这类字符串被误判为「无需翻译」会让界面出现英文。
# 实测 v1.28 曾把 ToolCallStatus 的 In Progress / Completed / Failed、
# LoadError 的 Failed to install: {msg} 等 44 条误判进禁翻清单。
#
# 只匹配 Display：`impl Error for X {}` 常是空块，若不排除，它会先被
# 当成一个 impl 块闭合，导致紧随其后的 Display 块没被覆盖。
_DISPLAY_IMPL = re.compile(
    r'impl(?:\s*<[^>]*>)?\s+(?:(?:std::)?fmt::)?Display\s+for\b',
)


def _display_line_ranges(content: str) -> list[tuple[int, int]]:
    """返回所有 Display/Error impl 块的字符区间。

    用花括号配对定位块尾，足以应付 Rust 正常缩进的代码；不追求
    完备的语法分析——误判成「在 Display 里」只会让该条多翻一次，
    代价远小于漏判导致的界面英文。
    """
    ranges: list[tuple[int, int]] = []
    for m in _DISPLAY_IMPL.finditer(content):
        start = m.start()
        # 找到 impl 之后第一个 { 作为块起点
        brace = content.find("{", m.end())
        if brace == -1:
            continue
        depth = 0
        for i in range(brace, len(content)):
            ch = content[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    ranges.append((start, i + 1))
                    break
    return ranges


def is_user_visible(
    original: str, file_path: str, source_root: str,
) -> bool:
    """判断字符串是否出现在 Display/Error 实现里（即会展示给用户）。

    找不到源码时返回 False（无法判断，维持调用方的原有判定）。
    """
    if not source_root or not file_path:
        return False
    root = Path(source_root)
    rel = file_path[4:] if file_path.startswith("zed/") else file_path
    p = root / rel
    if not p.is_file():
        return False
    try:
        content = p.read_text(encoding="utf-8")
    except OSError:
        return False
    if "Display" not in content and "Error" not in content:
        return False

    needle = f'"{original}"'
    ranges = _display_line_ranges(content)
    if not ranges:
        return False
    pos = 0
    while True:
        idx = content.find(needle, pos)
        if idx == -1:
            break
        if any(s <= idx < e for s, e in ranges):
            return True
        pos = idx + len(needle)
    return False


async def _judge_all(
    items: list[tuple[str, str]],
    ai_cfg: AIConfig,
    data: dict,
    dnt_path: str,
    source_root: str = "",
) -> int:
    """并发跑 Claude Code 判定会话，每完成一批立即追加并保存进度，返回判定总数。

    与翻译共用同一套会话机制（oc_translate），只是换 agent 定义与
    MCP tool：判定用 classifier + mark_untranslatables，结果同样落 JSONL。
    """
    from .oc_translate import (
        SessionArgs, collect_untranslatable, load_source_snippet,
        run_session, write_mcp_config,
    )

    run_dir = Path("i18n/.judge")
    out_dir = run_dir / "out"
    dnt_dir = run_dir / "dnt"
    out_dir.mkdir(parents=True, exist_ok=True)
    dnt_dir.mkdir(parents=True, exist_ok=True)
    mcp_config = str(run_dir / "mcp.json")
    write_mcp_config(
        mcp_config, str(Path(__file__).resolve().parent / "mcp_server.py"),
    )

    semaphore = asyncio.Semaphore(ai_cfg.concurrency)
    batches = _build_batches(items)
    log.info("AI 判定: %d 条，拆分为 %d 批", len(items), len(batches))

    async def one(
        idx: int, fp: str, strings: list[str],
    ) -> tuple[str, dict[str, str]]:
        if _shutdown_requested:
            log.info("收到关闭信号，停止处理新批次")
            return fp, {}
        batch_id = f"j{idx:04d}"
        prompt = build_judge_prompt(
            fp, strings, load_source_snippet(source_root, fp),
        )
        args = SessionArgs(
            batch_id=batch_id, prompt=prompt, model=ai_cfg.model,
            out_dir=str(out_dir), dnt_dir=str(dnt_dir),
            glossary_path="", mcp_config_path=mcp_config,
            agent="classifier",
        )
        async with semaphore:
            res = await run_session(args)
        if not res.ok:
            log.warning("判定批次 %s 失败: %s", batch_id, res.detail)
        # 判定结果落在 dnt JSONL：{original, reason}
        return fp, {
            r["original"]: r.get("reason", "unknown")
            for r in res.untranslatable
            if r.get("original") in set(strings)
        }

    tasks = [
        asyncio.create_task(one(i, fp, ss))
        for i, (fp, ss) in enumerate(batches)
    ]
    judged_count = 0
    pbar = ProgressBar(len(tasks), desc="判定")
    for coro in asyncio.as_completed(tasks):
        if _shutdown_requested:
            log.info("收到关闭信号，取消剩余任务")
            for task in tasks:
                if not task.done():
                    task.cancel()
            break
        fp, judged = await coro
        # 立即追加到 data 并保存，确保每批完成后进度都被持久化
        for s, reason in judged.items():
            entry = {"file": fp, "original": s, "reason": f"ai:{reason}"}
            data["entries"].append(entry)
            judged_count += 1
        if judged:
            save_json(data, dnt_path)
        pbar.update(extra=f"已判定 {judged_count}")
    pbar.finish()
    return judged_count


async def _recheck_entries(
    data: dict,
    dnt_path: str,
    ai_cfg: AIConfig | None,
    source_root: str,
    limit: int = 0,
) -> int:
    """复查清单里已有的条目，剔除 AI 认为该翻译的。返回剔除条数。

    与新增判定走同一套会话机制，区别是「提交」的语义反过来：
    classifier 只提交它认为无需翻译的条目，所以**没被提交的**就是
    该翻译的，从清单里剔除。

    保守起见，整批失败（会话超时/异常）时不动该批任何条目——
    宁可留着旧判定，也不能因为一次失败就把整片文案放出来重翻。
    """
    entries = data.get("entries", [])
    if not entries:
        log.info("复查：清单为空，跳过")
        return 0
    if not ai_cfg:
        log.warning("复查需要 AI 判定，但未提供 AI 配置，跳过")
        return 0

    items = [(e["file"], e["original"]) for e in entries]
    if limit > 0:
        log.info("复查：按 --limit 只处理前 %d 条", limit)
        items = items[:limit]

    # 逐条复查：把清单条目当作候选重新送判定
    from .oc_translate import (
        SessionArgs, load_source_snippet, run_session, write_mcp_config,
    )

    run_dir = Path("i18n/.recheck")
    out_dir = run_dir / "out"
    dnt_dir = run_dir / "dnt"
    out_dir.mkdir(parents=True, exist_ok=True)
    dnt_dir.mkdir(parents=True, exist_ok=True)
    mcp_config = str(run_dir / "mcp.json")
    write_mcp_config(
        mcp_config, str(Path(__file__).resolve().parent / "mcp_server.py"),
    )

    semaphore = asyncio.Semaphore(ai_cfg.concurrency)
    batches = _build_batches(items)
    log.info("复查：%d 条，拆分为 %d 批", len(items), len(batches))

    # 每批的判定结果：batch_id -> 被判定为「无需翻译」的原文集合
    confirmed: dict[int, set[str]] = {}
    failed: set[int] = set()

    async def one(idx: int, fp: str, strings: list[str]) -> None:
        if _shutdown_requested:
            return
        batch_id = f"r{idx:04d}"
        prompt = build_judge_prompt(
            fp, strings, load_source_snippet(source_root, fp),
        )
        args = SessionArgs(
            batch_id=batch_id, prompt=prompt, model=ai_cfg.model,
            out_dir=str(out_dir), dnt_dir=str(dnt_dir),
            glossary_path="", mcp_config_path=mcp_config,
            agent="classifier",
        )
        async with semaphore:
            res = await run_session(args)
        if not res.ok:
            # 失败批次不动：保守，避免整批文案被误放出。
            # 注意：agent 通过 report_judgement 明确报告过结论的批次，
            # 即使一条都没标记也会走 ok=True 分支（整批都该翻译是合法结论）。
            log.warning("复查批次 %s 失败，保留原判定: %s", batch_id, res.detail)
            failed.add(idx)
            return
        keep = {
            r["original"] for r in res.untranslatable if r.get("original")
        }
        if not keep:
            log.info(
                "复查批次 %s：整批 %d 条都该翻译，全部剔除",
                batch_id, len(strings),
            )
        confirmed[idx] = keep

    tasks = [
        asyncio.create_task(one(i, fp, ss))
        for i, (fp, ss) in enumerate(batches)
    ]
    pbar = ProgressBar(len(tasks), desc="复查")
    for coro in asyncio.as_completed(tasks):
        await coro
        pbar.update()
    pbar.finish()

    # 重新构造清单：只保留仍被判定为无需翻译的条目。
    # 先建「该保留的 (file, original) 集合」再一遍过滤，避免 O(n²)。
    keep_pairs: set[tuple[str, str]] = set()
    for idx, (fp, ss) in enumerate(batches):
        if idx in failed or idx not in confirmed:
            # 失败/未完成的批次原样保留
            keep_pairs.update((fp, s) for s in ss)
        else:
            keep_pairs.update((fp, s) for s in confirmed[idx])

    covered_pairs = {(fp, s) for fp, ss in batches for s in ss}
    kept: list[dict] = []
    removed = 0
    for e in entries:
        pair = (e["file"], e["original"])
        if pair in covered_pairs and pair not in keep_pairs:
            removed += 1
            continue
        kept.append(e)

    data["entries"] = kept
    save_json(data, dnt_path)
    log.info(
        "复查完成：%d 条 → %d 条，剔除 %d 条（失败批次 %d 个已保留原判定）",
        len(entries), len(kept), removed, len(failed),
    )
    return removed


def mark_untranslatable(
    trans_path: str,
    dnt_path: str,
    ai_cfg: AIConfig | None = None,
    use_ai: bool = True,
    limit: int = 0,
    source_root: str = "",
    recheck_existing: bool = False,
) -> dict[str, int]:
    """扫描翻译文件中的空值并扩充禁翻清单，返回统计。"""
    global _current_data, _current_dnt_path

    # 注册信号处理器
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    translations: TranslationDict = load_json(trans_path)
    data, covered, covered_global = _load_registry(dnt_path)

    # 设置全局变量，以便信号处理器可以保存进度
    _current_data = data
    _current_dnt_path = dnt_path

    # 复查已有规则：把清单里已有的条目重新送判定，AI 认为该翻译的剔除。
    # 这是唯一能纠正历史误判的途径——collect_candidates 只看「不在清单里」
    # 的条目，已在清单中的永远得不到第二次审视。
    rechecked_removed = 0
    if recheck_existing:
        if not use_ai:
            log.warning("--recheck-existing 需要 AI 判定，与 --rules-only 冲突，跳过复查")
        else:
            rechecked_removed = asyncio.run(
                _recheck_entries(data, dnt_path, ai_cfg, source_root, limit),
            )
            covered = {(e["file"], e["original"]) for e in data["entries"]}
            covered_global = {e["original"] for e in data["global_entries"]}

    candidates = collect_candidates(translations, covered, covered_global)
    log.info("待判定空值条目: %d", len(candidates))
    if not candidates:
        return {"candidates": 0, "by_rule": 0, "by_ai": 0, "added": 0}

    # Display/Error 实现里的字符串会展示给用户，绝不能进禁翻清单。
    # 在候选阶段就剔除：既省 AI 调用，也避免规则误判（rule_verdict 只看
    # 字符串形态，看不出上下文）。
    if source_root:
        before = len(candidates)
        candidates = [
            (fp, s) for fp, s in candidates
            if not is_user_visible(s, fp, source_root)
        ]
        excluded = before - len(candidates)
        if excluded:
            log.info("剔除 %d 条 Display 实现内的用户可见字符串", excluded)
    else:
        log.warning(
            "未提供 --source-root，跳过用户可见性校验；"
            "Display 内的文案可能被误判为禁翻",
        )

    # 实时追加到 data["entries"]，确保信号处理器能保存增量进度
    added_count = 0
    undecided: list[tuple[str, str]] = []
    for file_path, original in candidates:
        if _shutdown_requested:
            log.warning("收到关闭信号，停止规则判定")
            break
        reason = rule_verdict(original)
        if reason:
            entry = {"file": file_path, "original": original, "reason": reason}
            data["entries"].append(entry)
            added_count += 1
        else:
            undecided.append((file_path, original))
    by_rule = added_count
    log.info("规则判定 %d 条，剩余 %d 条待 AI 判定", by_rule, len(undecided))

    by_ai = 0
    if use_ai and undecided and not _shutdown_requested:
        if limit > 0 and len(undecided) > limit:
            log.info("本次只处理前 %d 条（--limit）", limit)
            undecided = undecided[:limit]
        if ai_cfg is None:
            ai_cfg = AIConfig()
        # _judge_all 现在直接追加到 data 并返回判定总数
        by_ai = asyncio.run(
            _judge_all(undecided, ai_cfg, data, dnt_path, source_root),
        )
        added_count += by_ai

    # 最终排序并保存
    data["entries"].sort(key=lambda e: (e["file"], e["original"]))
    save_json(data, dnt_path)

    # 清除全局变量
    _current_data = None
    _current_dnt_path = None

    if _shutdown_requested:
        log.warning("由于收到关闭信号，提前保存了 %d 条新增条目", added_count)
    else:
        log.info(
            "清单已更新: 新增 %d 条（规则 %d + AI %d），总计 %d 条",
            added_count, by_rule, by_ai, len(data["entries"]),
        )
    return {
        "candidates": len(candidates),
        "by_rule": by_rule,
        "by_ai": by_ai,
        "added": added_count,
    }


def run(args: argparse.Namespace) -> None:
    """CLI 入口"""
    ai_cfg = AIConfig(
        base_url=getattr(args, "base_url", ""),
        api_key=getattr(args, "api_key", ""),
        model=getattr(args, "model", ""),
        concurrency=getattr(args, "concurrency", 10),
    )
    stats = mark_untranslatable(
        args.input,
        args.do_not_translate,
        ai_cfg=ai_cfg,
        use_ai=not args.rules_only,
        limit=args.limit,
        source_root=getattr(args, "source_root", ""),
        recheck_existing=getattr(args, "recheck_existing", False),
    )
    log.info(
        "完成: 候选 %d, 规则 %d, AI %d, 新增 %d",
        stats["candidates"], stats["by_rule"], stats["by_ai"], stats["added"],
    )
