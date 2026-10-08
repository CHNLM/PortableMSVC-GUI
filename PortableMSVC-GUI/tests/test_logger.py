"""logger 模块回归测试（P2-10 补覆盖：落盘 / 轮转）。"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from app.utils import logger


class LoggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msvcgui_log_"))

    def tearDown(self):
        logger._log_file = None
        logger.MAX_LOG_BYTES = 5 * 1024 * 1024  # 还原默认
        logger.MAX_BACKUPS = 3
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_setup_creates_logs_dir(self):
        path = logger.setup_log_file(self.tmp)
        self.assertEqual(path, self.tmp / "logs" / "app.log")
        self.assertTrue((self.tmp / "logs").is_dir())

    def test_write_line_creates_file_with_timestamp(self):
        path = logger.setup_log_file(self.tmp)
        logger.write_line("hello world")
        content = path.read_text(encoding="utf-8")
        self.assertIn("hello world", content)
        self.assertIn("[", content)  # 带时间戳 [HH:MM:SS]

    def test_write_without_setup_is_noop(self):
        logger._log_file = None
        logger.write_line("should-not-crash")  # 不抛错即可

    def test_rotation_creates_backup(self):
        path = logger.setup_log_file(self.tmp)
        logger.MAX_LOG_BYTES = 64
        logger.MAX_BACKUPS = 1
        logger.write_line("x" * 200)   # 第一次写：创建 app.log（已超限）
        logger.write_line("y" * 200)   # 第二次写：触发轮转
        self.assertTrue(path.exists())
        self.assertTrue(path.with_name("app.log.1").exists(),
                        "超限后应生成轮转备份 app.log.1")
        # 轮转后新文件只含第二次写入内容
        self.assertIn("y" * 200, path.read_text(encoding="utf-8"))
        self.assertNotIn("x" * 200, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
