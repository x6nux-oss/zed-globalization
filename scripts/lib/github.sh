#!/usr/bin/env bash
# shellcheck shell=bash
#
# scripts/lib/github.sh —— GitHub REST API 统一访问封装
#
# 用途：所有对 api.github.com 的调用都走这里，统一获得
#   1. 认证（Authorization: Bearer $GH_TOKEN），把限额从 60 次/小时提到 5000 次/小时
#   2. 指数退避重试（2s → 4s → 8s → 16s + 随机 jitter），覆盖 403/429/5xx/超时/连接失败
#   3. 限流识别：优先遵守 Retry-After 与 X-RateLimit-Reset
#   4. 失败时输出 HTTP 状态与响应片段（token 已脱敏），绝不打 token
#
# 配套的 require_json 用来在 jq 取值/迭代之前校验响应结构，
# 避免上游返回错误 JSON 时出现 `jq: Cannot iterate over null`。
#
# 用法：
#   source scripts/lib/github.sh
#   if json=$(gh_api "repos/o/r/releases/tags/v1.0.0"); then
#     require_json "$json" '.assets | type == "array"' "release 响应" || exit 1
#     url=$(printf '%s' "$json" | jq -r '(.assets // [])[] | .browser_download_url')
#   fi
#
# 环境变量（均可选）：
#   GH_TOKEN / GITHUB_TOKEN  认证 token；两者都为空时降级为匿名请求（限额很低）
#   GH_API_MAX_ATTEMPTS      最大尝试次数，默认 5
#   GH_API_BASE_DELAY        退避基数（秒），默认 2 → 2/4/8/16
#   GH_API_MAX_WAIT          单次最长等待（秒），默认 60
#   GH_API_TIMEOUT           单次请求超时（秒），默认 60
#   GH_API_BASE_URL          仅测试用：覆盖 api.github.com（默认不设置）
#
# 返回码：
#   0  HTTP 2xx，响应体已写到 stdout
#   4  HTTP 404（资源不存在），响应体仍写到 stdout，便于调用方自行判断
#   1  其它失败（已重试耗尽或不可重试的 4xx），不输出响应体
#
# 兼容性：不使用 bash 4+ 特性，可在 macOS 自带 bash 3.2 下加载。

GH_API_MAX_ATTEMPTS="${GH_API_MAX_ATTEMPTS:-5}"
GH_API_BASE_DELAY="${GH_API_BASE_DELAY:-2}"
GH_API_MAX_WAIT="${GH_API_MAX_WAIT:-60}"
GH_API_TIMEOUT="${GH_API_TIMEOUT:-60}"

_gh_api_token() {
  printf '%s' "${GH_TOKEN:-${GITHUB_TOKEN:-}}"
}

# 相对路径（repos/o/r/...）自动补全为 api.github.com 绝对地址。
# GH_API_BASE_URL 仅用于本地测试时指向 mock 服务，生产环境不要设置。
_gh_api_url() {
  case "$1" in
    http://*|https://*) printf '%s' "$1" ;;
    *) printf '%s/%s' "${GH_API_BASE_URL:-https://api.github.com}" "${1#/}" ;;
  esac
}

# 从 -D 写出的响应头里取某个头的值（大小写不敏感，可移植 awk）
_gh_api_header() {
  [ -s "$1" ] || return 0
  awk -v name="$(printf '%s' "$2" | tr 'A-Z' 'a-z')" '
    { line = tolower($0) }
    index(line, name ":") == 1 {
      v = $0; sub(/^[^:]*:[ ]*/, "", v); sub(/\r$/, "", v); print v
    }' "$1" | tail -1
}

# 响应体压成一行并截断，用于错误信息
_gh_api_snippet() {
  [ -s "$1" ] || return 0
  tr '\n\r\t' '   ' < "$1" | cut -c1-500
}

# 把 token 从任意文本里抹掉（纵深防御：请求头本来就不会进日志）
_gh_api_redact() {
  local _token
  _token=$(_gh_api_token)
  if [ -n "$_token" ]; then
    sed "s|${_token}|[REDACTED]|g"
  else
    cat
  fi
}

# 计算下一次重试前要等多久：Retry-After > X-RateLimit-Reset > 指数退避
_gh_api_wait_seconds() {
  local _hdr="$1" _attempt="$2" _wait="" _retry_after _reset _now

  _retry_after=$(_gh_api_header "$_hdr" "retry-after")
  if [ -n "$_retry_after" ] && [ "$_retry_after" -eq "$_retry_after" ] 2>/dev/null; then
    _wait="$_retry_after"
  fi

  if [ -z "$_wait" ]; then
    _reset=$(_gh_api_header "$_hdr" "x-ratelimit-reset")
    if [ -n "$_reset" ] && [ "$_reset" -eq "$_reset" ] 2>/dev/null; then
      _now=$(date +%s)
      _wait=$(( _reset - _now ))
      if [ "$_wait" -lt 0 ]; then _wait=0; fi
    fi
  fi

  if [ -z "$_wait" ]; then
    # 指数退避 + jitter：2/4/8/16 秒上下浮动
    _wait=$(( GH_API_BASE_DELAY * (1 << (_attempt - 1)) ))
    if [ "$_wait" -gt 0 ]; then
      _wait=$(( _wait + RANDOM % (_wait + 1) ))
    fi
  fi

  if [ "$_wait" -gt "$GH_API_MAX_WAIT" ]; then _wait="$GH_API_MAX_WAIT"; fi
  if [ "$_wait" -lt 0 ]; then _wait=0; fi
  printf '%s' "$_wait"
  return 0
}

# gh_api [-X METHOD] [-H 'Header: value'] [-d RAW_BODY] <url-or-path>
#
# GET 请求示例：
#   json=$(gh_api "repos/${GH_REPO}/releases/tags/${tag}")
gh_api() {
  local _method="GET" _url="" _data="" _target=""
  local _body_file _hdr_file _err_file _status _rc _attempt _wait _h
  local -a _extra_headers=()

  while [ $# -gt 0 ]; do
    case "$1" in
      -X|--method) _method="$2"; shift 2 ;;
      -H|--header) _extra_headers[${#_extra_headers[@]}]="$2"; shift 2 ;;
      -d|--data)   _data="$2"; shift 2 ;;
      --) shift; if [ $# -gt 0 ]; then _target="$1"; shift; fi ;;
      -*) echo "gh_api: 未知选项 $1" >&2; return 1 ;;
      *)  _target="$1"; shift ;;
    esac
  done

  if [ -z "$_target" ]; then
    echo "gh_api: 缺少 URL" >&2
    return 1
  fi
  _url=$(_gh_api_url "$_target")

  _body_file=$(mktemp) || return 1
  _hdr_file=$(mktemp) || { rm -f "$_body_file"; return 1; }
  _err_file=$(mktemp) || { rm -f "$_body_file" "$_hdr_file"; return 1; }

  _attempt=1
  while [ "$_attempt" -le "$GH_API_MAX_ATTEMPTS" ]; do
    : > "$_body_file"; : > "$_hdr_file"; : > "$_err_file"

    local -a _args=(
      -sS --connect-timeout 10 --max-time "$GH_API_TIMEOUT"
      -D "$_hdr_file" -o "$_body_file" -w '%{http_code}'
      -H 'Accept: application/vnd.github+json'
      -H 'X-GitHub-Api-Version: 2022-11-28'
    )
    if [ "$_method" != "GET" ]; then
      _args[${#_args[@]}]="-X"
      _args[${#_args[@]}]="$_method"
    fi
    if [ -n "$_data" ]; then
      _args[${#_args[@]}]="--data-binary"
      _args[${#_args[@]}]="$_data"
    fi

    local _token
    _token=$(_gh_api_token)
    if [ -n "$_token" ]; then
      _args[${#_args[@]}]="-H"
      _args[${#_args[@]}]="Authorization: Bearer ${_token}"
    fi
    for _h in ${_extra_headers[@]+"${_extra_headers[@]}"}; do
      if [ -n "$_h" ]; then
        _args[${#_args[@]}]="-H"
        _args[${#_args[@]}]="$_h"
      fi
    done

    _rc=0
    _status=$(curl "${_args[@]}" "$_url" 2>"$_err_file") || _rc=$?
    # 连接失败时 curl 仍会打印 000，但不要依赖这一点
    if [ "$_rc" -ne 0 ]; then
      _status="000"
    fi

    case "$_status" in
      2??)
        cat "$_body_file"
        rm -f "$_body_file" "$_hdr_file" "$_err_file"
        return 0
        ;;
      404)
        # 资源不存在：不重试，交给调用方判断（轮询等待场景里很常见）
        echo "::warning::GitHub API 404: ${_method} ${_url}" >&2
        cat "$_body_file"
        rm -f "$_body_file" "$_hdr_file" "$_err_file"
        return 4
        ;;
      000|403|408|429|5??)
        _wait=$(_gh_api_wait_seconds "$_hdr_file" "$_attempt")
        if [ "$_attempt" -ge "$GH_API_MAX_ATTEMPTS" ]; then
          break
        fi
        if [ "$_status" = "403" ] || [ "$_status" = "429" ]; then
          echo "::warning::GitHub API 限流（HTTP ${_status}），${_wait}s 后重试（${_attempt}/${GH_API_MAX_ATTEMPTS}）：${_method} ${_url}" >&2
        elif [ "$_status" = "000" ]; then
          echo "::warning::GitHub API 请求失败（网络错误），${_wait}s 后重试（${_attempt}/${GH_API_MAX_ATTEMPTS}）：${_method} ${_url}" >&2
        else
          echo "::warning::GitHub API 返回 HTTP ${_status}，${_wait}s 后重试（${_attempt}/${GH_API_MAX_ATTEMPTS}）：${_method} ${_url}" >&2
        fi
        sleep "$_wait"
        _attempt=$(( _attempt + 1 ))
        continue
        ;;
      *)
        # 其它 4xx：重试也不会变好，直接明确失败
        break
        ;;
    esac
  done

  echo "::error::GitHub API 请求失败（HTTP ${_status}，已尝试 ${_attempt} 次）：${_method} ${_url}" >&2
  if [ -s "$_err_file" ]; then
    echo "::error::curl 错误: $(tr '\n\r' '  ' < "$_err_file" | cut -c1-300 | _gh_api_redact)" >&2
  fi
  if [ -s "$_body_file" ]; then
    echo "::error::响应片段: $(_gh_api_snippet "$_body_file" | _gh_api_redact)" >&2
  fi
  rm -f "$_body_file" "$_hdr_file" "$_err_file"
  return 1
}

# require_json <json> <jq 布尔表达式> [描述]
#
# 在 jq 迭代数组 / 取字段之前校验响应结构。不满足时输出「状态码无关」的
# 清晰错误与响应片段，并返回非零。
require_json() {
  local _json="$1" _expr="$2" _what="${3:-响应}"
  if [ -z "$_json" ]; then
    echo "::error::${_what}为空（期望满足: ${_expr}）" >&2
    return 1
  fi
  if ! printf '%s' "$_json" | jq -e "$_expr" >/dev/null 2>&1; then
    echo "::error::${_what}结构不符合预期（需要: ${_expr}）" >&2
    echo "::error::响应片段: $(printf '%s' "$_json" | tr '\n\r\t' '   ' | cut -c1-500)" >&2
    return 1
  fi
  return 0
}
