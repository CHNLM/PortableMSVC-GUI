"""manifest 模块回归测试（P2-2/3：畸形 id 防御、insiders 死代码移除）。"""
from __future__ import annotations

import unittest

from app.core import manifest as manifest_mod
from app.core.manifest import VSCatalog


class CollectVersionsDefensiveTest(unittest.TestCase):
    """畸形包 id 不得导致 IndexError 崩溃。"""

    def _catalog(self, packages: dict) -> VSCatalog:
        obj = VSCatalog.__new__(VSCatalog)
        obj.packages = packages
        obj.msvc_versions = {}
        obj.sdk_versions = {}
        obj.preview = False
        return obj

    def test_malformed_ids_do_not_crash(self):
        obj = self._catalog({
            # 段数不足 / 版本段非数字 / 正常的 SDK 组件
            "microsoft.vc.tools.hostx64.targetx64.base": [{}],
            "microsoft.vc.14.42.tools.hostx64.targetx64.base": [{}],
            "microsoft.visualstudio.component.windows10sdk.26100": [{}],
        })
        obj._collect_versions()  # 不应抛异常
        self.assertIn("14.42", obj.msvc_versions)
        self.assertIn("26100", obj.sdk_versions)

    def test_preview_filtering(self):
        obj = self._catalog({
            "microsoft.vc.14.43.tools.hostx64.targetx64.base": [{}],
            "microsoft.vc.preview.tools.hostx64.targetx64": [{"version": "14.43.12345.0"}],
        })
        obj._collect_versions()
        # 预览版对应版本被剔除（14.43 来自 preview 包）
        self.assertNotIn("14.43", obj.msvc_versions)

    def test_preview_pkg_missing_version_field_no_crash(self):
        """P2-5：preview 包缺 version 字段（清单结构变化）时不崩溃。"""
        obj = self._catalog({
            "microsoft.vc.14.43.tools.hostx64.targetx64.base": [{}],
            "microsoft.vc.preview.tools.hostx64.targetx64": [{"no_version": 1}],
        })
        obj._collect_versions()  # 不应抛 KeyError / IndexError
        self.assertIn("14.43", obj.msvc_versions, "缺 version 字段时不应误删正式版本")

    def test_short_package_id_no_crash(self):
        """P3-5：包 id 段数不足（结构变化）时不崩溃，且不产生垃圾版本。"""
        obj = self._catalog({
            "microsoft.vc.tools.hostx64.targetx64.base": [{}],   # 段数不足
            "microsoft.vc.14.42.tools.hostx64.targetx64.base": [{}],
        })
        obj._collect_versions()
        self.assertNotIn("tools", obj.msvc_versions)
        self.assertIn("14.42", obj.msvc_versions)


class InsidersRemovedTest(unittest.TestCase):
    """P2-3：insiders 通道死代码已移除。"""

    def test_manifest_urls_are_plain_strings(self):
        for vs, url in manifest_mod.MANIFEST_URLS.items():
            self.assertIsInstance(url, str, f"{vs} 的清单 URL 应为字符串")
            self.assertNotIn("insiders", url)

    def test_catalog_signature_has_no_insiders(self):
        import inspect
        sig = inspect.signature(VSCatalog.__init__)
        self.assertNotIn("insiders", sig.parameters)


class VersionResolveTest(unittest.TestCase):
    """P2-4：版本号支持精确 / 完整版本号 / 主版本前缀模糊匹配。"""

    def _catalog(self) -> VSCatalog:
        obj = VSCatalog.__new__(VSCatalog)
        obj.packages = {}
        obj.msvc_versions = {
            "14.40": "microsoft.vc.14.40.tools.hostx64.targetx64.base",
            "14.42": "microsoft.vc.14.42.tools.hostx64.targetx64.base",
        }
        obj.sdk_versions = {
            "22621": "microsoft.visualstudio.component.windows11sdk.22621",
            "26100": "microsoft.visualstudio.component.windows11sdk.26100",
        }
        obj.preview = False
        return obj

    def test_exact_match(self):
        cat = self._catalog()
        display, _ = cat.resolve_msvc("14.42")
        self.assertEqual(display, "14.42")
        self.assertEqual(cat.resolve_sdk("26100"),
                         "microsoft.visualstudio.component.windows11sdk.26100")

    def test_empty_uses_latest(self):
        cat = self._catalog()
        display, _ = cat.resolve_msvc("")
        self.assertEqual(display, "14.42")
        self.assertEqual(cat.resolve_sdk(""),
                         "microsoft.visualstudio.component.windows11sdk.26100")

    def test_full_msvc_version_prefix_match(self):
        """输入完整版本号 14.42.34433 → 归一化为主版本 14.42。"""
        cat = self._catalog()
        display, pid = cat.resolve_msvc("14.42.34433")
        self.assertEqual(display, "14.42")
        self.assertIn("14.42", pid)

    def test_full_sdk_version_prefix_match(self):
        """输入完整 SDK 版本号 10.0.26100.0 → 归一化为 build 号 26100。"""
        cat = self._catalog()
        pid = cat.resolve_sdk("10.0.26100.0")
        self.assertEqual(pid, "microsoft.visualstudio.component.windows11sdk.26100")

    def test_major_version_unique_match(self):
        """主版本号前缀匹配唯一候选时返回。"""
        obj = self._catalog()
        obj.msvc_versions = {"14.42": obj.msvc_versions["14.42"]}
        display, _ = obj.resolve_msvc("14.4")
        self.assertEqual(display, "14.42")

    def test_ambiguous_prefix_raises(self):
        """前缀匹配到多个候选时报错而不是随机选一个。"""
        cat = self._catalog()
        with self.assertRaises(manifest_mod.ManifestError) as ctx:
            cat.resolve_msvc("14.4")  # 14.40 与 14.42 均匹配
        self.assertIn("多个候选", str(ctx.exception))

    def test_unknown_version_raises(self):
        cat = self._catalog()
        with self.assertRaises(manifest_mod.ManifestError):
            cat.resolve_msvc("99.99")
        with self.assertRaises(manifest_mod.ManifestError):
            cat.resolve_sdk("99999")

    def test_empty_versions_raises_manifest_error(self):
        """P1-1：版本集合为空时应抛 ManifestError，而非裸 ValueError。"""
        obj = VSCatalog.__new__(VSCatalog)
        obj.packages = {}
        obj.msvc_versions = {}
        obj.sdk_versions = {}
        obj.preview = False
        with self.assertRaises(manifest_mod.ManifestError) as ctx:
            obj.resolve_msvc("")
        self.assertIn("MSVC", str(ctx.exception))
        with self.assertRaises(manifest_mod.ManifestError) as ctx:
            obj.resolve_sdk("")
        self.assertIn("Windows SDK", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
