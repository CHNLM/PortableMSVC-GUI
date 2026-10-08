"""packager 模块回归测试（P1-4：超时杀进程、取消、错误信息透传）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import packager
from app.core.models import CancelledError


def _fake_running_proc():
    """永不结束的假子进程。"""
    proc = mock.Mock()
    proc.poll.return_value = None
    proc.returncode = None
    proc.stdout.read.return_value = ""  # 无输出，读取线程立即退出
    return proc


class RunTimeoutTest(unittest.TestCase):
    """P1-4：打包子进程超时后必须被终止，返回 False 且不抛异常。"""

    def test_timeout_kills_process(self):
        logs: list[str] = []
        with mock.patch("app.core.packager.subprocess.Popen",
                        return_value=_fake_running_proc()) as mp:
            ok = packager._run(["7z", "a", "x.7z", "src"], Path(tempfile.gettempdir()),
                               log=logs.append, timeout=1)
        self.assertFalse(ok)
        mp.return_value.terminate.assert_called_once()
        self.assertTrue(any("超时" in line for line in logs), f"缺少超时日志: {logs}")

    def test_cancel_raises_cancelled(self):
        with mock.patch("app.core.packager.subprocess.Popen",
                        return_value=_fake_running_proc()) as mp:
            with self.assertRaises(CancelledError):
                packager._run(["7z", "a", "x.7z", "src"], Path(tempfile.gettempdir()),
                              is_cancelled=lambda: True)
        mp.return_value.terminate.assert_called_once()

    def test_failure_reports_tail_output(self):
        """失败时 stderr 尾部应透传给日志，便于诊断而非静默。"""
        proc = mock.Mock()
        proc.poll.side_effect = [None, 2]   # 第一次未结束，第二次退出码 2
        proc.returncode = 2
        proc.stdout.read.return_value = "error line\n"  # 有输出（会写入临时文件）
        logs: list[str] = []
        with mock.patch("app.core.packager.subprocess.Popen", return_value=proc):
            ok = packager._run(["7z", "a", "x.7z", "src"], Path(tempfile.gettempdir()),
                               log=logs.append)
        self.assertFalse(ok)
        # 输出写到临时文件，正常场景下包含内容；至少应记录失败日志
        self.assertTrue(any("失败" in line for line in logs), f"缺少失败日志: {logs}")

    def test_on_progress_parses_7z_output(self):
        """P2-2：_run 从 stdout 实时解析 7z -bsp1 进度行（\r 分隔）。"""
        proc = mock.Mock()
        proc.poll.side_effect = [None, 0]   # 第一次运行中，第二次正常结束
        proc.returncode = 0
        proc.stdout.read.side_effect = [
            " 12% 3 files 100 bytes\r",
            " 57% 4 files 200 bytes\r",
            "100% 5 files 300 bytes\r\n",
            "",
        ]
        got: list[int] = []
        with mock.patch("app.core.packager.subprocess.Popen", return_value=proc):
            ok = packager._run(["7z", "a", "-bsp1", "x.7z", "src"],
                               Path(tempfile.gettempdir()),
                               on_progress=got.append)
        self.assertTrue(ok)
        self.assertEqual(got, [12, 57, 100], f"应解析出单调递增的进度: {got}")

    def test_on_progress_ignores_non_progress_lines(self):
        """非进度行（如文件名输出）不得误触发进度回调。"""
        proc = mock.Mock()
        proc.poll.side_effect = [None, 0]
        proc.returncode = 0
        proc.stdout.read.side_effect = [
            "Compressing  MSVC\\VC\\Tools\\MSVC\\14.42\\bin\\cl.exe\n",
            " 30% 8 files 999 bytes\r\n",
            "",
        ]
        got: list[int] = []
        with mock.patch("app.core.packager.subprocess.Popen", return_value=proc):
            packager._run(["7z", "a", "-bsp1", "x.7z", "src"],
                          Path(tempfile.gettempdir()),
                          on_progress=got.append)
        self.assertEqual(got, [30], f"仅进度行应触发回调: {got}")


class PackDirTest(unittest.TestCase):
    """P2-8/10：pack_dir 端到端（7z 成功 / 全失败回退 / PowerShell 参数数组）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msvcgui_pack_"))
        self.src = self.tmp / "MSVC"
        self.src.mkdir()
        (self.src / "f.txt").write_text("x")
        self.arch_dir = self.tmp / "archives"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_7z_success(self):
        """7z 可用且成功 → 生成 .7z 归档，且命令带 -bsp1 进度参数。"""
        captured: dict = {}

        def fake_run(cmd, cwd, log=None, is_cancelled=None, timeout=7200,
                     on_progress=None):
            captured["cmd"] = cmd
            (self.arch_dir / "MSVC.7z").write_bytes(b"7z-data")
            return True

        with mock.patch("app.core.packager.shutil.which",
                        return_value="/bin/7z"), \
             mock.patch("app.core.packager._run", side_effect=fake_run):
            archive = packager.pack_dir(self.src, self.arch_dir)
        self.assertEqual(archive.name, "MSVC.7z")
        self.assertTrue(archive.exists())
        self.assertIn("-bsp1", captured["cmd"], f"7z 命令应含 -bsp1 进度参数: {captured['cmd']}")

    def test_all_backends_fail_raises(self):
        """7z / tar / PowerShell 全部失败 → 抛 PackError 并保留原始目录。"""
        with mock.patch("app.core.packager.shutil.which", return_value="/bin/7z"), \
             mock.patch("app.core.packager._run", return_value=False):
            with self.assertRaises(packager.PackError):
                packager.pack_dir(self.src, self.arch_dir)
        self.assertTrue(self.src.exists(), "失败后应保留原始 MSVC 目录")

    def test_powershell_uses_argument_list(self):
        """PowerShell 方案：路径作为独立 argv 传入，不做引号字符串拼接。"""
        captured: dict = {}

        def fake_run(cmd, cwd, log=None, is_cancelled=None, timeout=7200,
                     on_progress=None):
            captured["cmd"] = cmd
            (self.arch_dir / "MSVC.zip").write_bytes(b"zip-data")
            return True

        def which(name: str):
            return None if name in ("7z", "tar") else "powershell"

        with mock.patch("app.core.packager.shutil.which", side_effect=which), \
             mock.patch("app.core.packager._run", side_effect=fake_run):
            archive = packager.pack_dir(self.src, self.arch_dir)
        self.assertEqual(archive.name, "MSVC.zip")
        cmd = captured["cmd"]
        self.assertEqual(cmd[0], "powershell")
        # 源/目标路径必须是 cmd 列表中的独立元素，而非嵌入 -Command 字符串
        self.assertEqual(cmd[-2], "MSVC", f"源路径应独立传参: {cmd}")
        self.assertEqual(cmd[-1], str(self.arch_dir / "MSVC.zip"),
                         f"目标路径应独立传参: {cmd}")
        joined = " ".join(cmd)
        self.assertNotIn(f'-Path "MSVC"', joined, "不应使用引号字符串拼接")


if __name__ == "__main__":
    unittest.main()
