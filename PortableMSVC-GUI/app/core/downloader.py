"""带缓存、断点续传、自动重试与 SHA256 校验的文件下载模块。

设计要点：
- 流式写入 ``<dest>.part`` 临时文件（边写边算哈希），避免大文件全量进内存；
- 下载中断（网络错误 / 校验失败）后可基于已下载部分继续（HTTP Range）；
- URLError 自动指数退避重试；校验失败则丢弃临时文件重头再来；
- 全部完成并通过 SHA256 校验后才原子改名为正式文件，杜绝半成品缓存。
"""
from __future__ import annotations

import hashlib
import http.client
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .common import USER_AGENT, sanitize_url
from .models import CancelledError

ProgressCallback = Callable[[float], None]      # 参数: 当前文件下载进度 0-100
CancelCheck = Callable[[], bool]

CHUNK_SIZE = 1 << 20                            # 1 MiB
MAX_RETRIES = 3                                 # 网络错误自动重试次数
RETRY_BASE_DELAY = 1.0                          # 首次重试前等待秒数（指数退避）


class DownloadError(Exception):
    """下载失败（网络错误 / 校验失败）。"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK_SIZE)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _request(url: str, start: int = 0):
    headers = {"User-Agent": USER_AGENT}
    if start > 0:
        headers["Range"] = f"bytes={start}-"
    # 清单 URL 可能含未编码空格等非法字符，发请求前统一转义（已有 %XX 不受影响）
    return urllib.request.Request(sanitize_url(url), headers=headers)


def _can_resume(res, start: int) -> bool:
    """服务器是否接受了 Range 续传请求（206）。"""
    if start <= 0:
        return True
    return getattr(res, "status", 200) == 206


def download_to_file(
    url: str,
    dest: Path,
    sha256: str,
    ssl_context=None,
    on_progress: Optional[ProgressCallback] = None,
    is_cancelled: Optional[CancelCheck] = None,
) -> Path:
    """下载 url 到 dest，校验 SHA256，返回落盘文件路径。

    - dest 已存在且哈希一致 → 直接复用（返回路径）；
    - 下载使用 ``<dest>.part`` 临时文件，支持断点续传与自动重试；
    - 失败（网络/校验）会抛出 DownloadError；用户取消抛 CancelledError。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    sha256 = sha256.lower()

    # 缓存命中：文件已存在且哈希一致
    if dest.exists():
        try:
            if _sha256_file(dest) == sha256:
                return dest
        except OSError:
            pass

    start = 0
    if part.exists():
        # 上次中断的进度：从临时文件已下载字节数继续
        try:
            start = part.stat().st_size
        except OSError:
            start = 0

    for attempt in range(MAX_RETRIES + 1):
        if is_cancelled and is_cancelled():
            part.unlink(missing_ok=True)
            raise CancelledError("下载已被用户取消")

        try:
            req = _request(url, start)
            with urllib.request.urlopen(req, context=ssl_context, timeout=60.0) as res:
                resumed = _can_resume(res, start)
                if not resumed:
                    # 服务器不支持 Range：从头开始
                    start = 0
                    part.unlink(missing_ok=True)

                # 总大小：优先取 Content-Range（206 时 Content-Length 只是本次长度）
                total = _total_size(res, start)
                mode = "ab" if (resumed and start > 0) else "wb"
                h = hashlib.sha256()
                if mode == "ab":
                    # 已有部分也要参与最终哈希，先补算历史字节
                    _update_hash_from_file(h, part)

                size = start
                with open(part, mode) as f:
                    while True:
                        if is_cancelled and is_cancelled():
                            f.flush()
                            raise CancelledError("下载已被用户取消")
                        block = res.read(CHUNK_SIZE)
                        if not block:
                            break
                        f.write(block)
                        h.update(block)
                        size += len(block)
                        if on_progress:
                            if total:
                                on_progress(size * 100.0 / total)
                            else:
                                # 服务器未提供 Content-Length / Content-Range：
                                # 用渐进式估算 y = 99*(1 - 0.985^blocks) 单调逼近 99%。
                                # 相比固定"每 MiB 0.5%"（小文件几乎不动、大文件过早卡 99%），
                                # 该曲线保证进度持续可见变化且不会跳变。
                                blocks = size / (1 << 20)
                                on_progress(min(99.0, 99.0 * (1 - 0.985 ** blocks)))

            # 校验整体哈希
            digest = h.hexdigest()
            if digest != sha256:
                part.unlink(missing_ok=True)
                if attempt < MAX_RETRIES:
                    # 数据损坏（网络传输问题）：丢弃临时文件，从头重新下载
                    # （损坏段无法续传，必须整文件重来）
                    start = 0
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                raise DownloadError(
                    f"SHA256 校验失败: {dest.name}（期望 {sha256}，实际 {digest}）"
                )
            part.replace(dest)
            return dest
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError) as err:
            # 416（Range Not Satisfiable）：服务器认为续传区间越界，通常是
            # .part 已在上一轮下载完整但未及改名（进程在 part.replace(dest)
            # 前被中断/崩溃）。此时直接校验 part 哈希：一致则视为完成，
            # 否则丢弃 part 从头下载；该分支不消耗网络错误的重试次数。
            if isinstance(err, urllib.error.HTTPError) and err.code == 416:
                try:
                    if part.exists() and _sha256_file(part) == sha256:
                        part.replace(dest)
                        return dest
                except OSError:
                    pass
                part.unlink(missing_ok=True)
                start = 0
                continue
            # 非法 URL：InvalidURL 属 HTTPException，但并非临时网络故障，
            # 重试无意义，直接报明确错误（sanitize_url 后正常不应再触发）。
            if isinstance(err, http.client.InvalidURL):
                part.unlink(missing_ok=True)
                raise DownloadError(f"URL 非法（无法转义，请检查清单）: {url} ({err})") from err
            # 网络错误：连接中断(RemoteDisconnected)等未必是 URLError，一并重试
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                start = _safe_part_size(part)
                continue
            part.unlink(missing_ok=True)
            raise DownloadError(f"下载失败: {url} ({err})") from err
        except CancelledError:
            part.unlink(missing_ok=True)
            raise


def _total_size(res, start: int) -> int:
    """估算资源总大小；无法获取时返回 0（此时不做百分比进度）。"""
    cr = res.headers.get("Content-Range")  # 形如 "bytes 100-999/10000"
    if cr and "/" in cr:
        try:
            return int(cr.rsplit("/", 1)[1])
        except ValueError:
            pass
    length = res.headers.get("Content-Length")
    if length is not None:
        try:
            return start + int(length)
        except ValueError:
            pass
    return 0


def _safe_part_size(part: Path) -> int:
    try:
        return part.stat().st_size
    except OSError:
        return 0


def _update_hash_from_file(h: hashlib._Hash, path: Path):
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK_SIZE)
            if not block:
                break
            h.update(block)
