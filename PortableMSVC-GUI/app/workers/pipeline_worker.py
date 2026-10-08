"""QThread 后台任务：负责在子线程中执行环境检查与完整流水线。

通过信号与界面线程通信，保证下载/解包等长任务不阻塞 UI。
"""
from __future__ import annotations

import traceback

from PySide6.QtCore import QThread, Signal

from ..core import environment
from ..core.models import CancelledError, PipelineConfig
from ..core.pipeline import Pipeline
from ..utils import logger


class PipelineWorker(QThread):
    progress = Signal(int)                      # 总体进度 0-100
    log = Signal(str)                           # 日志行
    env_checks_done = Signal(object)            # list[EnvCheck]
    succeeded = Signal(object)                  # PipelineResult
    cancelled = Signal()                        # 用户取消
    failed = Signal(str)                        # 错误信息

    def __init__(self, cfg: PipelineConfig, check_only: bool = False, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._check_only = check_only
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):  # noqa: C901 —— 后台线程入口
        try:
            if self._check_only:
                try:
                    checks = environment.run_checks(
                        self._cfg.output_dir,
                        is_cancelled=lambda: self._cancel,
                    )
                except CancelledError:
                    self.cancelled.emit()
                    return
                self.env_checks_done.emit(checks)
                return

            logger.write_line(f"=== 开始执行: vs={self._cfg.vs} host={self._cfg.host} "
                              f"targets={','.join(self._cfg.targets)} ===")

            pipeline = Pipeline(
                self._cfg,
                on_log=self._emit_log,
                on_progress=self.progress.emit,
                is_cancelled=lambda: self._cancel,
            )
            result = pipeline.run()

            if self._cancel or result.cancelled:
                logger.write_line("用户取消")
                self.cancelled.emit()
                return
            if result.ok:
                logger.write_line("=== 执行成功 ===")
                self.succeeded.emit(result)
            else:
                logger.write_line(f"=== 执行失败: {result.error} ===")
                self.failed.emit(result.error)
        except Exception as exc:  # noqa: BLE001 —— 兜底防止线程静默崩溃
            detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            logger.write_line(detail)
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _emit_log(self, msg: str):
        logger.write_line(msg)
        self.log.emit(msg)
