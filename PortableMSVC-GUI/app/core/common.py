"""core 层公共小工具，供各模块复用，避免重复定义。

- USER_AGENT    所有对外 HTTP 请求的统一 User-Agent；
- version_key   点分版本号 → 可比较 tuple（用于版本排序）；
- sanitize_url  URL 非法字符转义（percent-encoding），发请求前统一调用。
"""
from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

USER_AGENT = "portable-msvc-gui/1.0"

# RFC 3986 中 path / query 允许保留的字符（unreserved 由 quote 自动保留）。
# 关键：把 "%" 纳入 safe，避免对已有的 %XX 二次编码（%20 → %2520）。
_PATH_SAFE = "/:@%!$&'()*+,;="
_QUERY_SAFE = "/?:@%!$&'()*+,;="


def sanitize_url(url: str) -> str:
    """把 URL 中的非法字符转义为 percent-encoding，保留已有 %XX 不二次编码。

    微软 VS 清单中部分下载链接的路径含未编码空格（如 "/w kits2/"），
    Python 的 http.client 会以 InvalidURL 直接拒绝。此函数按组件拆分后，
    仅对 path 与 query 做 RFC 3986 合规编码，覆盖空格、非 ASCII 及其它非法字符，
    结构（scheme / netloc / fragment）保持不变；合法 URL 输入时输出等于输入（幂等）。
    """
    parts = urlsplit(url)
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        quote(parts.path, safe=_PATH_SAFE),
        quote(parts.query, safe=_QUERY_SAFE),
        parts.fragment,
    ))


def version_key(version: str) -> tuple:
    """把点分版本号转成可比较的 tuple，避免字符串排序错误（如 14.9 > 14.41）。

    非数字段按 0 处理以保证健壮性（如 "14.42.0-beta"）。
    """
    return tuple(int(seg) if seg.isdigit() else 0 for seg in version.split("."))
