"""运行环境自检：平台、msiexec、7z、磁盘空间、网络连通性。"""
from __future__ import annotations

import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .common import USER_AGENT
from .models import CancelledError, EnvCheck

# MSVC + Windows SDK 全量下载所需的最小可用空间（保守估计 8GB）
MIN_FREE_BYTES = 8 * 1024 ** 3
WARN_FREE_BYTES = 12 * 1024 ** 3

CHECK_URL = "https://aka.ms/vs/stable/channel"


def disk_free_bytes(path: Path) -> int | None:
    """返回路径所在磁盘剩余字节数；不可用返回 None。"""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def disk_free_gb(path: Path) -> float | None:
    """返回路径所在磁盘剩余空间（GB）；不可用返回 None。"""
    free = disk_free_bytes(path)
    return None if free is None else free / 1024 ** 3


def run_checks(output_dir: Path,
               is_cancelled: Optional[Callable[[], bool]] = None) -> list[EnvCheck]:
    """执行全部环境检查，返回检查项列表。

    is_cancelled：可选，检查项之间检查取消状态（抛 CancelledError）。
    """
    checks: list[EnvCheck] = []

    def _check_cancel():
        if is_cancelled and is_cancelled():
            raise CancelledError("环境检测已取消")

    # 平台
    _check_cancel()
    if sys.platform == "win32":
        checks.append(EnvCheck("操作系统", "info", "Windows 环境，支持完整流程"))
    else:
        checks.append(EnvCheck("操作系统", "error",
                               f"当前为 {sys.platform}，Windows SDK 解包将依赖 msitextract，建议在 Windows 上运行"))

    # msiexec（Windows SDK 解包必需）
    _check_cancel()
    if sys.platform == "win32":
        if shutil.which("msiexec"):
            checks.append(EnvCheck("msiexec", "info", "可用于 Windows SDK MSI 解包"))
        else:
            checks.append(EnvCheck("msiexec", "error", "未找到 msiexec，无法解包 Windows SDK"))

    # 7z（可选，仅影响打包压缩率）
    _check_cancel()
    if shutil.which("7z"):
        checks.append(EnvCheck("7-Zip", "info", "已安装，将使用超高压缩率打包"))
    else:
        checks.append(EnvCheck("7-Zip", "warn", "未安装，将回退到 tar/zip 打包（压缩率较低）"))

    # 磁盘空间（直接用字节比较，避免浮点换算在边界值上误判）
    _check_cancel()
    free = disk_free_bytes(output_dir)
    if free is None:
        checks.append(EnvCheck("磁盘空间", "warn", f"无法读取磁盘信息: {output_dir}"))
    elif free < MIN_FREE_BYTES:
        checks.append(EnvCheck("磁盘空间", "error",
                               f"{output_dir} 所在盘剩余 {free / 1024 ** 3:.1f} GB，低于建议的 8 GB"))
    elif free < WARN_FREE_BYTES:
        checks.append(EnvCheck("磁盘空间", "warn",
                               f"{output_dir} 所在盘剩余 {free / 1024 ** 3:.1f} GB，工具链较大建议预留 12 GB"))
    else:
        checks.append(EnvCheck("磁盘空间", "info",
                               f"{output_dir} 所在盘剩余 {free / 1024 ** 3:.1f} GB，空间充足"))

    # 网络连通性
    _check_cancel()
    try:
        req = urllib.request.Request(CHECK_URL, method="HEAD",
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=5):
            checks.append(EnvCheck("网络连通", "info", f"可访问 {CHECK_URL}"))
    except Exception:
        checks.append(EnvCheck("网络连通", "error",
                               f"无法访问微软清单服务器，请检查网络（下载过程可能失败）"))

    return checks


def has_blocking_errors(checks: list[EnvCheck]) -> bool:
    return any(c.level == "error" for c in checks)
