"""主窗口：配置面板 + 阶段化进度 + 日志区 + 结果卡片。

状态机：IDLE → CHECKING → RUNNING → DONE / FAILED
布局（方案 A / VS Installer 风格）：顶部阶段指示器 → 配置两列网格 →
阶段进度条（6 段）→ 可折叠日志 → 结果面板 → 底部固定操作条
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QEvent, QSettings, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from ..core import environment
from ..core.models import EnvCheck, PipelineConfig, PipelineResult
from ..utils import logger
from ..workers.pipeline_worker import PipelineWorker

LICENSE_URL = "https://visualstudio.microsoft.com/license-terms/"

# 流水线 6 个阶段（名称 + 进度百分比区间）
# 与 pipeline 实际进度对齐：清单 2 → MSVC 下载 10-55 → SDK 下载 55-80 →
# cab 下载 80-84 → 解包 84-86 → 整理瘦身 86-90 → 打包 90-100（7z 实时 92-100）
STAGES = [("配置", 0, 2), ("下载", 2, 84), ("解包", 84, 86),
          ("瘦身", 86, 90), ("打包", 90, 100), ("完成", 100, 100)]


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("便携版 MSVC 工具链")
        self.resize(1024, 860)

        self._worker: PipelineWorker | None = None
        self._last_result: PipelineResult | None = None

        # 配置持久化：INI 存于用户配置目录（不污染注册表，便携）
        self._settings = QSettings(QSettings.IniFormat, QSettings.UserScope,
                                   "PortableMSVC", "PortableMSVC-GUI")

        # 输出目录磁盘空间提示：400ms 防抖，避免每次击键都做 disk_usage
        self._disk_timer = QTimer(self)
        self._disk_timer.setSingleShot(True)
        self._disk_timer.setInterval(400)
        self._disk_timer.timeout.connect(self._update_disk_label)

        self._build_ui()
        self._apply_defaults()
        self._reset_state()

        # 全局事件过滤器：点击空白/标签等不可聚焦区域时，让输入框退出聚焦
        self._last_click_pos: QPoint | None = None
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # ------------------------------------------------------------------ 界面构建
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(14)

        root.addWidget(self._build_header())

        # 配置区：两列网格（输出目录 | 版本与架构，选项横跨）
        self.config_area = QFrame()
        self.config_area.setObjectName("configArea")
        config_layout = QGridLayout(self.config_area)
        config_layout.setContentsMargins(0, 0, 0, 0)
        config_layout.setHorizontalSpacing(14)
        config_layout.setVerticalSpacing(14)
        config_layout.addWidget(self._build_output_group(), 0, 0)
        config_layout.addWidget(self._build_options_group(), 0, 1)
        config_layout.addWidget(self._build_version_group(), 1, 0, 1, 2)
        config_layout.setColumnStretch(0, 1)
        config_layout.setColumnStretch(1, 1)
        root.addWidget(self.config_area)

        # 阶段进度区
        root.addLayout(self._build_stage_progress())

        # 日志区（可折叠）
        root.addLayout(self._build_log_header())
        self.log_panel = QPlainTextEdit()
        self.log_panel.setReadOnly(True)
        self.log_panel.setMaximumBlockCount(3000)
        self.log_panel.setMinimumHeight(220)
        root.addWidget(self.log_panel, stretch=1)

        # 结果面板（默认隐藏）
        self.result_panel = self._build_result_panel()
        self.result_panel.setVisible(False)
        root.addWidget(self.result_panel)

        # 底部固定操作条
        root.addLayout(self._build_action_bar())

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("headerPanel")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(4, 10, 4, 12)
        layout.setSpacing(16)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        title = QLabel("便携式 MSVC 工具链")
        title.setObjectName("appTitle")
        subtitle = QLabel("一键下载 MSVC 编译器 + Windows SDK，无需安装 Visual Studio，自动打包为绿色工具链")
        subtitle.setObjectName("appSubtitle")
        text_col.addWidget(title)
        text_col.addWidget(subtitle)
        layout.addLayout(text_col, 1)

        # 阶段指示器（配置 → 下载 → 解包 → 瘦身 → 打包 → 完成）
        chips_row = QHBoxLayout()
        chips_row.setSpacing(6)
        self._stage_chips: list[QLabel] = []
        for name, _lo, _hi in STAGES:
            chip = QLabel(name)
            chip.setObjectName("stageChip")
            chip.setProperty("stageState", "idle")
            chips_row.addWidget(chip)
            self._stage_chips.append(chip)
        layout.addLayout(chips_row)
        return header

    def _build_stage_progress(self) -> QHBoxLayout:
        """阶段进度行：当前阶段文本 + 6 段分段进度条 + 百分比。"""
        row = QHBoxLayout()
        row.setSpacing(10)

        self.stage_label = QLabel("就绪")
        self.stage_label.setObjectName("statusLabel")
        self.stage_label.setMinimumWidth(90)
        row.addWidget(self.stage_label)

        self._stage_bars: list[QProgressBar] = []
        for _name, _lo, _hi in STAGES:
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setFixedHeight(14)
            row.addWidget(bar, 1)
            self._stage_bars.append(bar)

        # 兼容保留整体进度条（隐藏，供内部状态同步使用）
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)

        self.progress_pct = QLabel("0%")
        self.progress_pct.setObjectName("statusLabel")
        self.progress_pct.setMinimumWidth(44)
        self.progress_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.progress_pct)
        return row

    def _build_log_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel("运行日志")
        label.setObjectName("fieldLabel")
        self.save_log_btn = QPushButton("保存日志")
        self.save_log_btn.setToolTip("把当前日志面板内容保存为文件")
        self.save_log_btn.clicked.connect(self._on_save_log)
        self.log_toggle_btn = QPushButton("收起")
        self.log_toggle_btn.setToolTip("折叠/展开日志区")
        self.log_toggle_btn.setCheckable(True)
        self.log_toggle_btn.setChecked(False)
        self.log_toggle_btn.clicked.connect(self._on_toggle_log)
        row.addWidget(label)
        row.addStretch(1)
        row.addWidget(self.save_log_btn)
        row.addWidget(self.log_toggle_btn)
        return row

    def _on_save_log(self):
        """把当前日志面板内容导出为文件，便于事后排查。"""
        log_path = logger.log_file()
        default = (str(log_path) if log_path
                   else str(Path(self.output_edit.text().strip() or ".") / "logs" / "app.log"))
        path, _ = QFileDialog.getSaveFileName(self, "保存日志", default,
                                              "日志文件 (*.log);;文本文件 (*.txt)")
        if not path:
            return
        try:
            Path(path).write_text(self.log_panel.toPlainText(), encoding="utf-8")
            self.stage_label.setText(f"日志已保存: {path}")
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", f"无法写入日志文件：{exc}")

    def _set_chip_state(self, index: int, state: str):
        chip = self._stage_chips[index]
        if chip.property("stageState") == state:
            return
        chip.setProperty("stageState", state)
        chip.style().unpolish(chip)
        chip.style().polish(chip)

    def _update_stage_state(self, value: int):
        """根据总体进度 0-100 更新 6 段进度条与顶部阶段指示器。"""
        value = max(0, min(100, value))
        for i, (_name, lo, hi) in enumerate(STAGES):
            # 段内进度
            span = hi - lo
            if value >= hi:
                pct = 100
                state = "done"
            elif value <= lo:
                pct = 0
                state = "idle"
            else:
                # span 理论上恒 > 0（完成段 100-100 只可能落在前两个分支），
                # 但为防御边界取值仍加保护，避免除零。
                pct = int((value - lo) / (span or 1) * 100)
                state = "active"
            self._stage_bars[i].setValue(pct)
            self._set_chip_state(i, state)

    def _on_toggle_log(self):
        collapsed = self.log_toggle_btn.isChecked()
        self.log_panel.setVisible(not collapsed)
        self.log_toggle_btn.setText("展开" if collapsed else "收起")
        if collapsed:
            # 折叠后把窗口收缩到内容所需高度，避免上方区域被拉伸占位
            QTimer.singleShot(0, self._shrink_after_log_collapse)

    def _shrink_after_log_collapse(self):
        """折叠日志后收缩窗口高度（只缩不放），消除布局多余空间。"""
        hint = self.sizeHint()
        if self.height() > hint.height():
            self.resize(self.width(), max(hint.height(), self.minimumHeight()))

    # ------------------------------------------------------------------ 焦点管理
    def eventFilter(self, obj, event):  # noqa: N802 —— Qt 事件过滤器命名
        if event.type() == QEvent.MouseButtonPress:
            self._last_click_pos = event.globalPosition().toPoint()
            # 延迟到事件链处理完后再判定，避免抢在控件自身的聚焦/失焦逻辑之前
            QTimer.singleShot(0, self._maybe_clear_focus)
        return super().eventFilter(obj, event)

    def _maybe_clear_focus(self):
        """点击不可聚焦区域（空白、标签、分组框等）时，让输入框退出聚焦。

        Qt 默认焦点只在可聚焦控件间转移：点击 QLabel / 空白等 NoFocus 区域时
        焦点会停留在上一个输入框。这里统一把这类点击视为"离开输入"，清除焦点。
        """
        focus = QApplication.focusWidget()
        if focus is None or self._last_click_pos is None:
            return
        if not isinstance(focus, (QLineEdit, QPlainTextEdit, QComboBox)):
            return  # 焦点不在输入类控件上，无需处理
        clicked = QApplication.widgetAt(self._last_click_pos)
        if clicked is None:
            focus.clearFocus()
            return
        if clicked is focus or focus.isAncestorOf(clicked) or clicked.isAncestorOf(focus):
            return  # 点击仍在输入控件自身或其弹出层（下拉列表）内
        if isinstance(clicked, (QLineEdit, QPlainTextEdit, QComboBox,
                                QCheckBox, QPushButton)):
            return  # 可聚焦控件，点击后焦点自然移交
        if clicked.focusPolicy() != Qt.NoFocus:
            return  # 其余有焦点策略的控件接管焦点
        focus.clearFocus()

    def _build_output_group(self) -> QGroupBox:
        group = QGroupBox("输出目录")
        grid = QGridLayout(group)
        grid.setContentsMargins(14, 18, 14, 14)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("选择输出目录（MSVC 文件夹与压缩包将生成于此）")
        self.output_edit.textChanged.connect(lambda _t: self._disk_timer.start())

        self.browse_btn = QPushButton("浏览…")
        self.browse_btn.clicked.connect(self._on_browse)

        self.disk_label = QLabel()
        self.disk_label.setObjectName("statusLabel")

        grid.addWidget(self.output_edit, 0, 0, 1, 1)
        grid.addWidget(self.browse_btn, 0, 1, 1, 1)
        grid.addWidget(self.disk_label, 1, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        return group

    def _build_version_group(self) -> QGroupBox:
        group = QGroupBox("版本与架构")
        grid = QGridLayout(group)
        grid.setContentsMargins(14, 18, 14, 14)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)

        grid.addWidget(self._field_label("Visual Studio 版本"), 0, 0)
        self.vs_combo = QComboBox()
        for value, label in [
            ("latest", "最新稳定版 (latest)"),
            ("2026", "Visual Studio 2026"),
            ("2022", "Visual Studio 2022"),
            ("2019", "Visual Studio 2019"),
        ]:
            self.vs_combo.addItem(label, value)
        grid.addWidget(self.vs_combo, 0, 1)

        grid.addWidget(self._field_label("主机架构"), 0, 2)
        self.host_combo = QComboBox()
        for value, label in [("x64", "x64"), ("x86", "x86"), ("arm64", "arm64")]:
            self.host_combo.addItem(label, value)
        grid.addWidget(self.host_combo, 0, 3)

        grid.addWidget(self._field_label("目标架构"), 1, 0)
        target_box = QWidget()
        target_layout = QHBoxLayout(target_box)
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.setSpacing(14)
        self.target_checks: dict[str, QCheckBox] = {}
        for arch in ("x64", "x86", "arm", "arm64"):
            check = QCheckBox(arch)
            self.target_checks[arch] = check
            target_layout.addWidget(check)
        target_layout.addStretch(1)
        grid.addWidget(target_box, 1, 1, 1, 3)

        grid.addWidget(self._field_label("MSVC 版本"), 2, 0)
        self.msvc_edit = QLineEdit()
        self.msvc_edit.setPlaceholderText("留空 = 最新")
        grid.addWidget(self.msvc_edit, 2, 1)

        grid.addWidget(self._field_label("Windows SDK 版本"), 2, 2)
        self.sdk_edit = QLineEdit()
        self.sdk_edit.setPlaceholderText("留空 = 最新")
        grid.addWidget(self.sdk_edit, 2, 3)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        return group

    def _build_options_group(self) -> QGroupBox:
        group = QGroupBox("选项")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(12)

        self.pack_check = QCheckBox("执行完毕后打包为压缩包（优先 7z，回退 zip）")
        self.pack_check.setChecked(True)
        layout.addWidget(self.pack_check)

        pack_row = QHBoxLayout()
        pack_row.setSpacing(8)
        self.pack_level_label = QLabel("压缩级别")
        self.pack_level_label.setObjectName("fieldLabel")
        self.pack_level_combo = QComboBox()
        for value, label in [
            (1, "快速 (1)"),
            (5, "均衡 (5)"),
            (9, "高压缩 (9，最慢)"),
        ]:
            self.pack_level_combo.addItem(label, value)
        self.pack_level_combo.setCurrentIndex(1)  # 默认均衡
        self.pack_level_combo.setEnabled(True)
        self.pack_check.toggled.connect(self.pack_level_combo.setEnabled)
        pack_row.addWidget(self.pack_level_label)
        pack_row.addWidget(self.pack_level_combo)
        pack_row.addStretch(1)
        layout.addLayout(pack_row)

        self.preview_check = QCheckBox("允许预览版 MSVC（release candidate）")
        layout.addWidget(self.preview_check)

        license_row = QHBoxLayout()
        license_row.setSpacing(8)
        self.license_check = QCheckBox("我已阅读并接受")
        self.license_check.setChecked(True)
        license_link = QLabel('<a href="#" style="color:#2563eb;">Visual Studio 许可协议</a>')
        license_link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        license_link.linkActivated.connect(lambda _url: QDesktopServices.openUrl(QUrl(LICENSE_URL)))
        license_row.addWidget(self.license_check)
        license_row.addWidget(license_link)
        license_row.addStretch(1)
        layout.addLayout(license_row)
        return group

    def _build_action_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(10)

        self.env_check_btn = QPushButton("环境检测")
        self.env_check_btn.clicked.connect(self._on_env_check)

        self.start_btn = QPushButton("开始执行")
        self.start_btn.setObjectName("primaryButton")
        self.start_btn.clicked.connect(self._on_start)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("dangerButton")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._on_cancel)

        bar.addWidget(self.env_check_btn)
        bar.addStretch(1)
        bar.addWidget(self.cancel_btn)
        bar.addWidget(self.start_btn)
        return bar

    def _build_result_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("resultPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        self.result_title = QLabel()
        self.result_title.setObjectName("resultTitle")
        layout.addWidget(self.result_title)

        self.result_detail = QLabel()
        self.result_detail.setObjectName("statusLabel")
        self.result_detail.setWordWrap(True)
        self.result_detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.result_detail)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.open_dir_btn = QPushButton("打开输出目录")
        self.open_dir_btn.setObjectName("primaryButton")
        self.open_dir_btn.clicked.connect(self._on_open_dir)
        self.copy_path_btn = QPushButton("复制路径")
        self.copy_path_btn.clicked.connect(self._on_copy_path)
        self.reset_btn = QPushButton("再次执行")
        self.reset_btn.clicked.connect(self._reset_state)
        buttons.addWidget(self.open_dir_btn)
        buttons.addWidget(self.copy_path_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.reset_btn)
        layout.addLayout(buttons)
        return panel

    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        return label

    # ------------------------------------------------------------------ 默认值与状态
    def _apply_defaults(self):
        """从 QSettings 恢复上次配置；首次启动使用默认值。"""
        s = self._settings
        self.output_edit.setText(str(s.value("output_dir", str(logger.exe_dir()))))

        vs = s.value("vs", "latest")
        idx = self.vs_combo.findData(vs)
        self.vs_combo.setCurrentIndex(idx if idx >= 0 else 0)

        host = s.value("host", "x64")
        idx = self.host_combo.findData(host)
        self.host_combo.setCurrentIndex(idx if idx >= 0 else 0)

        targets = s.value("targets", ["x64", "x86"])
        if isinstance(targets, str):
            targets = [targets]  # QSettings 单元素列表会以字符串返回
        for arch, check in self.target_checks.items():
            check.setChecked(arch in targets)

        self.msvc_edit.setText(str(s.value("msvc_version", "")))
        self.sdk_edit.setText(str(s.value("sdk_version", "")))
        self.preview_check.setChecked(bool(s.value("preview", False, type=bool)))
        self.pack_check.setChecked(bool(s.value("pack", True, type=bool)))
        level = int(s.value("pack_level", 5))
        idx = self.pack_level_combo.findData(level)
        if idx >= 0:
            self.pack_level_combo.setCurrentIndex(idx)

    def _save_config(self, cfg: PipelineConfig):
        """把当前配置写入 QSettings，供下次启动恢复。"""
        s = self._settings
        s.setValue("output_dir", str(cfg.output_dir))
        s.setValue("vs", cfg.vs)
        s.setValue("host", cfg.host)
        s.setValue("targets", cfg.targets)
        s.setValue("msvc_version", cfg.msvc_version)
        s.setValue("sdk_version", cfg.sdk_version)
        s.setValue("preview", cfg.preview)
        s.setValue("pack", cfg.pack)
        s.setValue("pack_level", cfg.pack_level)
        s.sync()

    def _reset_state(self):
        self._last_result = None
        self.result_panel.setVisible(False)
        self.progress_bar.setValue(0)
        self.stage_label.setText("就绪")
        self.progress_pct.setText("0%")
        self._update_stage_state(0)
        self.log_panel.clear()
        self.config_area.setEnabled(True)
        self.env_check_btn.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.cancel_btn.setVisible(False)

    def _update_disk_label(self):
        free_gb = environment.disk_free_gb(Path(self.output_edit.text()))
        self.disk_label.setText(
            f"可用空间：{free_gb:.1f} GB" if free_gb is not None else "路径不可用"
        )

    # ------------------------------------------------------------------ 事件
    def _on_browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def _collect_config(self) -> PipelineConfig:
        targets = [arch for arch, check in self.target_checks.items() if check.isChecked()]
        return PipelineConfig(
            vs=self.vs_combo.currentData(),
            host=self.host_combo.currentData(),
            targets=targets,  # 可为空；validate() 会拦截"至少选择一个目标架构"
            msvc_version=self.msvc_edit.text().strip(),
            sdk_version=self.sdk_edit.text().strip(),
            preview=self.preview_check.isChecked(),
            accept_license=self.license_check.isChecked(),
            output_dir=Path(self.output_edit.text().strip() or "."),
            pack=self.pack_check.isChecked(),
            pack_level=self.pack_level_combo.currentData(),
        )

    def _on_env_check(self):
        cfg = self._collect_config()
        self._save_config(cfg)
        logger.setup_log_file(cfg.output_dir)
        self._start_worker(cfg, check_only=True)

    def _on_start(self):
        cfg = self._collect_config()
        self._save_config(cfg)

        errors = cfg.validate()
        if errors:
            QMessageBox.warning(self, "配置有误", "\n".join(errors))
            return
        if not cfg.accept_license:
            QMessageBox.warning(self, "许可协议", "请先勾选接受 Visual Studio 许可协议。")
            return
        if cfg.output_dir.exists() and (cfg.output_dir / "MSVC").exists():
            ret = QMessageBox.question(
                self, "目录已存在",
                "输出目录中已存在 MSVC 文件夹，是否覆盖后重新生成？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                return

        logger.setup_log_file(cfg.output_dir)
        self._start_worker(cfg, check_only=False)

    def _on_cancel(self):
        if self._worker is not None:
            self.cancel_btn.setEnabled(False)
            self.stage_label.setText("正在取消…")
            self._worker.cancel()

    def _start_worker(self, cfg: PipelineConfig, check_only: bool):
        self._set_running(True, check_only=check_only)
        self._worker = PipelineWorker(cfg, check_only=check_only)
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self._on_log)
        self._worker.env_checks_done.connect(self._on_env_checks_done)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(lambda: self._set_running(False))
        self._worker.start()

    def _set_running(self, running: bool, check_only: bool = False):
        for w in (self.env_check_btn, self.start_btn):
            w.setEnabled(not running)
        # 环境检测（check_only）同样可取消（网络检查等长耗时步骤可中断）
        self.cancel_btn.setVisible(running)
        # 执行时配置区降噪：置灰并锁定，焦点让给进度与日志
        self.config_area.setEnabled(not running)
        if running:
            self.result_panel.setVisible(False)
            self.stage_label.setText("环境检测中…" if check_only else "执行中…")
            self.log_panel.clear()
            if not check_only:
                self._update_stage_state(0)
        elif self.stage_label.text() in ("环境检测中…", "执行中…"):
            # 仅当状态仍是"进行中"时重置；成功/失败/取消已有专属状态，不覆盖
            self.stage_label.setText("就绪")

    # ------------------------------------------------------------------ 信号处理
    def _on_progress(self, value: int):
        self.progress_bar.setValue(value)
        self.progress_pct.setText(f"{value}%")
        self._update_stage_state(value)

    def _on_log(self, line: str):
        self.log_panel.appendPlainText(line)

    def _on_env_checks_done(self, checks: list[EnvCheck]):
        blocking = [c for c in checks if c.level == "error"]
        marks = {"error": "✗", "warn": "⚠", "info": "✓"}
        lines = ["==================== 环境检测结果 ===================="]
        lines.extend(f"{marks[c.level]} {c.name}：{c.detail}" for c in checks)
        if environment.has_blocking_errors(checks):
            lines.append(
                f"检测完成：共 {len(checks)} 项，发现 {len(blocking)} 个阻断性问题，请处理后重试。"
            )
        else:
            lines.append(f"检测完成：共 {len(checks)} 项，环境检查通过，可以开始执行。")
        lines.append("=" * 54)
        self.log_panel.appendPlainText("\n".join(lines))

    def _on_succeeded(self, result: PipelineResult):
        self._last_result = result
        self.progress_bar.setValue(100)
        self.progress_pct.setText("100%")
        self._update_stage_state(100)
        self.stage_label.setText("完成")
        self._show_result(result, failed=False)

    def _on_cancelled(self):
        self.stage_label.setText("已取消")
        if self._last_result is None:
            self._last_result = PipelineResult(ok=False, cancelled=True, error="操作已取消")
        else:
            self._last_result.ok = False
            self._last_result.cancelled = True
        self._show_result(self._last_result, cancelled=True)

    def _on_failed(self, message: str):
        self.stage_label.setText("失败")
        if self._last_result is None:
            self._last_result = PipelineResult(ok=False, error=message)
        else:
            self._last_result.ok = False
            self._last_result.error = message
        self._show_result(self._last_result, failed=True)

    def _show_result(self, result: PipelineResult, failed: bool = False, cancelled: bool = False):
        panel = self.result_panel
        panel.setObjectName("resultPanelCancelled" if cancelled else (
            "resultPanelError" if failed else "resultPanel"))
        panel.style().unpolish(panel)
        panel.style().polish(panel)

        if cancelled:
            self.result_title.setObjectName("resultTitleCancelled")
            self.result_title.setText("操作已取消")
            detail = ("已中止执行。下载缓存已保留（便于断点续传），"
                      "部分解包产物可能残留，可在确认后手动清理并重新开始。")
            if result.msvc_dir and result.msvc_dir.exists():
                detail += f"\n已生成的不完整目录：{result.msvc_dir}"
            self.result_detail.setText(detail)
            self.open_dir_btn.setEnabled(bool(result.msvc_dir and result.msvc_dir.exists()))
        elif failed:
            self.result_title.setObjectName("resultTitleError")
            self.result_title.setText("执行失败")
            detail = f"错误信息：{result.error}"
            if result.msvc_dir and result.msvc_dir.exists():
                detail += f"\n已生成的不完整目录：{result.msvc_dir}"
            self.result_detail.setText(detail)
            self.open_dir_btn.setEnabled(bool(result.msvc_dir and result.msvc_dir.exists()))
        else:
            self.result_title.setObjectName("resultTitle")
            self.result_title.setText("执行成功，工具链已就绪")
            parts = [f"MSVC 版本：{result.msvc_version}", f"Windows SDK：{result.sdk_version}"]
            if result.archive:
                parts.append(f"压缩包：{result.archive}")
            if result.msvc_dir:
                parts.append(f"工具链目录：{result.msvc_dir}")
            if result.setup_bats:
                parts.append("环境脚本：" + "、".join(str(p) for p in result.setup_bats))
            self.result_detail.setText("\n".join(parts))
            self.open_dir_btn.setEnabled(True)

        panel.setVisible(True)

    def _on_open_dir(self):
        target = None
        if self._last_result:
            if self._last_result.archive and self._last_result.archive.exists():
                target = self._last_result.archive.parent
            elif self._last_result.msvc_dir and self._last_result.msvc_dir.exists():
                target = self._last_result.msvc_dir
        if target is None:
            target = Path(self.output_edit.text())
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _on_copy_path(self):
        if not self._last_result:
            return
        paths = []
        if self._last_result.archive:
            paths.append(str(self._last_result.archive))
        if self._last_result.msvc_dir:
            paths.append(str(self._last_result.msvc_dir))
        QApplication.clipboard().setText("\n".join(paths))
        self.stage_label.setText("路径已复制到剪贴板")

    # ------------------------------------------------------------------ 关闭
    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            if not self._worker.wait(3000):
                # 线程仍在收尾（可能在 msiexec/打包等阻塞阶段）：
                # 提供「等待」与「强制退出」两个选项。
                box = QMessageBox(self)
                box.setWindowTitle("正在收尾")
                box.setIcon(QMessageBox.Warning)
                box.setText("后台任务仍在执行，无法立即安全退出。")
                box.setInformativeText(
                    "「等待」：任务继续运行，可稍后再次关闭窗口。\n"
                    "「强制退出」：立即终止后台任务并退出程序，"
                    "可能残留半成品文件或子进程。"
                )
                wait_btn = box.addButton("等待", QMessageBox.AcceptRole)
                force_btn = box.addButton("强制退出", QMessageBox.DestructiveRole)
                box.setDefaultButton(wait_btn)
                box.exec()
                if box.clickedButton() is wait_btn:
                    event.ignore()
                    return
                # 用户选择强制退出：先请求中断并等待，仍卡死则终止线程。
                # 注意：terminate() 不执行线程清理代码，可能留下子进程与半成品，
                # 已在提示文案中明确告知。
                self._worker.requestInterruption()
                if not self._worker.wait(2000):
                    self._worker.terminate()
                    self._worker.wait(5000)
        super().closeEvent(event)
