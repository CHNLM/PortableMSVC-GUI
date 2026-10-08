"""解包模块：zip 包、Windows SDK 的 .msi / .cab。

Windows 上通过 msiexec 管理式安装（/a）把 MSI 展开到目标目录；
非 Windows 平台退化为 msiextract（需安装 msitools）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable, Iterator, Optional

from .models import CancelledError

LogCallback = Callable[[str], None]

# 合法 cab 文件名：仅字母数字、下划线、点、连字符，且必须以 .cab 结尾
_CAB_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]+\.cab$", re.IGNORECASE)

# zip 炸弹防护阈值：微软官方包远低于此，防御恶意/损坏包写满磁盘
MAX_ZIP_ENTRIES = 200_000          # 单包条目数上限
MAX_ZIP_EXPANDED_BYTES = 64 << 30  # 解压后总大小上限（64 GiB）


def extract_zip_contents(zip_path: Path, dest: Path, prefix: str = "Contents/",
                         is_cancelled: Optional[Callable[[], bool]] = None):
    """把 zip 包中 prefix 前缀下的文件解压到 dest（去掉前缀），流式写出。

    防御 Zip Slip：解压目标必须位于 dest 之内，否则跳过并告警，
    防止恶意条目（如 ``Contents/../../evil.txt``）逃逸到目标目录外。
    支持 is_cancelled 回调：解压中途取消时抛 CancelledError。
    前置防御：非 zip 文件（如误下载的 MSI/CAB）直接报错；
    条目数或解压总量超阈值（zip 炸弹）中止。
    """
    zip_path = Path(zip_path)
    if not zipfile.is_zipfile(zip_path):
        raise RuntimeError(f"下载的文件不是有效的 zip 包: {zip_path.name}")
    dest = Path(dest).resolve()
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        total_expanded = sum(i.file_size for i in infos)
        if len(infos) > MAX_ZIP_ENTRIES or total_expanded > MAX_ZIP_EXPANDED_BYTES:
            raise RuntimeError(
                f"zip 包内容异常过大（{len(infos)} 条目 / "
                f"{total_expanded / (1 << 30):.1f} GiB），已中止解包: {zip_path.name}"
            )
        for info in infos:
            name = info.filename
            if is_cancelled and is_cancelled():
                raise CancelledError("解包已被用户取消")
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if rel.endswith("/"):
                # 目录条目：仅确保目录存在，不写 0 字节空文件
                (dest / rel).mkdir(parents=True, exist_ok=True)
                continue
            out = (dest / rel).resolve()
            if not out.is_relative_to(dest):
                print(f"!! 跳过越界压缩包条目: {name}", file=sys.stderr)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, open(out, "wb") as f:
                shutil.copyfileobj(src, f, length=1 << 20)


def _scan_cab_names(data: bytes) -> Iterator[str]:
    """在字节流中同时扫描 ASCII 与 UTF-16LE 两种 ".cab" 字形，尽力还原文件名。

    MSI 数据库字符串表通常以 UTF-16LE 存储（如 ".\0c\0a\0b\0"），但二进制中
    也可能出现纯 ASCII 的路径引用，因此两种都要匹配。命中后向前回扫到
    可打印 ASCII 边界，取出尽可能完整的文件名，再做清洗：
    - 丢弃所有非可打印/非文件名字符（含 UTF-16 的 \\0 填充）；
    - 保留最后一段路径（去掉前缀目录）；
    - 仅接受形如 ``xxx.cab`` 的合理名字，过滤掉二进制噪声命中。
    """
    marker_ascii = b".cab"
    marker_utf16 = b".\x00c\x00a\x00b\x00"
    for marker in (marker_ascii, marker_utf16):
        step = len(marker)
        index = 0
        while True:
            index = data.find(marker, index)
            if index < 0:
                break
            start = index
            # 回扫：ASCII 按字节、UTF-16 按双字节单元，取到可打印边界
            if marker is marker_utf16:
                while start >= 2 and 0x20 <= data[start - 2] < 0x7f and data[start - 1] == 0:
                    start -= 2
            else:
                while start > 0 and 0x20 <= data[start - 1] < 0x7f:
                    start -= 1
            raw = data[start:index + len(marker)]
            name = raw.decode("ascii", errors="ignore").replace("\x00", "")
            # 去掉路径前缀，只保留文件名部分
            name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if _CAB_NAME_RE.match(name) and len(name) <= 260:
                yield name
            index += step


def get_msi_cabs(msi_path: Path):
    """从 MSI 二进制中提取引用的 .cab 文件名（流式扫描，尽力而为）。

    注意：本函数只是辅助手段，结果可能不完整或含噪声（MSI 字符串表编码
    多样）。调用方应以**清单中的 .cab payload 枚举**为权威来源，本函数
    仅用于交叉校验与日志。
    """
    overlap = 128  # 窗口重叠：需覆盖最长 marker（UTF-16 8 字节）加回扫余量
    tail = b""
    with open(msi_path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            window = tail + chunk
            yield from _scan_cab_names(window)
            tail = window[-overlap:]


def _creation_flags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _run_process(cmd: list[str], log: Optional[LogCallback] = None,
                 is_cancelled: Optional[Callable[[], bool]] = None,
                 timeout: float = 7200) -> tuple[int, str]:
    """运行子进程并等待结束，支持取消与超时（超时/取消均会终止子进程）。

    输出写入临时文件避免管道阻塞（msiexec/7z 输出可能超过管道缓冲）；
    返回 (returncode, 输出内容)。取消时抛 CancelledError。
    """
    import tempfile
    import time

    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as out:
        proc = subprocess.Popen(
            cmd, creationflags=_creation_flags(),
            stdout=out, stderr=subprocess.STDOUT, text=True,
        )
        start = time.monotonic()
        while proc.poll() is None:
            if is_cancelled and is_cancelled():
                _terminate(proc)
                raise CancelledError("操作已取消")
            if timeout > 0 and time.monotonic() - start > timeout:
                if log:
                    log(f"子进程执行超时（>{timeout:.0f}s），已强制终止: {cmd[0]}")
                _terminate(proc)
                break
            time.sleep(0.2)
        out.flush()
        out.seek(0)
        output = out.read()
    return proc.returncode or 0, output


def _terminate(proc):
    """先温和 terminate，等待片刻后强制 kill，确保不留子进程。"""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _flatten_program_files(root: Path):
    """防御性整理：若解包产物落在 root/Program Files/Windows Kits，则提升到 root。

    分两遍处理：先建目录骨架、再移动文件。相比一次性 list(rglob) 收集
    数万条目到内存，迭代式更省内存；同时规避"遍历中修改目录树导致
    rglob 迭代器漏项"的问题（先只 mkdir，不移动原树，第二遍文件仍在原处）。
    """
    pf = root / "Program Files"
    kits = pf / "Windows Kits"
    if not kits.exists():
        return
    # 第一遍：在目标位置建立目录骨架
    for src in kits.rglob("*"):
        if src.is_dir():
            rel = src.relative_to(pf)
            (root / rel).mkdir(parents=True, exist_ok=True)
    # 第二遍：移动文件（父目录被移动后路径可能已失效，跳过即可）
    for src in kits.rglob("*"):
        if src.is_file() and src.exists():
            rel = src.relative_to(pf)
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
    shutil.rmtree(pf, ignore_errors=True)


def extract_msi(msi_path: Path, dest: Path, log: Optional[LogCallback] = None,
                is_cancelled: Optional[Callable[[], bool]] = None):
    """把 MSI 管理式解包到 dest。Windows 用 msiexec，其它平台用 msiextract。

    支持通过 is_cancelled 回调中断（子进程会被终止并抛 CancelledError）。
    """
    msi_path = Path(msi_path)
    dest = Path(dest).resolve()

    if sys.platform == "win32":
        # 用参数列表而非 shell 字符串拼接，避免路径含 & " % 等字符时被解析/注入。
        # TARGETDIR 以反斜杠结尾（msiexec 惯例），避免个别安装包拼接路径异常。
        targetdir = str(dest).rstrip("\\/") + "\\"
        rc, output = _run_process(
            ["msiexec", "/a", str(msi_path), "/quiet", "/qn", f"TARGETDIR={targetdir}"],
            log=log, is_cancelled=is_cancelled,
        )
        if rc != 0:
            err = output.strip()
            raise RuntimeError(f"msiexec 解包失败: {msi_path.name} {err}")
        if log:
            log(f"已解包 {msi_path.name}")
    else:
        if shutil.which("msiextract") is None:
            raise RuntimeError("非 Windows 平台需要安装 msitools（msiextract）")
        rc, output = _run_process(
            ["msiextract", "-C", str(dest), str(msi_path)],
            log=log, is_cancelled=is_cancelled,
        )
        if rc != 0:
            raise RuntimeError(f"msiextract 解包失败: {msi_path.name} {output.strip()}")
        if log:
            log(f"已解包 {msi_path.name}")

        # msiextract 可能解出 Program Files 目录结构，统一提升到 dest 根
        _flatten_program_files(dest)


def move_windows_kits_up(root: Path):
    """防御性整理：若解包产物落在 root/Program Files/Windows Kits，则提升到 root。"""
    _flatten_program_files(root)
