"""Visual Studio / Windows SDK 清单解析。

通过 aka.ms 下载 VS 通道清单（channel manifest），再定位 VS 功能清单
（Visual Studio manifest），从中收集可用的 MSVC 与 Windows SDK 版本，
并解析出具体包（package）的下载地址。
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

from .common import USER_AGENT, sanitize_url, version_key

MANIFEST_URLS = {
    "latest": "https://aka.ms/vs/stable/channel",
    "2026":   "https://aka.ms/vs/18/stable/channel",
    "2022":   "https://aka.ms/vs/17/release/channel",
    "2019":   "https://aka.ms/vs/16/release/channel",
}


class ManifestError(Exception):
    """清单获取或解析失败。"""


def _first(items, cond=lambda x: True, default=None):
    return next((item for item in items if cond(item)), default)


def _latest_version(versions) -> str:
    """从版本集合中取数值意义上的最新版本。"""
    return max(versions, key=version_key)


def _fetch_json(url: str, ssl_context=None, timeout: float = 60.0):
    # 清单 URL 来自上游字符串，可能含未编码非法字符，统一转义后再请求
    req = urllib.request.Request(sanitize_url(url), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, context=ssl_context, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def _load_with_cert_fallback(url: str):
    """下载 JSON；若遇证书校验失败则尝试用 certifi 的 Mozilla 根证书重试。"""
    try:
        return _fetch_json(url)
    except urllib.error.URLError as err:
        cause = err.args[0] if err.args else None
        if not isinstance(cause, ssl.SSLCertVerificationError):
            raise
        try:
            import certifi
        except ModuleNotFoundError:
            raise ManifestError(
                "SSL 证书校验失败，且未安装 certifi 包。请先 `pip install certifi`，"
                "或更新 Windows 受信任根证书。"
            ) from err
        ctx = ssl.create_default_context(cafile=certifi.where())
        return _fetch_json(url, ssl_context=ctx)


class VSCatalog:
    """封装 VS 通道清单与功能清单，提供版本枚举与包解析能力。"""

    def __init__(self, vs: str = "latest", preview: bool = False):
        self.vs = vs
        self.preview = preview
        channel_url = MANIFEST_URLS[vs]
        self.channel = _load_with_cert_fallback(channel_url)

        item_name = "Microsoft.VisualStudio.Manifests.VisualStudio"
        item = _first(self.channel["channelItems"], lambda x: x["id"] == item_name)
        if item is None:
            raise ManifestError(f"通道清单中找不到 {item_name}")
        vs_url = item["payloads"][0]["url"]
        self.vsmanifest = _load_with_cert_fallback(vs_url)

        # 按小写 id 建立包索引（一个 id 可能对应多个包，取第一个匹配语言者）
        self.packages: dict[str, list[dict]] = {}
        for p in self.vsmanifest["packages"]:
            self.packages.setdefault(p["id"].lower(), []).append(p)

        self.msvc_versions: dict[str, str] = {}   # 显示版本 -> 包 id
        self.sdk_versions: dict[str, str] = {}    # SDK 版本号 -> 包 id
        self._collect_versions()

    # ------------------------------------------------------------------ 版本收集
    def _collect_versions(self):
        for pid in self.packages:
            if (pid.startswith("microsoft.vc.")
                    and pid.endswith(".tools.hostx64.targetx64.base")
                    and "premium" not in pid):
                parts = pid.split(".")
                # 提取 "14.42"（段 2-3）；段数不足时为空串，被下方 isdigit 过滤
                pver = ".".join(parts[2:4]) if len(parts) >= 4 else ""
                if pver and pver[0].isnumeric():
                    self.msvc_versions[pver] = pid
            elif (pid.startswith("microsoft.visualstudio.component.windows10sdk.")
                  or pid.startswith("microsoft.visualstudio.component.windows11sdk.")):
                pver = pid.split(".")[-1]
                if pver.isnumeric():
                    self.sdk_versions[pver] = pid

        if not self.preview:
            # 非预览模式下，剔除预览版工具链对应的版本号
            preview_pkgs = self.packages.get("microsoft.vc.preview.tools.hostx64.targetx64", [])
            if preview_pkgs:
                # .get 防御：清单结构变化导致 preview 包缺 version 字段时不崩溃
                pver = ".".join((preview_pkgs[0].get("version") or "").split(".")[:2])
                if pver and pver[0].isnumeric():
                    self.msvc_versions.pop(pver, None)

    # ------------------------------------------------------------------ 版本解析
    def _resolve_version(self, versions: dict[str, str], version: str, label: str) -> str:
        """按精确 / 完整版本号 / 主版本前缀解析，返回包 id。

        - 空集合 → 抛 ManifestError（清单解析不到任何版本，避免 max() 裸 ValueError）；
        - 空串 → 数值最新版；
        - 精确命中（如 "14.42"）→ 直接返回；
        - 完整版本号（如 "14.42.34433"）→ 匹配其主版本前缀 "14.42"；
        - 主版本号前缀（如 "14.4"）→ 仅当候选唯一时返回，否则报错避免歧义。
        """
        if not versions:
            raise ManifestError(
                f"清单中未找到可用的 {label} 版本"
                "（可能是清单结构变化，或网络返回了异常数据）"
            )
        if not version:
            return versions[_latest_version(versions)]
        pid = versions.get(version)
        if pid is not None:
            return pid
        # 前缀匹配两条路径：
        #   v.startswith(version)      —— 输入为主版本前缀（"14.4" → "14.40"/"14.42"）
        #   version.startswith(v+".")  —— 输入为完整版本号（"14.42.34433" → "14.42"）
        cands = [v for v in versions
                 if v.startswith(version) or version.startswith(v + ".")]
        if len(cands) == 1:
            return versions[cands[0]]
        if len(cands) > 1:
            raise ManifestError(
                f"{label} {version} 匹配到多个候选，请指定完整主版本号: "
                + ", ".join(sorted(cands))
            )
        raise ManifestError(f"未知的 {label} 版本: {version}")

    def resolve_msvc(self, version: str = "") -> tuple[str, str]:
        """根据（可能为空的）版本号解析出 (显示版本, 完整包 id)。"""
        pid = self._resolve_version(self.msvc_versions, version, "MSVC")
        display = pid.removeprefix("microsoft.vc.").removesuffix(".tools.hostx64.targetx64.base")
        return display, pid

    def resolve_sdk(self, version: str = "") -> str:
        """根据（可能为空的）版本号解析出 SDK 包 id。

        支持完整版本号（"10.0.26100.0"）与 build 号（"26100"）两种写法。
        """
        if version.startswith("10.0.") and version.endswith(".0"):
            build = version.removeprefix("10.0.").removesuffix(".0")
            if build.isdigit():
                version = build  # 完整 SDK 版本号 → 归一化为 build 号
        return self._resolve_version(self.sdk_versions, version, "Windows SDK")

    # ------------------------------------------------------------------ 包查询
    def package(self, pid: str, lang: str | None = "en-US") -> dict | None:
        """按 id 取包；优先返回指定语言的变体（language 为 None 时返回第一个）。"""
        pkgs = self.packages.get(pid.lower())
        if not pkgs:
            return None
        return _first(pkgs, lambda p: p.get("language") in (None, lang), default=pkgs[0])

    def license_url(self) -> str:
        """返回 VS 产品许可协议地址。"""
        item = _first(self.channel["channelItems"],
                      lambda x: x["id"] == "Microsoft.VisualStudio.Product.BuildTools")
        if item is None:
            return "https://visualstudio.microsoft.com/license-terms/"
        resource = _first(item.get("localizedResources", []), lambda x: x["language"] == "en-us")
        return resource["license"] if resource else "https://visualstudio.microsoft.com/license-terms/"
