"""downloader 模块回归测试（P2-1：校验失败重试、缓存命中；P1-1：416 续传）。"""
from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from app.core.common import sanitize_url
from app.core.downloader import download_to_file


class _Ctx:
    """模拟 urllib 响应（支持 with 与流式 read）。"""

    def __init__(self, data: bytes, status: int = 200):
        self.status = status
        self.headers = {}
        self._buf = io.BytesIO(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n: int = -1):
        return self._buf.read(n)


class _Err416:
    """模拟 HTTPError 的 body（仅需 read/close）。"""

    def __init__(self):
        self.code = 416

    def __str__(self):
        return "HTTP Error 416: Range Not Satisfiable"

    def read(self):
        return b""

    def close(self):
        pass


def _raise_416(req, context=None, timeout=60.0):
    """总是抛 416（服务器拒绝 Range 续传）——用于 callable side_effect。"""
    raise urllib.error.HTTPError("https://x/f", 416, "Range Not Satisfiable",
                                 {}, _Err416())


def _http416():
    """返回 416 HTTPError 实例——用于 list side_effect（mock 遇异常实例会抛出）。"""
    return urllib.error.HTTPError("https://x/f", 416, "Range Not Satisfiable",
                                  {}, _Err416())


class DownloadRetryTest(unittest.TestCase):
    """P2-1：SHA256 校验失败必须丢弃 .part 并从头重下（而非直接抛错）。"""

    def test_sha_mismatch_retries_from_scratch(self):
        good = b"the-correct-content"
        good_sha = hashlib.sha256(good).hexdigest()
        bad = b"corrupted-bytes"  # 第一次传输损坏
        responses = [_Ctx(bad), _Ctx(good)]

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "f.bin"
            with mock.patch("app.core.downloader.urllib.request.urlopen",
                            side_effect=responses) as uo, \
                 mock.patch("app.core.downloader.time.sleep"):
                path = download_to_file("https://x/f.bin", dest, good_sha)
            self.assertEqual(path, dest)
            self.assertEqual(dest.read_bytes(), good)
            self.assertFalse(dest.with_name(dest.name + ".part").exists())
            # 校验失败后必须从头重下一次（共 2 次请求）
            self.assertEqual(uo.call_count, 2, "校验失败后应重试一次")

    def test_persistent_corruption_raises(self):
        good_sha = hashlib.sha256(b"expected").hexdigest()
        responses = [_Ctx(b"bad1"), _Ctx(b"bad2"), _Ctx(b"bad3"), _Ctx(b"bad4")]
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "f.bin"
            with mock.patch("app.core.downloader.urllib.request.urlopen", side_effect=responses), \
                 mock.patch("app.core.downloader.time.sleep"):
                with self.assertRaises(Exception) as ctx:
                    download_to_file("https://x/f.bin", dest, good_sha)
            self.assertIn("SHA256", str(ctx.exception))

    def test_cache_hit_skips_download(self):
        good = b"cached-content"
        sha = hashlib.sha256(good).hexdigest()
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "f.bin"
            dest.write_bytes(good)
            with mock.patch("app.core.downloader.urllib.request.urlopen") as uo:
                path = download_to_file("https://x/f.bin", dest, sha)
            self.assertEqual(path, dest)
            uo.assert_not_called()


class Http416ResumeTest(unittest.TestCase):
    """P1-1：.part 已完整但未改名时，服务器 416 不应导致下载失败。"""

    def test_complete_part_416_is_accepted(self):
        """part 哈希一致 → 416 时直接完成，不再发起任何请求。"""
        good = b"complete-part-content"
        sha = hashlib.sha256(good).hexdigest()
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "f.bin"
            part = Path(td) / "f.bin.part"
            part.write_bytes(good)  # 上次在改名之前中断：part 已完整
            with mock.patch("app.core.downloader.urllib.request.urlopen",
                            side_effect=_raise_416) as uo:
                path = download_to_file("https://x/f.bin", dest, sha)
            self.assertEqual(path, dest)
            self.assertEqual(dest.read_bytes(), good)
            self.assertFalse(part.exists(), "part 应已改名为正式文件")
            uo.assert_called_once()  # 仅尝试续传一次即成功

    def test_bad_part_416_restarts_from_scratch(self):
        """part 哈希不一致 → 416 后丢弃 part 从头下载，最终成功。"""
        good = b"fresh-content"
        sha = hashlib.sha256(good).hexdigest()
        responses = [_http416(), _Ctx(good)]  # 第一次 416，第二次全量 200
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "f.bin"
            part = Path(td) / "f.bin.part"
            part.write_bytes(b"stale-corrupt-part")  # 内容与目标哈希不符
            with mock.patch("app.core.downloader.urllib.request.urlopen",
                            side_effect=responses) as uo, \
                 mock.patch("app.core.downloader.time.sleep"):
                path = download_to_file("https://x/f.bin", dest, sha)
            self.assertEqual(path, dest)
            self.assertEqual(dest.read_bytes(), good)
            self.assertFalse(part.exists())
            self.assertEqual(uo.call_count, 2)

    def test_missing_part_416_restarts_from_scratch(self):
        """part 不存在（空下载目录）但服务器 416 → 从头下载。"""
        good = b"another-content"
        sha = hashlib.sha256(good).hexdigest()
        responses = [_http416(), _Ctx(good)]
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "f.bin"
            with mock.patch("app.core.downloader.urllib.request.urlopen",
                            side_effect=responses) as uo, \
                 mock.patch("app.core.downloader.time.sleep"):
                path = download_to_file("https://x/f.bin", dest, sha)
            self.assertEqual(path, dest)
            self.assertEqual(dest.read_bytes(), good)
            self.assertEqual(uo.call_count, 2)

    def test_416_does_not_consume_retry_budget(self):
        """416 分支不消耗网络错误重试次数：连续 416 后仍能全量重下。"""
        good = b"after-many-416s"
        sha = hashlib.sha256(good).hexdigest()
        # 3 次 416（若消耗 MAX_RETRIES=3 会失败）+ 1 次全量成功
        responses = [_http416(), _http416(), _http416(), _Ctx(good)]
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "f.bin"
            with mock.patch("app.core.downloader.urllib.request.urlopen",
                            side_effect=responses) as uo, \
                 mock.patch("app.core.downloader.time.sleep"):
                path = download_to_file("https://x/f.bin", dest, sha)
            self.assertEqual(path, dest)
            self.assertEqual(dest.read_bytes(), good)
            self.assertEqual(uo.call_count, 4)


class ProgressWithoutLengthTest(unittest.TestCase):
    """P2-3：服务器未提供 Content-Length 时进度须单调可见且不卡死/不越界。"""

    def test_estimate_monotonic_and_bounded(self):
        data = b"z" * (10 * 1024 * 1024)  # 10 MiB，无 Content-Length
        sha = hashlib.sha256(data).hexdigest()
        ctx = _Ctx(data)
        ctx.headers = {}  # 无 Content-Length / Content-Range
        progress: list[float] = []
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "f.bin"
            with mock.patch("app.core.downloader.urllib.request.urlopen",
                            return_value=ctx):
                download_to_file("https://x/f.bin", dest, sha,
                                 on_progress=progress.append)
        self.assertTrue(progress, "应上报进度")
        self.assertTrue(all(0 <= v < 100 for v in progress), f"进度越界: {progress}")
        # 单调不减（连续值允许相等，因为 int 取整可能重复）
        prev = -1
        for v in progress:
            self.assertGreaterEqual(v, prev - 1e-9, f"进度回退: {progress}")
            prev = v
        # 10 MiB 时应落在渐进曲线的合理区间（旧算法只会到 ~5%）
        self.assertGreater(progress[-1], 5.0, f"10MiB 进度应明显超过旧算法: {progress[-1]}")
        self.assertLess(progress[-1], 99.0)


class SanitizeUrlTest(unittest.TestCase):
    """清单 URL 含未编码空格等非法字符时须转义（本次 CI 失败回归用例）。"""

    def test_space_is_percent_encoded(self):
        raw = ("https://download.visualstudio.microsoft.com/download/pr/336fd7d6/Win11Sdk/"
               "July2026Servicing28000/w kits2/Installers/Universal%20CRT%20Headers-x86_en-us.msi")
        fixed = sanitize_url(raw)
        self.assertIn("/w%20kits2/", fixed)
        self.assertNotIn(" ", fixed)

    def test_existing_percent_encoding_not_double_encoded(self):
        raw = "https://x/a%20b/c"
        self.assertEqual(sanitize_url(raw), raw)

    def test_query_and_subdelims_preserved(self):
        raw = "https://x/p(a)+,;=?a=b&c=d"
        self.assertEqual(sanitize_url(raw), raw)

    def test_non_ascii_is_utf8_encoded(self):
        self.assertEqual(sanitize_url("https://x/中文 file.msi"),
                         "https://x/%E4%B8%AD%E6%96%87%20file.msi")

    def test_request_applies_sanitize(self):
        """download_to_file 内部发请求前应已把空格转义为 %20。"""
        good = b"ok"
        sha = hashlib.sha256(good).hexdigest()
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "f.bin"
            with mock.patch("app.core.downloader.urllib.request.urlopen",
                            return_value=_Ctx(good)) as uo:
                download_to_file("https://x/a b/f.bin", dest, sha)
        req = uo.call_args.args[0]
        self.assertEqual(req.full_url, "https://x/a%20b/f.bin")


if __name__ == "__main__":
    unittest.main()
