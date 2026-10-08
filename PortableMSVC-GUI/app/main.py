"""便携版 MSVC 工具链 GUI 入口。"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from app.ui.main_window import MainWindow


def _is_compiled() -> bool:
    """判断是否运行在打包后的可执行文件中。

    - PyInstaller：设置 sys.frozen；
    - Nuitka：注入 __compiled__ 伪全局（源码态为 NameError）。
    两者皆非时为开发态（python -m app.main）。
    """
    if getattr(sys, "frozen", False):
        return True
    try:
        return bool(__compiled__)  # type: ignore[name-defined]  # Nuitka 注入
    except NameError:
        return False


def _resource_root() -> Path:
    """打包后的资源根目录：PyInstaller 用 _MEIPASS，Nuitka 用 exe 所在目录；
    开发态返回项目根。theme.qss / portable_msvc.ico 均在此目录下查找。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    if _is_compiled():
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[1]


def _icon_path() -> Path:
    """定位程序图标：打包态取资源根，开发态取项目根。"""
    p = _resource_root() / "portable_msvc.ico"
    if p.exists():
        return p
    return Path(__file__).resolve().parents[1] / "portable_msvc.ico"


def _load_stylesheet() -> str:
    """加载主题样式：打包态资源根平铺 theme.qss，开发态取 app/ui/theme.qss。"""
    p = _resource_root() / "theme.qss"
    if not p.exists():
        p = Path(__file__).resolve().parent / "ui" / "theme.qss"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _install_excepthook():
    """windowed 模式下 GUI 线程未捕获异常会静默消失，这里统一弹框 + 写日志。"""
    from app.utils import logger

    def _hook(exc_type, exc_value, exc_tb):
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.write_line(detail)
        QMessageBox.critical(
            None, "程序错误",
            f"发生未处理的异常：{exc_value}\n\n详细信息已写入日志。",
        )

    sys.excepthook = _hook


def main():
    _install_excepthook()
    # Windows 任务栏图标分组：设置 AppUserModelID 避免任务栏显示默认 python 图标
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PortableMSVC.GUI")
        except Exception:
            pass
    # 高分屏适配（必须在创建 QApplication 之前设置）
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("PortableMSVC-GUI")
    app.setWindowIcon(QIcon(str(_icon_path())))
    app.setStyleSheet(_load_stylesheet())

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
