"""GitHub REST API 统一访问封装。

为 ``src/`` 下所有 GitHub API 调用提供：

* 认证（``GITHUB_TOKEN`` / ``GH_TOKEN`` → ``Authorization: Bearer``），把限额从
  60 次/小时提升到 5000 次/小时；
* 指数退避重试：``urllib3.Retry(total=5, backoff_factor=1,
  status_forcelist=[403, 429, 500, 502, 503, 504], respect_retry_after_header=True)``，
  并叠加随机抖动；
* 空响应防御：2xx 但响应体为空 / 非法 JSON / 结构不符合预期时，按可重试处理，
  耗尽后抛 :class:`GitHubAPIError`（带状态码与脱敏后的响应片段），
  调用方不会再拿到 ``None`` 去迭代。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"

#: 需要重试的 HTTP 状态码。403 是 GitHub 限流时最常见的返回码，一并重试。
RETRYABLE_STATUS = frozenset({403, 429, 500, 502, 503, 504})

#: 立即失败、不重试的状态码（凭据问题 / 资源不存在，重试无意义）。
FATAL_STATUS = frozenset({400, 401, 404, 422})

_SNIPPET_CHARS = 500


class GitHubAPIError(RuntimeError):
    """GitHub API 不可用，或返回了空 / 非预期的响应结构。"""

    def __init__(
        self, message: str, *, status: int | None = None, snippet: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.snippet = snippet

    def __str__(self) -> str:  # pragma: no cover - 仅影响日志可读性
        base = super().__str__()
        if self.status is None:
            return base
        return f"{base}（HTTP {self.status}）"


class _JitterRetry(Retry):
    """在 urllib3 的指数退避上叠加随机抖动，避免并发请求同步重试。"""

    def get_backoff_time(self) -> float:
        backoff = super().get_backoff_time()
        if backoff:
            backoff += random.uniform(0, backoff / 2)
        return backoff


def token_from_env() -> str:
    """读取 GitHub token；不存在时返回空串（降级为匿名请求）。"""
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def build_session(
    token: str | None = None,
    *,
    total: int = 5,
    backoff_factor: float = 1.0,
) -> requests.Session:
    """构造带重试策略的 ``requests.Session``。"""
    session = requests.Session()
    session.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "zedl10n-github-api",
    })
    tok = token_from_env() if token is None else token
    if tok:
        session.headers["Authorization"] = f"Bearer {tok}"

    retry = _JitterRetry(
        total=total,
        backoff_factor=backoff_factor,
        status_forcelist=sorted(RETRYABLE_STATUS),
        respect_retry_after_header=True,
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        # 不在这里抛，交给调用方读取状态码并给出带响应片段的错误
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_SESSION: requests.Session | None = None


def default_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = build_session()
    return _SESSION


def _redact(text: str) -> str:
    token = token_from_env()
    return text.replace(token, "[REDACTED]") if token else text


def _snippet(text: str) -> str:
    return " ".join(text.split())[:_SNIPPET_CHARS]


def _absolute(target: str) -> str:
    if target.startswith(("http://", "https://")):
        return target
    return f"{API_ROOT}/{target.lstrip('/')}"


def request_json(
    target: str,
    *,
    session: requests.Session | None = None,
    params: dict[str, Any] | None = None,
    expect: type | tuple[type, ...] = (dict, list),
    timeout: float = 30.0,
    body_retries: int = 3,
    body_backoff: float = 1.0,
) -> Any:
    """GET 一个 GitHub API 地址并返回解析后的 JSON。

    :param target: 完整 URL 或相对路径（如 ``repos/o/r/releases/latest``）
    :param expect: 期望的顶层 JSON 类型；不匹配即视为异常响应
    :param body_retries: 2xx 但响应体异常时的重试次数（含首次）
    :param body_backoff: 响应体异常时的退避基数（秒）

    :raises GitHubAPIError: 重试耗尽，或遇到不可重试的状态码
    """
    sess = session if session is not None else default_session()
    url = _absolute(target)
    last_error: GitHubAPIError | None = None

    for attempt in range(1, body_retries + 1):
        try:
            resp = sess.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last_error = GitHubAPIError(f"请求 {url} 失败: {exc}")
        else:
            status = resp.status_code
            body = resp.text or ""
            safe_body = _snippet(_redact(body))

            if status in FATAL_STATUS:
                raise GitHubAPIError(
                    f"{url} 返回 HTTP {status}，不重试",
                    status=status, snippet=safe_body,
                )

            if not 200 <= status < 300:
                last_error = GitHubAPIError(
                    f"{url} 返回 HTTP {status}",
                    status=status, snippet=safe_body,
                )
            else:
                data: Any = None
                if body.strip():
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        data = None
                if data is not None and isinstance(data, expect):
                    return data
                kind = "空响应" if data is None else f"非预期 JSON 类型 {type(data).__name__}"
                last_error = GitHubAPIError(
                    f"{url} 返回{kind}（期望 {expect}）",
                    status=status, snippet=safe_body,
                )

        if attempt < body_retries:
            delay = body_backoff * (2 ** (attempt - 1)) + random.uniform(0, body_backoff)
            log.warning(
                "GitHub API 响应异常，%.1fs 后重试（%d/%d）：%s",
                delay, attempt, body_retries, last_error,
            )
            time.sleep(delay)

    assert last_error is not None
    raise last_error
