"""extractor 模块回归测试（重点：get_msi_cabs 的 cab 名提取）。"""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.core.extractor import (
    CancelledError,
    _run_process,
    extract_msi,
    extract_zip_contents,
    get_msi_cabs,
    move_windows_kits_up,
)


def _write_tmp(data: bytes) -> Path:
    """写临时 MSI 文件，返回路径（调用方负责 unlink）。"""
    f = tempfile.NamedTemporaryFile(suffix=".msi", delete=False)
    f.write(data)
    f.close()
    return Path(f.name)


class GetMsiCabsTest(unittest.TestCase):
    """验证 get_msi_cabs 对 ASCII / UTF-16 / 噪声 / 跨块边界的处理。"""

    def test_ascii_reference(self):
        path = _write_tmp(b"XX" + b"Installers/sdk_1.cab" + b"YY")
        self.assertIn("sdk_1.cab", set(get_msi_cabs(path)))
        path.unlink(missing_ok=True)

    def test_utf16_reference(self):
        """MSI 字符串表常用 UTF-16LE，必须能命中 '.\0c\0a\0b\0' 字形。"""
        path = _write_tmp(b"XX" + "disk2.cab".encode("utf-16-le") + b"YY")
        self.assertIn("disk2.cab", set(get_msi_cabs(path)))
        path.unlink(missing_ok=True)

    def test_noise_matches_filtered(self):
        """二进制噪声中出现的 .cab 序列不得产生带脏字符/超长的名字。"""
        # 300 个 Z（超长）+ 含文件系统非法字符 < > 的名字
        path = _write_tmp(b"\x00\xff\x01" + b"Z" * 300 + b".cab" + b"weird<name>.cab")
        names = set(get_msi_cabs(path))
        for n in names:
            self.assertRegex(n, r"^[A-Za-z0-9_\-\.]+\.cab$", f"含脏字符: {n}")
            self.assertLessEqual(len(n), 260, f"超长: {n}")
            self.assertNotIn("Z" * 10, n, f"超长噪声未被过滤: {n}")
        path.unlink(missing_ok=True)

    def test_path_prefix_stripped(self):
        """提取结果只保留文件名，不带目录前缀。"""
        path = _write_tmp(b"Installers/WindowsSdk_10.0.26100.0_1.cab")
        names = set(get_msi_cabs(path))
        self.assertIn("WindowsSdk_10.0.26100.0_1.cab", names)
        self.assertTrue(all("/" not in n and "\\" not in n for n in names))
        path.unlink(missing_ok=True)

    def test_cross_chunk_boundary(self):
        """文件名跨 1MiB 分块边界仍能命中（窗口重叠足够）。"""
        # 前一块接近满：让 marker 落在第二块的头部
        prefix = b"A" * (1 << 20)
        data = prefix[:-8] + "boundary.cab".encode("utf-16-le")
        path = _write_tmp(data)
        self.assertIn("boundary.cab", set(get_msi_cabs(path)))
        path.unlink(missing_ok=True)

    def test_duplicates_deduped(self):
        """同一 cab 多次引用时，去重由调用方 set 完成（此处验证不产生重复生成问题）。"""
        path = _write_tmp(b"a.cab" + b"\x00" + b"a.cab")
        self.assertEqual(len(set(get_msi_cabs(path))), 1)
        path.unlink(missing_ok=True)


class ExtractMsiCancelTest(unittest.TestCase):
    """P1-3：解包子进程（msiexec/msiextract）可被取消，且会终止子进程。"""

    def test_cancel_terminates_and_raises(self):
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None  # 永不结束，模拟阻塞的 msiexec
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] >= 2  # 第 2 次检查时触发取消

        with mock.patch("app.core.extractor.subprocess.Popen", return_value=fake_proc):
            with self.assertRaises(CancelledError):
                extract_msi(Path("C:/fake/x.msi"), Path("C:/fake/dest"),
                            is_cancelled=cancel)
        fake_proc.terminate.assert_called_once()
        fake_proc.wait.assert_called()

    def test_failed_process_raises_runtime_error(self):
        """子进程非零退出（如 msiexec 报错）→ extract_msi 抛 RuntimeError。"""
        fake_proc = mock.Mock()
        fake_proc.poll.side_effect = [None, 1]  # 第一次未结束，第二次退出码 1
        fake_proc.returncode = 1

        with mock.patch("app.core.extractor.subprocess.Popen", return_value=fake_proc):
            with self.assertRaises(RuntimeError):
                extract_msi(Path("C:/fake/x.msi"), Path("C:/fake/dest"))

    def test_run_process_timeout_terminates(self):
        """P1-4：_run_process 超时必须终止子进程并返回，不无限挂起。"""
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        fake_proc.returncode = None  # 被终止后无退出码

        with mock.patch("app.core.extractor.subprocess.Popen", return_value=fake_proc):
            rc, _out = _run_process(["sleep", "999"], timeout=1)
        self.assertEqual(rc, 0)  # returncode 为 None 时按 0 处理
        fake_proc.terminate.assert_called_once()

    @unittest.skipUnless(__import__("sys").platform == "win32", "仅 Windows 使用 msiexec")
    def test_msiexec_targetdir_ends_with_backslash(self):
        """P3-5：TARGETDIR 应以反斜杠结尾（msiexec 惯例），避免路径拼接异常。"""
        fake_proc = mock.Mock()
        fake_proc.poll.side_effect = [None, 0]  # 第一次运行中，第二次正常结束
        fake_proc.returncode = 0

        with mock.patch("app.core.extractor.subprocess.Popen", return_value=fake_proc) as mp:
            extract_msi(Path("C:/fake/x.msi"), Path("C:/fake/dest"))
        cmd = mp.call_args.args[0]
        tdir = next(a for a in cmd if a.startswith("TARGETDIR="))
        self.assertTrue(tdir.endswith("\\"), f"TARGETDIR 应以反斜杠结尾: {tdir!r}")
        self.assertNotIn("TARGETDIR=C:/fake/dest\"", " ".join(cmd))


class ExtractZipCancelTest(unittest.TestCase):
    """P2-7：zip 解包支持取消（大 zip 解包期间不再无法中断）。"""

    def test_cancel_raises_before_write(self):
        with tempfile.TemporaryDirectory() as td:
            zpath = Path(td) / "a.zip"
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("Contents/a.txt", b"1")
                z.writestr("Contents/b.txt", b"2")
            dest = Path(td) / "out"
            with self.assertRaises(CancelledError):
                extract_zip_contents(zpath, dest, is_cancelled=lambda: True)
            self.assertFalse(dest.exists(), "取消时应尚未写入任何文件")

    def test_cancel_after_first_file(self):
        with tempfile.TemporaryDirectory() as td:
            zpath = Path(td) / "a.zip"
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("Contents/a.txt", b"1")
                z.writestr("Contents/b.txt", b"2")
            dest = Path(td) / "out"
            calls = {"n": 0}

            def cancel():
                calls["n"] += 1
                return calls["n"] >= 2  # 第一个文件写完后取消

            with self.assertRaises(CancelledError):
                extract_zip_contents(zpath, dest, is_cancelled=cancel)
            self.assertTrue((dest / "a.txt").exists())
            self.assertFalse((dest / "b.txt").exists())


class ExtractZipDefensiveTest(unittest.TestCase):
    """P2-3 / P2-6 / P3-1：目录条目、非 zip 校验、炸弹阈值防护。"""

    def test_directory_entry_creates_dir_not_empty_file(self):
        """目录条目（以 / 结尾）应生成目录，而非 0 字节空文件。"""
        with tempfile.TemporaryDirectory() as td:
            zpath = Path(td) / "a.zip"
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("Contents/dir/", b"")          # 目录条目
                z.writestr("Contents/dir/file.txt", b"hi")  # 目录内文件
            dest = Path(td) / "out"
            extract_zip_contents(zpath, dest)
            self.assertTrue((dest / "dir").is_dir(), "目录条目应创建目录")
            self.assertEqual((dest / "dir" / "file.txt").read_text(), "hi")

    def test_not_a_zip_raises(self):
        """非 zip 文件（如误下载的 MSI/CAB）应明确报错，而非底层 BadZipFile。"""
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "not_a_zip.zip"
            fake.write_bytes(b"MZ\x90\x00not a zip at all")
            with self.assertRaises(RuntimeError) as ctx:
                extract_zip_contents(fake, Path(td) / "out")
            self.assertIn("不是有效的 zip", str(ctx.exception))

    def test_too_many_entries_raises(self):
        """条目数超过阈值（zip 炸弹）应中止。"""
        from app.core import extractor as extractor_mod
        with tempfile.TemporaryDirectory() as td:
            zpath = Path(td) / "bomb.zip"
            with zipfile.ZipFile(zpath, "w") as z:
                for i in range(5):
                    z.writestr(f"Contents/f{i}.txt", b"x")
            dest = Path(td) / "out"
            original = extractor_mod.MAX_ZIP_ENTRIES
            extractor_mod.MAX_ZIP_ENTRIES = 3  # 阈值调小以便测试
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    extract_zip_contents(zpath, dest)
                self.assertIn("异常过大", str(ctx.exception))
            finally:
                extractor_mod.MAX_ZIP_ENTRIES = original


class FlattenProgramFilesTest(unittest.TestCase):
    """P2-8：Program Files 目录提升（顺带回归 P1 误删的函数已恢复）。"""

    def test_flatten_moves_files_up(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            kits = root / "Program Files" / "Windows Kits" / "10" / "bin"
            kits.mkdir(parents=True)
            (kits / "x.txt").write_text("hi")
            move_windows_kits_up(root)
            self.assertTrue((root / "Windows Kits" / "10" / "bin" / "x.txt").exists())
            self.assertFalse((root / "Program Files").exists(), "原 Program Files 树应被清理")

    def test_no_program_files_noop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "VC").mkdir()
            move_windows_kits_up(root)  # 不应抛错、不应误删
            self.assertTrue((root / "VC").exists())


if __name__ == "__main__":
    unittest.main()
