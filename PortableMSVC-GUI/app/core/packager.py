"""打包模块：把生成的 MSVC 目录压缩为可分发的归档。

依次尝试：7z（超高压缩，优先）→ 系统 tar（zip）→ PowerShell Compress-Archive。
子进程支持取消与超时：取消/超时都会终止子进程，不留后台残留。
7z 场景通过 ``-bsp1`` 把压缩进度输出到 stdout，由 _run 实时解析并上报
on_progress(0-100)，解决打包大目录（数万文件、可能数十分钟）期间进度条停滞。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from .models import CancelledError

LogCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]
ProgressCallback = Callable[[int], None]

# 7z -bsp1 进度行：行首空白 + 百分比 + % + 空白，如 "  51% 132 files 4567890123 bytes"
_PROGRESS_RE = re.compile(r"^\s*(\d{1,3})%\s")


class PackError(Exception):
    """所有打包方式均失败。"""


def _creation_flags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _run(cmd: list[str], cwd: Path, log: Optional[LogCallback] = None,
         is_cancelled: Optional[CancelCheck] = None,
         timeout: int = 7200,
         on_progress: Optional[ProgressCallback] = None) -> bool:
    """运行子进程并等待结束；取消/超时均终止子进程并返回 False。

    输出写入临时文件（避免管道缓冲死锁），失败时把尾部输出透传给 log 便于诊断。
    on_progress(0-100)：可选，从 stdout 实时解析进度百分比（目前仅 7z -bsp1
    输出进度行，tar / PowerShell 无进度输出，不会触发回调）。
    """
    import threading

    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as out:
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(cwd), creationflags=_creation_flags(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
        except OSError as err:
            if log:
                log(f"无法启动 {cmd[0]}: {err}")
            return False

        def _drain():
            """实时读取 stdout：写入临时文件（供失败尾部透传）+ 解析进度行。"""
            buf = ""
            try:
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    out.write(chunk)
                    if on_progress:
                        buf += chunk
                        # 7z 进度行以 \r 覆盖同一行刷新，也兼容 \n 分隔
                        while "\r" in buf or "\n" in buf:
                            nl = buf.find("\n")
                            cr = buf.find("\r")
                            cut = (nl if cr < 0 else cr) + 1
                            line, buf = buf[:cut], buf[cut:]
                            m = _PROGRESS_RE.match(line)
                            if m:
                                pct = int(m.group(1))
                                if 0 <= pct <= 100:
                                    on_progress(pct)
                if on_progress and buf:
                    m = _PROGRESS_RE.match(buf)
                    if m:
                        pct = int(m.group(1))
                        if 0 <= pct <= 100:
                            on_progress(pct)
            except (ValueError, OSError):
                # 主线程已结束（正常返回/超时/取消）并关闭了 out 或管道，
                # 读取线程随进程退出，静默结束即可。
                pass

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()

        start = time.monotonic()
        while proc.poll() is None:
            if is_cancelled and is_cancelled():
                if log:
                    log("打包已被用户取消，正在终止子进程…")
                _terminate(proc)
                reader.join(timeout=2)
                raise CancelledError("打包已被用户取消")
            if time.monotonic() - start > timeout:
                if log:
                    log(f"打包超时（>{timeout}s），已强制终止 {cmd[0]}，尝试备用方案…")
                _terminate(proc)
                reader.join(timeout=2)
                return False
            time.sleep(0.2)
        reader.join(timeout=5)

        out.flush()
        out.seek(0)
        output = out.read().strip()
        ok = proc.returncode == 0
        if not ok and log:
            tail = "\n".join(output.splitlines()[-5:])
            log(f"{cmd[0]} 执行失败（exit={proc.returncode}）:\n{tail or '(无输出)'}")
        return ok


def _terminate(proc):
    """先温和 terminate，等待片刻后强制 kill，确保不留子进程。"""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def pack_dir(src: Path, archive_dir: Path, log: Optional[LogCallback] = None,
             level: int = 5, is_cancelled: Optional[CancelCheck] = None,
             on_progress: Optional[ProgressCallback] = None) -> Path:
    """压缩 src（目录本身，含同名根节点）到 archive_dir，返回归档路径。

    level 仅对 7z 有效（1-9，默认 5：压缩率与耗时均衡；
    mx9 对已压缩 DLL/海量小文件收益有限但耗时数十分钟）。
    支持 is_cancelled 回调：取消时抛 CancelledError（由流水线统一处理）。
    on_progress(0-100)：7z 场景实时上报压缩进度（tar / PowerShell 无进度输出）。
    """
    src = Path(src)
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    name = src.name

    # 1) 7z 压缩（级别可配，默认均衡档）
    if shutil.which("7z"):
        if log:
            log(f"检测到 7-Zip，压缩级别 mx{level}（打包大目录可能需要几分钟）...")
        archive = archive_dir / f"{name}.7z"
        # -bsp1：压缩进度输出到 stdout（供 _run 实时解析为 on_progress）
        if _run(["7z", "a", f"-mx{level}", "-bsp1", str(archive), name], archive_dir,
                log=log, is_cancelled=is_cancelled, on_progress=on_progress) \
                and archive.exists() and archive.stat().st_size > 0:
            if log:
                log(f"7z 打包完成: {archive.name}")
            return archive
        archive.unlink(missing_ok=True)
        if log:
            log("7z 打包失败，尝试备用方案...")

    # 2) 系统 tar（自动按扩展名选 zip 格式）
    if shutil.which("tar"):
        if log:
            log("使用系统 tar.exe 打包为 zip...")
        archive = archive_dir / f"{name}.zip"
        if _run(["tar", "-a", "-c", "-f", str(archive), name], archive_dir,
                log=log, is_cancelled=is_cancelled) \
                and archive.exists() and archive.stat().st_size > 0:
            if log:
                log(f"tar 打包完成: {archive.name}")
            return archive
        archive.unlink(missing_ok=True)
        if log:
            log("tar 打包失败，尝试 PowerShell 方案...")

    # 3) PowerShell Compress-Archive
    if sys.platform == "win32" and shutil.which("powershell"):
        if log:
            log("使用 PowerShell Compress-Archive 打包（较慢）...")
        archive = archive_dir / f"{name}.zip"
        # 路径作为独立 argv 传入（param 绑定），避免字符串拼接时
        # 目录名含引号/特殊字符导致解析失败或注入。
        ps_script = ("& { param($src, $dst) "
                     "Compress-Archive -Path $src -DestinationPath $dst -Force }")
        cmd = ["powershell", "-NoProfile", "-Command", ps_script,
               str(name), str(archive)]
        if _run(cmd, archive_dir, log=log, is_cancelled=is_cancelled) \
                and archive.exists() and archive.stat().st_size > 0:
            if log:
                log(f"PowerShell 打包完成: {archive.name}")
            return archive
        archive.unlink(missing_ok=True)

    raise PackError("所有打包方式均失败，已保留原始 MSVC 文件夹供检查")
