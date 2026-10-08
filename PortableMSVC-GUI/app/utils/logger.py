"""日志工具：把运行日志同时落盘到 <输出目录>/logs/app.log，便于事后排查。

线程安全：追加写用锁保护；超限自动轮转（保留最近 3 份备份）。
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path

_log_file: Path | None = None
_lock = threading.Lock()
_oserror_reported = False          # 日志写失败后仅提示一次，避免刷屏

MAX_LOG_BYTES = 5 * 1024 * 1024      # 单个日志文件上限 5 MB
MAX_BACKUPS = 3                      # 轮转保留 app.log.1 ~ app.log.3


def setup_log_file(output_dir: Path) -> Path:
    """初始化日志文件路径（日志目录 = 输出目录/logs）。"""
    global _log_file
    logs_dir = Path(output_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    with _lock:
        _log_file = logs_dir / "app.log"
    return _log_file


def log_file() -> Path | None:
    return _log_file


def _rotate_if_needed(path: Path):
    """日志超限时轮转：app.log → app.log.1 → ... → app.log.MAX_BACKUPS。"""
    try:
        if path.stat().st_size <= MAX_LOG_BYTES:
            return
    except OSError:
        return
    try:
        for i in range(MAX_BACKUPS - 1, 0, -1):
            old = path.with_name(f"{path.name}.{i}")
            if old.exists():
                old.replace(path.with_name(f"{path.name}.{i + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError:
        pass


def write_line(msg: str):
    """带时间戳写入日志文件（GUI 控件同步显示由界面层负责）。"""
    global _oserror_reported
    if _log_file is None:
        return
    try:
        ts = datetime.now().strftime("%H:%M:%S")
        with _lock:
            _rotate_if_needed(_log_file)
            with _log_file.open("a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
    except OSError as exc:
        # 日志落盘失败不应影响主流程；但首次失败时向 stderr 提示一次，
        # 避免用户完全无感知（如输出目录被删除/只读）。
        if not _oserror_reported:
            _oserror_reported = True
            print(f"[logger] 日志写入失败（后续不再重复提示）: {exc}", file=sys.stderr)


def exe_dir() -> Path:
    """返回程序所在目录：Nuitka 打包后为 exe 目录，开发态为项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]
