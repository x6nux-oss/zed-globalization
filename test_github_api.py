#!/usr/bin/env python3
"""GitHub API 重试 / 空响应防御的回归测试

直接运行: python3 test_github_api.py
也可用 pytest 收集: pytest test_github_api.py
"""

import contextlib
import http.server
import logging
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from zedl10n.github_api import (  # noqa: E402
    GitHubAPIError, build_session, request_json,
)

_FAST = {"total": 5, "backoff_factor": 0.001}


class _MockGitHub:
    """按脚本依次返回响应的本地 HTTP 服务；脚本最后一项会被重复。"""

    def __init__(self, script):
        self.script = script
        self.requests = []
        self.headers = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append(self.path)
                outer.headers.append({k: v for k, v in self.headers.items()})
                idx = min(len(outer.requests) - 1, len(outer.script) - 1)
                status, body, headers = outer.script[idx]
                payload = body.encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/test"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _session():
    session = build_session(**_FAST)
    # 本地 mock 不应经过任何代理
    session.trust_env = False
    return session


@contextlib.contextmanager
def _mock(script):
    mock = _MockGitHub(script)
    session = _session()
    try:
        yield mock, session
    finally:
        session.close()
        mock.close()


def test_429_then_200_retries_and_returns_body() -> None:
    """429（带 Retry-After）→ 200：应重试后成功。"""
    script = [
        (429, '{"message":"API rate limit exceeded"}', {"Retry-After": "0"}),
        (200, '{"tag_name":"v1.2.3"}', {}),
    ]
    with _mock(script) as (mock, session):
        data = request_json(mock.url, session=session)
        assert data == {"tag_name": "v1.2.3"}, data
        assert len(mock.requests) == 2, mock.requests


def test_5xx_then_200_retries_and_returns_body() -> None:
    """503 → 502 → 200：应重试到成功。"""
    script = [
        (503, '{"message":"unavailable"}', {}),
        (502, '{"message":"bad gateway"}', {}),
        (200, '{"tag_name":"v9.9.9"}', {}),
    ]
    with _mock(script) as (mock, session):
        data = request_json(mock.url, session=session)
        assert data == {"tag_name": "v9.9.9"}, data
        assert len(mock.requests) == 3, mock.requests


def test_null_body_raises_with_status_and_snippet() -> None:
    """2xx 但 body 为 null：重试耗尽后应抛错，而不是把 None 交给调用方。"""
    with _mock([(200, "null", {})]) as (mock, session):
        try:
            request_json(mock.url, session=session, body_backoff=0.001)
        except GitHubAPIError as exc:
            assert exc.status == 200, exc.status
            assert "null" in exc.snippet, exc.snippet
        else:
            raise AssertionError("null 响应应抛 GitHubAPIError")
        assert len(mock.requests) == 3, mock.requests


def test_empty_body_raises() -> None:
    """2xx 但 body 为空：应抛 GitHubAPIError。"""
    with _mock([(200, "", {})]) as (mock, session):
        try:
            request_json(mock.url, session=session, body_backoff=0.001)
        except GitHubAPIError as exc:
            assert "空响应" in str(exc), str(exc)
        else:
            raise AssertionError("空响应应抛 GitHubAPIError")
        assert len(mock.requests) == 3, mock.requests


def test_unexpected_shape_raises() -> None:
    """期望数组却拿到错误对象：应抛 GitHubAPIError，而不是迭代 null。"""
    with _mock([(200, '{"message":"Not Found"}', {})]) as (mock, session):
        try:
            request_json(
                mock.url, session=session, expect=list, body_backoff=0.001,
            )
        except GitHubAPIError as exc:
            assert "非预期" in str(exc), str(exc)
        else:
            raise AssertionError("结构不符应抛 GitHubAPIError")


def test_404_fails_fast_without_retry() -> None:
    """404 不可重试：只发一次请求，立刻抛错并带状态码。"""
    with _mock([(404, '{"message":"Not Found"}', {})]) as (mock, session):
        try:
            request_json(mock.url, session=session)
        except GitHubAPIError as exc:
            assert exc.status == 404, exc.status
        else:
            raise AssertionError("404 应抛 GitHubAPIError")
        assert len(mock.requests) == 1, mock.requests


def test_authorization_header_is_sent() -> None:
    """配置了 token 时应走 Authorization: Bearer 认证。"""
    os.environ["GITHUB_TOKEN"] = "ghs_UNITTEST_TOKEN"
    try:
        with _mock([(200, '{"ok":true}', {})]) as (mock, session):
            request_json(mock.url, session=session)
            auth = mock.headers[0].get("Authorization")
            assert auth == "Bearer ghs_UNITTEST_TOKEN", auth
    finally:
        os.environ.pop("GITHUB_TOKEN", None)


def test_error_message_does_not_leak_token() -> None:
    """错误信息不得包含 token。"""
    os.environ["GITHUB_TOKEN"] = "ghs_UNITTEST_TOKEN"
    try:
        with _mock([(500, '{"message":"boom"}', {})]) as (mock, session):
            try:
                request_json(mock.url, session=session)
            except GitHubAPIError as exc:
                assert "ghs_UNITTEST_TOKEN" not in str(exc), str(exc)
                assert "ghs_UNITTEST_TOKEN" not in exc.snippet, exc.snippet
            else:
                raise AssertionError("持续 500 应抛 GitHubAPIError")
    finally:
        os.environ.pop("GITHUB_TOKEN", None)


def test_fetch_release_notes_degrades_to_empty_string() -> None:
    """取不到 Release Notes 时返回空串（保持原有降级语义）。"""
    from zedl10n import release_notes
    import zedl10n.github_api as api

    def _boom(target, **_kwargs):
        raise api.GitHubAPIError("boom", status=500, snippet="boom")

    real = api.request_json
    api.request_json = _boom
    try:
        assert release_notes.fetch_release_notes("v0.0.0") == ""
    finally:
        api.request_json = real


def test_fetch_release_notes_returns_body() -> None:
    """正常响应时返回 release body。"""
    from zedl10n import release_notes
    import zedl10n.github_api as api

    real = api.request_json
    api.request_json = lambda target, **_kw: {"body": "## Changes\n- fixed"}
    try:
        assert release_notes.fetch_release_notes("v1.0.0") == "## Changes\n- fixed"
    finally:
        api.request_json = real


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
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERR  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
