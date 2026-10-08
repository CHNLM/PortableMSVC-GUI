"""pipeline 模块 P0 修复回归测试。

覆盖：
- _prepare_output：重复执行前旧 MSVC 目录备份、新目录重建；
- _download_sdk：cab 以清单 payloads 为权威来源下载，MSI 引用交叉校验，
  清单缺失的引用降级跳过，且无 cab 时中断报错。
"""
from __future__ import annotations

import base64
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.models import CancelledError, PipelineConfig
from app.core import pipeline as pipeline_mod
from app.core.pipeline import Pipeline


def _make_pipeline(tmp: Path) -> Pipeline:
    cfg = PipelineConfig(output_dir=tmp, targets=["x64"])
    return Pipeline(cfg)


class PrepareOutputTest(unittest.TestCase):
    """P0-2：重复执行不清理旧目录 → 备份重建。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msvcgui_p0_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_existing_dir_creates(self):
        p = _make_pipeline(self.tmp)
        p._prepare_output()
        self.assertTrue(p.msvc_dir.is_dir())
        self.assertEqual(list(self.tmp.glob("MSVC.bak-*")), [])

    def test_existing_dir_backed_up(self):
        p = _make_pipeline(self.tmp)
        p.msvc_dir.mkdir(parents=True)
        (p.msvc_dir / "stale.txt").write_text("old-content")
        p._prepare_output()
        # 旧目录被整体备份，内容保留
        backups = list(self.tmp.glob("MSVC.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "stale.txt").read_text(), "old-content")
        # 新目录已重建且为空
        self.assertTrue(p.msvc_dir.is_dir())
        self.assertEqual(list(p.msvc_dir.iterdir()), [])

    def test_twice_yields_two_backups(self):
        """连续两次 _prepare_output 应各生成一个备份，不互相覆盖。"""
        p = _make_pipeline(self.tmp)
        p.msvc_dir.mkdir(parents=True)
        (p.msvc_dir / "a.txt").write_text("1")
        p._prepare_output()
        (p.msvc_dir / "b.txt").write_text("2")
        p._prepare_output()
        backups = sorted(self.tmp.glob("MSVC.bak-*"))
        self.assertEqual(len(backups), 2)
        self.assertEqual((backups[0] / "a.txt").read_text(), "1")
        self.assertEqual((backups[1] / "b.txt").read_text(), "2")

    def test_third_run_keeps_only_latest_two(self):
        """P2-5：第三次执行时清理最旧备份，只保留最近 2 份。"""
        p = _make_pipeline(self.tmp)
        p.msvc_dir.mkdir(parents=True)
        (p.msvc_dir / "a.txt").write_text("1")
        p._prepare_output()
        (p.msvc_dir / "b.txt").write_text("2")
        p._prepare_output()
        (p.msvc_dir / "c.txt").write_text("3")
        p._prepare_output()
        backups = sorted(self.tmp.glob("MSVC.bak-*"))
        self.assertEqual(len(backups), 2, f"应只保留 2 份备份: {backups}")
        # 最新备份包含第三次的内容
        latest = max(backups, key=lambda b: b.stat().st_mtime)
        self.assertEqual((latest / "c.txt").read_text(), "3")

    def test_downloads_dir_untouched(self):
        """备份只影响 MSVC 目录，downloads 缓存保留（断点续传）。"""
        p = _make_pipeline(self.tmp)
        p.msvc_dir.mkdir(parents=True)
        p.downloads_dir.mkdir(parents=True)
        (p.downloads_dir / "cache.part").write_bytes(b"x")
        p._prepare_output()
        self.assertTrue((p.downloads_dir / "cache.part").exists())


# ------------------------------------------------------------------ SDK cab 下载

SDK_PKG = {
    "dependencies": {},
    "payloads": [
        {"fileName": "Installers/Windows SDK for Windows Store Apps Tools-x86_en-us.msi",
         "url": "https://x/1.msi", "sha256": "a" * 64},
        {"fileName": "Installers/sdk_cab1.cab", "url": "https://x/c1", "sha256": "b" * 64},
        {"fileName": "Installers/sdk_cab2.cab", "url": "https://x/c2", "sha256": "c" * 64},
    ],
}


class _FakeCatalog:
    def __init__(self, pkg: dict):
        self._pkg = pkg

    def package(self, pid, lang=None):
        return self._pkg


class SdkCabDownloadTest(unittest.TestCase):
    """P0-1：cab 以清单 payloads 为权威来源，MSI 引用仅交叉校验。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msvcgui_p0_"))
        self.p = _make_pipeline(self.tmp)
        self.p._prepare_output()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cabs_from_manifest_downloaded(self):
        """清单中的 cab payload 全部下载；MSI 引用但清单缺失者跳过并告警。"""
        downloaded: list[str] = []

        def fake_download(url, dest, sha, on_progress=None, is_cancelled=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x")
            downloaded.append(dest.name)
            return dest

        with mock.patch("app.core.pipeline.download_to_file", side_effect=fake_download), \
             mock.patch("app.core.pipeline.get_msi_cabs",
                        return_value=["sdk_cab2.cab", "ghost.cab"]), \
             mock.patch("app.core.pipeline.extract_msi"), \
             mock.patch("app.core.pipeline.move_windows_kits_up"):
            self.p._download_sdk(_FakeCatalog(SDK_PKG), "sdk")

        self.assertIn("sdk_cab1.cab", downloaded)
        self.assertIn("sdk_cab2.cab", downloaded)
        self.assertNotIn("ghost.cab", downloaded)  # 清单缺失 → 跳过
        self.assertIn("Windows SDK for Windows Store Apps Tools-x86_en-us.msi", downloaded)

    def test_msi_scan_failure_does_not_break_cab_download(self):
        """即使 get_msi_cabs 提取失败（返回空），清单枚举仍能拿到全部 cab。"""
        downloaded: list[str] = []

        def fake_download(url, dest, sha, on_progress=None, is_cancelled=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x")
            downloaded.append(dest.name)
            return dest

        with mock.patch("app.core.pipeline.download_to_file", side_effect=fake_download), \
             mock.patch("app.core.pipeline.get_msi_cabs", return_value=[]), \
             mock.patch("app.core.pipeline.extract_msi"), \
             mock.patch("app.core.pipeline.move_windows_kits_up"):
            self.p._download_sdk(_FakeCatalog(SDK_PKG), "sdk")

        self.assertIn("sdk_cab1.cab", downloaded)
        self.assertIn("sdk_cab2.cab", downloaded)

    def test_no_cab_in_manifest_raises(self):
        """清单与 MSI 都找不到任何 cab 时中断，而不是静默继续产生残缺 SDK。"""
        pkg_no_cab = {
            "dependencies": {},
            "payloads": [
                {"fileName": "Installers/foo.msi", "url": "u", "sha256": "a" * 64},
            ],
        }
        with mock.patch("app.core.pipeline.download_to_file"), \
             mock.patch("app.core.pipeline.get_msi_cabs", return_value=[]):
            with self.assertRaises(RuntimeError):
                self.p._download_sdk(_FakeCatalog(pkg_no_cab), "sdk")

    def test_cab_download_reports_progress(self):
        """P1-2：cab 下载期间必须上报进度（80-84 区间），进度条不再卡死 80%。"""
        progress: list[int] = []
        cfg = PipelineConfig(output_dir=self.tmp, targets=["x64"])
        p = Pipeline(cfg, on_progress=progress.append)
        p._prepare_output()

        def fake_download(url, dest, sha, on_progress=None, is_cancelled=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x")
            if on_progress:
                on_progress(50.0)  # 模拟下载到一半
            return dest

        with mock.patch("app.core.pipeline.download_to_file", side_effect=fake_download), \
             mock.patch("app.core.pipeline.get_msi_cabs", return_value=[]), \
             mock.patch("app.core.pipeline.extract_msi"), \
             mock.patch("app.core.pipeline.move_windows_kits_up"):
            p._download_sdk(_FakeCatalog(SDK_PKG), "sdk")

        self.assertTrue(any(80 <= v < 84 for v in progress),
                        f"未在 80-84 区间上报进度: {progress}")

    def test_missing_sdk_pkg_raises(self):
        """P1-2：SDK 包缺失必须中断（raise），而不是告警后继续执行。"""
        class _NoPkgCatalog:
            def package(self, pid, lang=None):
                return None

        with self.assertRaises(RuntimeError) as ctx:
            self.p._download_sdk(_NoPkgCatalog(), "sdk-missing-pid")
        self.assertIn("Windows SDK", str(ctx.exception))
        self.assertIn("sdk-missing-pid", str(ctx.exception))


# ------------------------------------------------------------------ 脚本渲染

class SetupBatRenderTest(unittest.TestCase):
    """P1-1：卸载脚本不得 DeleteValue 整条环境变量；渲染参数正确替换。"""

    def test_uninstall_bat_protects_path_vars(self):
        """P1-1：卸载脚本对 PATH/INCLUDE/LIB 等路径型变量用 SetValue 而非
        DeleteValue（防止误删导致系统命令不可用）；DeleteValue 仅允许用于
        WindowsSDKVersion 的精确匹配删除（该变量不能留空值）。"""
        self.assertIn("SetValue", pipeline_mod._PS_UNINSTALL)
        self.assertIn("ExpandString", pipeline_mod._PS_UNINSTALL)
        # 路径型变量处理：按前缀过滤 + SetValue（安全），不 DeleteValue
        self.assertIn("Remove-EnvPath 'PATH'", pipeline_mod._PS_UNINSTALL)
        # WindowsSDKVersion：精确匹配后 DeleteValue（干净删除，不留空值）
        self.assertIn("Remove-EnvValue 'WindowsSDKVersion'", pipeline_mod._PS_UNINSTALL)
        self.assertIn("DeleteValue", pipeline_mod._PS_UNINSTALL)

    def test_uninstall_bat_renders_valid_encoded_command(self):
        content = pipeline_mod._render_env_uninstall_bat("x64", "10.0.26100.0")
        self.assertIn("powershell", content)
        self.assertIn("EncodedCommand", content)
        self.assertNotIn("DeleteValue", content)  # bat 外层只负责调用，逻辑全在 PS 内

    def test_setup_bat_renders_versions(self):
        content = pipeline_mod._render_setup_bat("x64", "x86", "14.42", "10.0.26100.0")
        self.assertIn('set "VCToolsVersion=14.42"', content)
        self.assertIn('set "WindowsSDKVersion=10.0.26100.0\\"', content)
        # 与真实工具链目录一致：bin\Hostx64（大写 H 小写 x）
        self.assertIn(r"bin\Hostx64\x86", content)

    def test_setup_bat_paths_are_quoted(self):
        """P2-9：所有 set 赋值用引号包裹，路径含 & 等 cmd 元字符时不被错误解析。"""
        for name, content in [
            ("setup", pipeline_mod._render_setup_bat("x64", "x86", "14.42", "10.0.26100.0")),
            ("install", pipeline_mod._render_env_install_bat("x64", "x86", "14.42", "10.0.26100.0")),
            ("uninstall", pipeline_mod._render_env_uninstall_bat("x86", "10.0.26100.0")),
        ]:
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("set ") and "=" in line:
                    self.assertTrue(line.startswith('set "'), f"{name}: 未加引号 -> {line}")
                    self.assertTrue(line.endswith('"'), f"{name}: 未闭合引号 -> {line}")

    def test_config_default_pack_level(self):
        cfg = PipelineConfig(output_dir=Path("."))
        self.assertEqual(cfg.pack_level, 5)
        self.assertEqual(cfg.validate(), [])

    def test_pack_level_out_of_range_invalid(self):
        """P2-7：压缩级别超出 1-9 应在校验阶段被拦截。"""
        for bad in (0, 10, -1):
            cfg = PipelineConfig(output_dir=Path("."), pack_level=bad)
            self.assertIn("压缩级别", "；".join(cfg.validate()),
                          f"pack_level={bad} 应校验失败")

    def test_env_scripts_broadcast_setting_change(self):
        """P2-5：安装/卸载脚本写完注册表后必须广播 WM_SETTINGCHANGE。"""
        self.assertIn("SendMessageTimeout", pipeline_mod._PS_INSTALL)
        self.assertIn("SendMessageTimeout", pipeline_mod._PS_UNINSTALL)
        self.assertIn("0x001A", pipeline_mod._PS_INSTALL)  # WM_SETTINGCHANGE
        self.assertIn("0x001A", pipeline_mod._PS_UNINSTALL)

    def test_uninstall_render_signature(self):
        """P2-4：卸载脚本渲染需要 sdkv（精确匹配 WindowsSDKVersion 删除值）。"""
        import inspect
        sig = inspect.signature(pipeline_mod._render_env_uninstall_bat)
        self.assertEqual(list(sig.parameters), ["target", "sdkv"])

    def test_install_bat_writes_sdk_and_toolchain_vars(self):
        """P1-3：安装脚本必须写入 WindowsSDKVersion（Nuitka 判定 SDK 的关键变量）
        以及 VCToolsInstallDir / WindowsSdkBinPath，否则打包工具探测不到 SDK。"""
        content = pipeline_mod._render_env_install_bat("x64", "x86", "14.42", "10.0.26100.0")
        enc = re.search(r"EncodedCommand ([A-Za-z0-9+/=]+)", content).group(1)
        ps = base64.b64decode(enc).decode("utf-16-le")
        self.assertIn("Set-EnvValue 'WindowsSDKVersion' ('10.0.26100.0\\')", ps)
        self.assertIn("Set-EnvValue 'VCToolsInstallDir'", ps)
        self.assertIn("Set-EnvValue 'WindowsSdkBinPath'", ps)

    def test_uninstall_bat_matches_sdk_version_exactly(self):
        """P1-3：卸载脚本按 __SDKV__\\ 精确匹配删除 WindowsSDKVersion，
        不会误删其它 SDK 版本的值，也不会留下空值（Nuitka int('') 会崩溃）。"""
        content = pipeline_mod._render_env_uninstall_bat("x64", "10.0.26100.0")
        enc = re.search(r"EncodedCommand ([A-Za-z0-9+/=]+)", content).group(1)
        ps = base64.b64decode(enc).decode("utf-16-le")
        self.assertIn("Remove-EnvValue 'WindowsSDKVersion' ('10.0.26100.0\\')", ps)
        self.assertIn("Remove-EnvPath 'VCToolsInstallDir'", ps)
        self.assertIn("Remove-EnvPath 'WindowsSdkBinPath'", ps)


# ------------------------------------------------------------------ P1-2 关键包缺失

class CriticalMsvcPkgTest(unittest.TestCase):
    """P1-2：关键 MSVC 包缺失必须中断，可选包缺失仅告警。"""

    def test_critical_classification(self):
        is_crit = pipeline_mod._is_critical_msvc_pkg
        # 关键：编译器本体 / CRT 公共头 / 桌面 CRT 库
        self.assertTrue(is_crit("microsoft.vc.14.42.tools.hostx64.targetx64.base"))
        self.assertTrue(is_crit("microsoft.vc.14.42.tools.hostx64.targetarm64.base"))
        self.assertTrue(is_crit("microsoft.vc.14.42.crt.headers.base"))
        self.assertTrue(is_crit("microsoft.vc.14.42.crt.x64.desktop.base"))
        self.assertTrue(is_crit("microsoft.vc.14.42.crt.arm.desktop.base"))
        # 可选：premium 变体 / res / asan / pgo / store / redist / source
        self.assertFalse(is_crit("microsoft.vc.14.42.premium.tools.hostx64.targetx64.base"))
        self.assertFalse(is_crit("microsoft.vc.14.42.tools.hostx64.targetx64.res.base"))
        self.assertFalse(is_crit("microsoft.vc.14.42.asan.x64.base"))
        self.assertFalse(is_crit("microsoft.vc.14.42.pgo.x64.base"))
        self.assertFalse(is_crit("microsoft.vc.14.42.crt.x64.store.base"))
        self.assertFalse(is_crit("microsoft.vc.14.42.crt.redist.x64.base"))
        self.assertFalse(is_crit("microsoft.vc.14.42.crt.source.base"))
        self.assertFalse(is_crit("microsoft.vc.preview.tools.hostx64.targetx64"))

    def _run_download_msvc(self, missing: set[str], empty_payloads: set[str] | None = None):
        """执行 _download_msvc（mock 网络与解包），返回实际请求的包 id 列表。"""
        cfg = PipelineConfig(output_dir=Path(tempfile.mkdtemp(prefix="msvcgui_p12_")),
                             targets=["x64"])
        p = Pipeline(cfg)
        p._prepare_output()

        catalog = _FakeMsvcCatalog(missing, empty_payloads)
        requested: list[str] = []

        def fake_download(url, dest, sha, on_progress=None, is_cancelled=None):
            requested.append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x")
            return dest

        with mock.patch("app.core.pipeline.download_to_file", side_effect=fake_download), \
             mock.patch("app.core.pipeline.extract_zip_contents"):
            p._download_msvc(catalog, "14.42")
        return requested

    def test_missing_critical_raises(self):
        missing = {"microsoft.vc.14.42.tools.hostx64.targetx64.base"}
        with self.assertRaises(RuntimeError) as ctx:
            self._run_download_msvc(missing)
        self.assertIn("tools.hostx64.targetx64.base", str(ctx.exception))

    def test_missing_critical_headers_raises(self):
        missing = {"microsoft.vc.14.42.crt.headers.base"}
        with self.assertRaises(RuntimeError):
            self._run_download_msvc(missing)

    def test_missing_optional_does_not_raise(self):
        missing = {"microsoft.vc.14.42.asan.x64.base",
                   "microsoft.vc.14.42.pgo.x64.base"}
        requested = self._run_download_msvc(missing)  # 不应抛错
        self.assertTrue(requested, "可选包缺失时其余包仍应下载")

    def test_missing_critical_and_optional_reports_critical_only(self):
        missing = {"microsoft.vc.14.42.crt.x64.desktop.base",
                   "microsoft.vc.14.42.asan.x64.base"}
        with self.assertRaises(RuntimeError) as ctx:
            self._run_download_msvc(missing)
        self.assertIn("crt.x64.desktop.base", str(ctx.exception))
        self.assertNotIn("asan", str(ctx.exception))

    def test_critical_pkg_with_empty_payloads_raises(self):
        """P2-1：关键包存在但 payloads 为空（清单结构异常）→ 同样视为缺失并中断。"""
        empty = {"microsoft.vc.14.42.tools.hostx64.targetx64.base"}
        with self.assertRaises(RuntimeError) as ctx:
            self._run_download_msvc(set(), empty_payloads=empty)
        self.assertIn("tools.hostx64.targetx64.base", str(ctx.exception))

    def test_optional_pkg_with_empty_payloads_skipped(self):
        """P2-1：可选包存在但 payloads 为空 → 告警跳过，不中断其余下载。"""
        empty = {"microsoft.vc.14.42.asan.x64.base"}
        requested = self._run_download_msvc(set(), empty_payloads=empty)
        self.assertTrue(requested, "可选包空 payload 不应中断其余包下载")

    def test_pack_progress_maps_to_92_100(self):
        """P2-2：打包进度（7z 0-100）映射到流水线 92-100 区间，单调推进。"""
        progress: list[int] = []
        p = Pipeline(PipelineConfig(output_dir=Path(".")),
                     on_progress=progress.append)
        mapper = lambda pct: p._progress(92 + pct * 8.0 / 100.0)  # noqa: E731
        mapper(0)
        mapper(50)
        mapper(100)
        self.assertEqual(progress, [92, 96, 100], f"映射应落在 92-100: {progress}")


class _FakeMsvcCatalog:
    """仅用于 _download_msvc 测试：可按 id 模拟缺失包或空 payload 包。"""

    def __init__(self, missing: set[str], empty_payloads: set[str] | None = None):
        self.packages: dict[str, list[dict]] = {}
        self._missing = missing
        self._empty = empty_payloads or set()

    def package(self, pid, lang=None):
        if pid in self._missing:
            return None
        if pid in self._empty:
            return {"payloads": []}  # 包存在但无 payload（清单结构异常）
        return {"payloads": [{"fileName": "pkg.zip", "url": "https://x/" + pid,
                              "sha256": "a" * 64}]}


# ------------------------------------------------------------------ P1-3 DIA SDK 目录

def _make_post_process_env(tmp: Path, host: str = "x64",
                           targets: list[str] | None = None) -> Pipeline:
    """构造 _post_process 可运行的最小目录树（MSVC 14.42 + SDK 10.0.26100.0）。"""
    cfg = PipelineConfig(output_dir=tmp, host=host, targets=targets or ["x64"])
    p = Pipeline(cfg)
    (p.msvc_dir / "VC/Tools/MSVC/14.42").mkdir(parents=True)
    (p.msvc_dir / "Windows Kits/10/bin/10.0.26100.0").mkdir(parents=True)
    return p


class DiaSdkDeployTest(unittest.TestCase):
    """P1-3：DIA SDK 目录名兼容（DIA SDK / DIA%20SDK 均可识别）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msvcgui_dia_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_space_name_deployed(self):
        """目录名为 'DIA SDK'（空格）时仍能部署 msdia140.dll 并清理。"""
        p = _make_post_process_env(self.tmp)
        dia = p.msvc_dir / "DIA SDK/bin/amd64"
        dia.mkdir(parents=True)
        (dia / "msdia140.dll").write_bytes(b"DIA-SPACE")
        p._post_process("14.42")
        dst = p.msvc_dir / "VC/Tools/MSVC/14.42/bin/Hostx64/x64/msdia140.dll"
        self.assertTrue(dst.exists(), "msdia140.dll 应被复制到 bin")
        self.assertEqual(dst.read_bytes(), b"DIA-SPACE")
        self.assertFalse((p.msvc_dir / "DIA SDK").exists(), "DIA SDK 目录应被清理")

    def test_encoded_name_deployed(self):
        """目录名为 'DIA%20SDK'（URL 编码）时仍能部署（兼容旧行为）。"""
        p = _make_post_process_env(self.tmp)
        dia = p.msvc_dir / "DIA%20SDK/bin/amd64"
        dia.mkdir(parents=True)
        (dia / "msdia140.dll").write_bytes(b"DIA-ENCODED")
        p._post_process("14.42")
        dst = p.msvc_dir / "VC/Tools/MSVC/14.42/bin/Hostx64/x64/msdia140.dll"
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_bytes(), b"DIA-ENCODED")
        self.assertFalse((p.msvc_dir / "DIA%20SDK").exists())

    def test_no_dia_dir_logs_warning(self):
        """没有 DIA SDK 目录时不应崩溃，且应有日志告警。"""
        p = _make_post_process_env(self.tmp)
        logs: list[str] = []
        p.on_log = logs.append
        p._post_process("14.42")
        self.assertTrue(any("DIA" in line for line in logs),
                        f"缺少 DIA 告警日志: {logs}")

    def test_dia_dir_without_dll_no_crash(self):
        """DIA SDK 目录存在但缺 msdia140.dll 时不崩溃，目录被清理。"""
        p = _make_post_process_env(self.tmp)
        (p.msvc_dir / "DIA SDK/bin/amd64").mkdir(parents=True)
        logs: list[str] = []
        p.on_log = logs.append
        p._post_process("14.42")
        self.assertTrue(any("msdia140.dll" in line for line in logs))
        self.assertFalse((p.msvc_dir / "DIA SDK").exists())


# ------------------------------------------------------------------ P1-4 取消与进度

class PostProcessCancelProgressTest(unittest.TestCase):
    """P1-4：整理瘦身阶段可取消，且进度推进到 90。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msvcgui_pp_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cancel_raises_cancelled(self):
        p = _make_post_process_env(self.tmp)
        p.is_cancelled = lambda: True
        with self.assertRaises(CancelledError):
            p._post_process("14.42")

    def test_progress_reaches_90(self):
        progress: list[int] = []
        p = _make_post_process_env(self.tmp)
        p.on_progress = progress.append
        p._post_process("14.42")
        self.assertEqual(progress[-1], 90, f"末次进度应为 90: {progress}")
        self.assertTrue(all(86 <= v <= 90 for v in progress),
                        f"进度应在 86-90 区间: {progress}")

    def test_run_advances_to_92_after_setup_bats(self):
        """run() 在生成环境脚本后应将进度推进到 92（不再卡 86）。"""
        progress: list[int] = []
        p = _make_post_process_env(self.tmp)
        p.on_progress = progress.append
        result = pipeline_mod.PipelineResult()
        p._write_setup_bats("14.42", result)
        p._progress(92)
        self.assertEqual(progress[-1], 92)
        self.assertTrue(result.setup_bats, "应生成环境脚本")


if __name__ == "__main__":
    unittest.main()
