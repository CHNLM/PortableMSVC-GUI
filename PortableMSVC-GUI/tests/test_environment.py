"""environment 模块回归测试（P2-10 补覆盖：磁盘 / 检查项 / 阻断判定）。"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.core import environment
from app.core.models import CancelledError, EnvCheck


class DiskFreeTest(unittest.TestCase):
    def test_normal(self):
        with mock.patch("app.core.environment.shutil.disk_usage",
                        return_value=SimpleNamespace(free=8 * 1024 ** 3)):
            gb = environment.disk_free_gb(Path("."))
        self.assertAlmostEqual(gb, 8.0, places=3)

    def test_oserror_returns_none(self):
        with mock.patch("app.core.environment.shutil.disk_usage",
                        side_effect=OSError("no disk")):
            self.assertIsNone(environment.disk_free_gb(Path(".")))

    def test_disk_free_bytes_exact(self):
        """P3-2：字节级 API 直接返回 disk_usage().free，无浮点换算。"""
        with mock.patch("app.core.environment.shutil.disk_usage",
                        return_value=SimpleNamespace(free=12 * 1024 ** 3)):
            self.assertEqual(environment.disk_free_bytes(Path(".")), 12 * 1024 ** 3)


class RunChecksTest(unittest.TestCase):
    """run_checks 在 win32 + 工具齐全 + 网络可用时应全部 info。"""

    def test_win32_all_ok(self):
        with mock.patch("app.core.environment.sys.platform", "win32"), \
             mock.patch("app.core.environment.shutil.which", return_value="/bin/tool"), \
             mock.patch("app.core.environment.shutil.disk_usage",
                        return_value=SimpleNamespace(free=50 * 1024 ** 3)), \
             mock.patch("app.core.environment.urllib.request.urlopen"):
            checks = environment.run_checks(Path("."))
        self.assertTrue(checks)
        self.assertTrue(any(c.name == "操作系统" and c.level == "info" for c in checks))
        self.assertTrue(any(c.name == "msiexec" and c.level == "info" for c in checks))
        self.assertTrue(any(c.name == "7-Zip" and c.level == "info" for c in checks))
        self.assertTrue(any(c.name == "磁盘空间" and c.level == "info" for c in checks))
        self.assertTrue(any(c.name == "网络连通" and c.level == "info" for c in checks))
        self.assertFalse(any(c.level == "error" for c in checks), f"不应有阻断项: {checks}")

    def test_non_win32_blocks(self):
        with mock.patch("app.core.environment.sys.platform", "linux"), \
             mock.patch("app.core.environment.shutil.which", return_value=None), \
             mock.patch("app.core.environment.shutil.disk_usage",
                        return_value=SimpleNamespace(free=50 * 1024 ** 3)), \
             mock.patch("app.core.environment.urllib.request.urlopen"):
            checks = environment.run_checks(Path("."))
        self.assertTrue(any(c.level == "error" for c in checks),
                        "非 Windows 平台应报告阻断性问题")

    def test_low_disk_warns_or_blocks(self):
        with mock.patch("app.core.environment.sys.platform", "win32"), \
             mock.patch("app.core.environment.shutil.which", return_value="/bin/tool"), \
             mock.patch("app.core.environment.shutil.disk_usage",
                        return_value=SimpleNamespace(free=4 * 1024 ** 3)), \
             mock.patch("app.core.environment.urllib.request.urlopen"):
            checks = environment.run_checks(Path("."))
        disk = next(c for c in checks if c.name == "磁盘空间")
        self.assertEqual(disk.level, "error", "低于 8GB 应为阻断")

    def test_cancelled_raises(self):
        """P3-3：is_cancelled 为 True 时应在检查项之间抛 CancelledError。"""
        with self.assertRaises(CancelledError):
            environment.run_checks(Path("."), is_cancelled=lambda: True)

    def test_cancel_not_triggered_when_false(self):
        """取消回调返回 False 时检查正常完成（不误取消）。"""
        with mock.patch("app.core.environment.sys.platform", "win32"), \
             mock.patch("app.core.environment.shutil.which", return_value="/bin/tool"), \
             mock.patch("app.core.environment.shutil.disk_usage",
                        return_value=SimpleNamespace(free=50 * 1024 ** 3)), \
             mock.patch("app.core.environment.urllib.request.urlopen"):
            checks = environment.run_checks(Path("."), is_cancelled=lambda: False)
        self.assertTrue(checks)


class HasBlockingErrorsTest(unittest.TestCase):
    def test_no_blocking(self):
        self.assertFalse(environment.has_blocking_errors([
            EnvCheck("a", "info", ""), EnvCheck("b", "warn", ""),
        ]))

    def test_any_error_blocks(self):
        self.assertTrue(environment.has_blocking_errors([
            EnvCheck("a", "info", ""), EnvCheck("b", "error", ""),
        ]))


if __name__ == "__main__":
    unittest.main()
