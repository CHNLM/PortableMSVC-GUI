"""整体流水线编排：清单解析 → MSVC 下载解包 → Windows SDK 下载解包 → 整理瘦身 → 生成环境脚本 → 打包。

本模块只依赖 core 内其它模块，不引入任何 GUI 框架。
进度通过 on_progress(0-100) 回调上报，日志通过 on_log(str) 上报，
取消通过 is_cancelled() 回调查询。
"""
from __future__ import annotations

import re
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .common import version_key
from .downloader import CancelledError, download_to_file
from .extractor import extract_msi, extract_zip_contents, get_msi_cabs, move_windows_kits_up
from .manifest import VSCatalog
from .models import PipelineConfig, PipelineResult
from .packager import pack_dir

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int], None]
CancelCheck = Callable[[], bool]

# ------------------------------------------------------------------ 环境脚本模板
# 占位符在 _render_setup_bat 中替换。bat 内容含 %~dp0 / %PATH% 与 PowerShell 的
# {} / $ / ; 等字符，因此不用 f-string / format，统一用 replace 占位符避免转义冲突。
# PowerShell 命令以 Base64(-EncodedCommand) 内嵌，彻底规避 cmd 对引号/分号的解析问题。
# 三个文件：setup_<arch>.bat（会话级）、setup_<arch>_install.bat（永久安装用户环境变量）、
#           setup_<arch>_uninstall.bat（从用户环境变量移除）。
SETUP_BAT_TEMPLATE = r"""@echo off
rem ================================================================
rem  Portable MSVC toolchain environment script (target: __TARGET__)
rem  Usage:
rem    setup_<arch>.bat                set env for the current cmd only
rem    setup_<arch>_install.bat        permanently add to current user env
rem    setup_<arch>_uninstall.bat      remove toolchain paths from user env
rem ================================================================
rem  Note: all "set" assignments are double-quoted so that paths
rem  containing cmd metacharacters (& ^ | < >) are not mis-parsed.

set "VSCMD_ARG_HOST_ARCH=__HOST__"
set "VSCMD_ARG_TGT_ARCH=__TARGET__"

set "VCToolsVersion=__MSVCV__"
set "WindowsSDKVersion=__SDKV__\"

set "VCToolsInstallDir=%~dp0VC\Tools\MSVC\__MSVCV__\"
set "WindowsSdkBinPath=%~dp0Windows Kits\10\bin\"

set "PATH=%~dp0VC\Tools\MSVC\__MSVCV__\bin\Host__HOST__\__TARGET__;%~dp0Windows Kits\10\bin\__SDKV__\__HOST__;%~dp0Windows Kits\10\bin\__SDKV__\__HOST__\ucrt;%PATH%"
set "INCLUDE=%~dp0VC\Tools\MSVC\__MSVCV__\include;%~dp0Windows Kits\10\Include\__SDKV__\ucrt;%~dp0Windows Kits\10\Include\__SDKV__\shared;%~dp0Windows Kits\10\Include\__SDKV__\um;%~dp0Windows Kits\10\Include\__SDKV__\winrt;%~dp0Windows Kits\10\Include\__SDKV__\cppwinrt"
set "LIB=%~dp0VC\Tools\MSVC\__MSVCV__\lib\__TARGET__;%~dp0Windows Kits\10\Lib\__SDKV__\ucrt\__TARGET__;%~dp0Windows Kits\10\Lib\__SDKV__\um\__TARGET__"
"""

# 独立安装脚本：把本工具链目录永久写入当前用户环境变量（HKCU，去重，REG_EXPAND_SZ）
# 除 PATH/INCLUDE/LIB 外，还写入 WindowsSDKVersion（Nuitka 判定 SDK 的关键变量，
# 由 vcvarsall 设置，缺它会导致 Nuitka 打包时告警）、VCToolsInstallDir、WindowsSdkBinPath。
INSTALL_ENV_BAT_TEMPLATE = r"""@echo off
rem ================================================================
rem  Install Portable MSVC toolchain env vars (target: __TARGET__)
rem  Permanently adds PATH / INCLUDE / LIB / WindowsSDKVersion /
rem  VCToolsInstallDir / WindowsSdkBinPath for the CURRENT USER (HKCU).
rem  No admin required. Restart cmd / IDE to take effect.
rem ================================================================
set "_MSVC_ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand __PS_INSTALL_ENC__
echo.
echo [ok] PATH / INCLUDE / LIB / WindowsSDKVersion / VCToolsInstallDir / WindowsSdkBinPath added to your user environment variables.
echo [ok] Please restart cmd / IDE for changes to take effect.
"""

# 独立卸载脚本：从当前用户环境变量中移除本工具链路径（按脚本所在目录匹配）
UNINSTALL_ENV_BAT_TEMPLATE = r"""@echo off
rem ================================================================
rem  Uninstall Portable MSVC toolchain env vars (target: __TARGET__)
rem  Removes this toolchain's paths from the CURRENT USER env vars.
rem ================================================================
set "_MSVC_ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand __PS_UNINSTALL_ENC__
echo.
echo [ok] Toolchain paths removed from your user environment variables.
"""

# PowerShell：把本工具链目录追加进用户环境变量（去重，保留 REG_EXPAND_SZ）
# 脚本目录通过环境变量 _MSVC_ROOT 传入（cmd 负责展开 %~dp0），保持便携性。
# 末尾广播 WM_SETTINGCHANGE，让资源管理器立即刷新环境缓存，无需重启。
_PS_INSTALL = r"""$t=$env:_MSVC_ROOT;$arch='__TARGET__';$h='__HOST__';$vc='VC\Tools\MSVC\__MSVCV__';$sdk='Windows Kits\10\bin\__SDKV__';$inc='Windows Kits\10\Include\__SDKV__';$lib='Windows Kits\10\Lib\__SDKV__';$paths=@($t+$vc+'\bin\Host'+$h+'\'+$arch,$t+$sdk+'\'+$h,$t+$sdk+'\'+$h+'\ucrt');$includes=@($t+$vc+'\include',$t+$inc+'\ucrt',$t+$inc+'\shared',$t+$inc+'\um',$t+$inc+'\winrt',$t+$inc+'\cppwinrt');$libs=@($t+$vc+'\lib\'+$arch,$t+$lib+'\ucrt\'+$arch,$t+$lib+'\um\'+$arch);$reg=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment',$true);function Add-EnvPath($n,$items){$cur=$reg.GetValue($n,'',[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames);if($cur -isnot [string]){$cur=''};$parts=@();foreach($p in ($cur -split ';')){if($p){$parts+=$p}};foreach($p in $items){if($parts -notcontains $p){$parts+=$p}};$reg.SetValue($n,($parts -join ';'),[Microsoft.Win32.RegistryValueKind]::ExpandString)};function Set-EnvValue($n,$val){$reg.SetValue($n,$val,[Microsoft.Win32.RegistryValueKind]::String)};Add-EnvPath 'PATH' $paths;Add-EnvPath 'INCLUDE' $includes;Add-EnvPath 'LIB' $libs;Set-EnvValue 'WindowsSDKVersion' ('__SDKV__\');Set-EnvValue 'VCToolsInstallDir' ($t+$vc+'\');Set-EnvValue 'WindowsSdkBinPath' ($t+'Windows Kits\10\bin\');$reg.Close();$s='[DllImport("user32.dll")] public static extern IntPtr SendMessageTimeout(IntPtr hWnd,uint Msg,IntPtr wParam,string lParam,uint fuFlags,uint uTimeout,out IntPtr lpdwResult);';$t=Add-Type -MemberDefinition $s -Name WinAPI -Namespace Native -PassThru;$null=$t::SendMessageTimeout([IntPtr]0xffff,0x001A,[IntPtr]::Zero,'Environment',0x0002,3000,[ref]([IntPtr]::Zero))"""

# PowerShell：从用户环境变量中移除本工具链相关条目
# - PATH/INCLUDE/LIB/VCToolsInstallDir/WindowsSdkBinPath：按脚本所在目录前缀过滤；
#   注意过滤后若变量为空，写入空字符串而非 DeleteValue —— 直接删除 PATH 变量
#   会让系统命令在新进程里不可用，属环境级事故，必须防御。
# - WindowsSDKVersion：精确匹配本工具链写入的值（__SDKV__\）才删除 —— 该变量可能
#   被其它 SDK 使用，且空字符串值会让 Nuitka 解析版本号崩溃（int('')），因此不能
#   留空值，必须干净删除（此变量非系统必需，DeleteValue 安全）。
# 末尾广播 WM_SETTINGCHANGE，让资源管理器立即刷新环境缓存，无需重启。
_PS_UNINSTALL = r"""$t=$env:_MSVC_ROOT;$reg=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment',$true);function Remove-EnvPath($n){$cur=$reg.GetValue($n,'',[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames);if($cur -isnot [string]){return};$parts=@();foreach($p in ($cur -split ';')){if($p -and ($p -notlike ($t+'*'))){$parts+=$p}};$reg.SetValue($n,($parts -join ';'),[Microsoft.Win32.RegistryValueKind]::ExpandString)};function Remove-EnvValue($n,$val){$cur=$reg.GetValue($n,'',[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames);if($cur -eq $val){$reg.DeleteValue($n)}};Remove-EnvPath 'PATH';Remove-EnvPath 'INCLUDE';Remove-EnvPath 'LIB';Remove-EnvPath 'VCToolsInstallDir';Remove-EnvPath 'WindowsSdkBinPath';Remove-EnvValue 'WindowsSDKVersion' ('__SDKV__\');$reg.Close();$s='[DllImport("user32.dll")] public static extern IntPtr SendMessageTimeout(IntPtr hWnd,uint Msg,IntPtr wParam,string lParam,uint fuFlags,uint uTimeout,out IntPtr lpdwResult);';$t=Add-Type -MemberDefinition $s -Name WinAPI -Namespace Native -PassThru;$null=$t::SendMessageTimeout([IntPtr]0xffff,0x001A,[IntPtr]::Zero,'Environment',0x0002,3000,[ref]([IntPtr]::Zero))"""


def _ps_encoded(cmd: str) -> str:
    """PowerShell 命令 → -EncodedCommand 所需的 Base64（UTF-16LE）。"""
    import base64
    return base64.b64encode(cmd.encode("utf-16-le")).decode("ascii")


def _render_setup_bat(host: str, target: str, msvcv: str, sdkv: str) -> str:
    """渲染 setup_<target>.bat（会话级环境设置）。"""
    return (SETUP_BAT_TEMPLATE
            .replace("__HOST__", host)
            .replace("__TARGET__", target)
            .replace("__MSVCV__", msvcv)
            .replace("__SDKV__", sdkv))


def _render_env_install_bat(host: str, target: str, msvcv: str, sdkv: str) -> str:
    """渲染 setup_<target>_install.bat（永久写入当前用户环境变量）。"""
    enc = _ps_encoded(_PS_INSTALL
                      .replace("__HOST__", host)
                      .replace("__TARGET__", target)
                      .replace("__MSVCV__", msvcv)
                      .replace("__SDKV__", sdkv))
    return (INSTALL_ENV_BAT_TEMPLATE
            .replace("__TARGET__", target)
            .replace("__PS_INSTALL_ENC__", enc))


def _render_env_uninstall_bat(target: str, sdkv: str) -> str:
    """渲染 setup_<target>_uninstall.bat（从用户环境变量移除工具链相关条目）。

    sdkv 用于精确匹配本工具链写入的 WindowsSDKVersion 值，避免误删其它 SDK。
    """
    enc = _ps_encoded(_PS_UNINSTALL.replace("__SDKV__", sdkv))
    return (UNINSTALL_ENV_BAT_TEMPLATE
            .replace("__TARGET__", target)
            .replace("__PS_UNINSTALL_ENC__", enc))


def _sdk_dep_score(dep_id: str) -> int:
    """为 SDK 依赖候选 id 打分：越像真正的安装包得分越高。"""
    d = dep_id.lower()
    s = 0
    if "component" not in d:
        s += 3
    if "installer" in d or "download" in d or "sdk" in d:
        s += 2
    if d.endswith(".base"):
        s += 1
    return s


# 关键 MSVC 包判定：缺失时必须中断流程，避免产出残缺工具链仍报"成功"。
# 关键包 = 编译器本体（tools.<host>.<target>.base，排除 premium 变体）、
#          公共 CRT 头文件（crt.headers.base）、桌面 CRT 库（crt.<t>.desktop.base）。
# 其余（asan/pgo/premium/store/redist/source 等）为可选，缺失仅告警跳过。
_CRITICAL_MSVC_TOOLS_RE = re.compile(
    r"^microsoft\.vc\.\d+\.\d+\.tools\.host(?:x64|x86|arm64)"
    r"\.target(?:x64|x86|arm|arm64)\.base$"
)
_CRITICAL_MSVC_CRT_DESKTOP_RE = re.compile(
    r"^microsoft\.vc\.\d+\.\d+\.crt\.(?:x64|x86|arm|arm64)\.desktop\.base$"
)


def _is_critical_msvc_pkg(pkg: str) -> bool:
    """判断 MSVC 包是否编译链必需（缺失应中断而非静默跳过）。"""
    p = pkg.lower()
    if p.endswith(".crt.headers.base"):
        return True
    return bool(_CRITICAL_MSVC_TOOLS_RE.match(p)
                or _CRITICAL_MSVC_CRT_DESKTOP_RE.match(p))


class Pipeline:
    def __init__(
        self,
        cfg: PipelineConfig,
        on_log: Optional[LogCallback] = None,
        on_progress: Optional[ProgressCallback] = None,
        is_cancelled: Optional[CancelCheck] = None,
    ):
        self.cfg = cfg
        self.on_log = on_log or (lambda msg: None)
        self.on_progress = on_progress or (lambda pct: None)
        self.is_cancelled = is_cancelled or (lambda: False)

        self.output_dir = Path(cfg.output_dir).resolve()
        self.msvc_dir = self.output_dir / "MSVC"
        self.downloads_dir = self.output_dir / "downloads"

    # ------------------------------------------------------------------ 工具方法
    @staticmethod
    def _pick_version_dir(parent: Path, label: str) -> str:
        """从版本子目录中取数值最新者；无有效目录时抛错（不做猜测兜底）。"""
        dirs = [d for d in parent.glob("*") if d.is_dir()]
        if not dirs:
            raise RuntimeError(f"{label} 目录为空，疑似下载不完整")
        return max(dirs, key=lambda d: version_key(d.name)).name

    def _check_cancel(self):
        if self.is_cancelled():
            raise CancelledError("操作已取消")

    def _prepare_output(self):
        """执行前准备：若存在旧 MSVC 目录，先备份为 MSVC.bak-<时间戳> 再重建。

        重复执行时旧目录中可能混有上一版本的组件（同名文件被覆盖、独有文件残留），
        直接覆盖会产出混合版本工具链。这里备份而非删除，作为安全网保留；
        但只保留最近 2 份备份，避免多次运行累积数个 GB 的 MSVC.bak-* 目录。
        downloads 缓存目录不动，以支持断点续传。
        """
        if not self.msvc_dir.exists():
            self.msvc_dir.mkdir(parents=True, exist_ok=True)
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = self.output_dir / f"MSVC.bak-{stamp}"
        if backup.exists():
            backup = self.output_dir / f"MSVC.bak-{stamp}-x"
        self.on_log(
            f"检测到旧目录 {self.msvc_dir.name}，已备份为 {backup.name}"
            "（自动保留最近 2 份，确认无误后旧备份可手动删除）"
        )
        self.msvc_dir.replace(backup)
        self.msvc_dir.mkdir(parents=True, exist_ok=True)
        # 清理过期备份：按修改时间排序，保留最近 2 份（含刚生成的 backup）
        old_backups = sorted(
            (p for p in self.output_dir.glob("MSVC.bak-*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        for old in old_backups[2:]:
            shutil.rmtree(old, ignore_errors=True)
            self.on_log(f"已清理过期备份 {old.name}")

    def _progress(self, pct: float):
        self.on_progress(max(0, min(100, int(pct))))

    def _stage(self, msg: str):
        self.on_log(f"== {msg} ==")

    # ------------------------------------------------------------------ 主流程
    def run(self) -> PipelineResult:
        result = PipelineResult(output_dir=self.output_dir, msvc_dir=self.msvc_dir)
        errors = self.cfg.validate()
        if errors:
            result.error = "；".join(errors)
            return result

        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.downloads_dir.mkdir(parents=True, exist_ok=True)
            self._prepare_output()   # 备份旧 MSVC 目录，避免版本混合污染

            self._stage("解析 VS / Windows SDK 清单")
            self._progress(2)
            catalog = VSCatalog(self.cfg.vs, preview=self.cfg.preview)
            msvc_ver, msvc_pid = catalog.resolve_msvc(self.cfg.msvc_version)
            sdk_pid = catalog.resolve_sdk(self.cfg.sdk_version)
            result.msvc_version = msvc_ver
            # SDK 包 id 末段为 build 号（如 26100），完整版本为 10.0.<build>.0
            result.sdk_version = f"10.0.{sdk_pid.split('.')[-1]}.0"
            self.on_log(f"选定 MSVC v{msvc_ver} / Windows SDK v{result.sdk_version}")

            if not self.cfg.accept_license:
                result.error = "未接受 Visual Studio 许可协议，已中止"
                return result

            self._download_msvc(catalog, msvc_ver)
            self._download_sdk(catalog, sdk_pid)
            self._post_process(msvc_ver)          # 整理瘦身：进度 86-90
            self._write_setup_bats(msvc_ver, result)
            self._progress(92)                    # 环境脚本生成完毕

            if self.cfg.pack:
                self._stage("打包压缩")
                # 打包进度区间 92-100：7z 场景实时映射（-bsp1 进度 0-100），
                # tar / PowerShell 无进度输出时停留在 92，完成后跳 100。
                archive = pack_dir(
                    self.msvc_dir, self.output_dir, log=self.on_log,
                    level=self.cfg.pack_level,
                    is_cancelled=self.is_cancelled,
                    on_progress=lambda pct: self._progress(92 + pct * 8.0 / 100.0),
                )
                result.archive = archive

            self._progress(100)
            self.on_log("全部完成！")
            result.ok = True
            # 清理下载缓存（zip/msi/cab），避免残留 1~3GB 临时文件
            self.on_log("清理下载缓存…")
            self.cleanup_downloads()
            return result
        except CancelledError as exc:
            result.cancelled = True
            result.error = str(exc)
            return result
        except Exception as exc:  # noqa: BLE001 —— 统一兜底，保证 GUI 拿到错误信息
            # 完整堆栈进日志（GUI 面板与文件日志），UI 只展示简短错误
            self.on_log(traceback.format_exc())
            result.error = f"{type(exc).__name__}: {exc}"
            return result

    # ------------------------------------------------------------------ MSVC 下载
    def _download_msvc(self, catalog: VSCatalog, msvc_ver: str):
        self._stage("下载 MSVC 组件")
        host = self.cfg.host

        pkg_names = [
            "microsoft.visualcpp.dia.sdk",
            f"microsoft.vc.{msvc_ver}.crt.headers.base",
            f"microsoft.vc.{msvc_ver}.crt.source.base",
            f"microsoft.vc.{msvc_ver}.asan.headers.base",
            f"microsoft.vc.{msvc_ver}.pgo.headers.base",
        ]
        for target in self.cfg.targets:
            pkg_names += [
                f"microsoft.vc.{msvc_ver}.tools.host{host}.target{target}.base",
                f"microsoft.vc.{msvc_ver}.tools.host{host}.target{target}.res.base",
                f"microsoft.vc.{msvc_ver}.crt.{target}.desktop.base",
                f"microsoft.vc.{msvc_ver}.crt.{target}.store.base",
                f"microsoft.vc.{msvc_ver}.premium.tools.host{host}.target{target}.base",
                f"microsoft.vc.{msvc_ver}.pgo.{target}.base",
            ]
            if target in ("x86", "x64"):
                pkg_names.append(f"microsoft.vc.{msvc_ver}.asan.{target}.base")

            redist_suffix = ".onecore.desktop" if target == "arm" else ""
            redist_pkg = f"microsoft.vc.{msvc_ver}.crt.redist.{target}{redist_suffix}.base"
            if redist_pkg not in catalog.packages:
                redist = catalog.package(f"microsoft.visualcpp.crt.redist.{target}{redist_suffix}")
                if redist is None:
                    self.on_log(f"!! 缺少 redist 包: {target}")
                    continue
                # dependencies 为 dict(id -> version)，迭代键即可
                deps = redist.get("dependencies", {})
                dep_iter = deps.keys() if isinstance(deps, dict) else deps
                dep = next((d for d in dep_iter if d.lower().endswith(".base")), None)
                if dep is None:
                    self.on_log(f"!! redist 依赖解析失败: {target}")
                    continue
                redist_pkg = dep.lower()
            pkg_names.append(redist_pkg)

        # 预计算 (包, payload) 列表，用于进度统计。
        # 注意：包存在但 payloads 为空/缺键同样视为缺失（微软清单结构变化时
        # 可能给出空 payload），必须进入关键/可选判定，避免静默产出残缺工具链。
        jobs: list[tuple[str, dict]] = []
        missing_critical: list[str] = []
        for pkg in sorted(pkg_names):
            p = catalog.package(pkg)
            payloads = (p or {}).get("payloads") or []
            if p is None or not payloads:
                if _is_critical_msvc_pkg(pkg):
                    missing_critical.append(pkg)
                else:
                    self.on_log(f"!! 缺少可选包: {pkg}（跳过）")
                continue
            for payload in payloads:
                jobs.append((pkg, payload))

        # 关键包缺失：中止流程，避免静默产出无法编译的工具链
        if missing_critical:
            raise RuntimeError(
                "缺少关键 MSVC 包，无法构建可用工具链: "
                + ", ".join(sorted(missing_critical))
            )

        total = len(jobs)
        for idx, (pkg, payload) in enumerate(jobs):
            self._check_cancel()
            filename = payload["fileName"]
            base = 10 + idx * 45.0 / total
            span = 45.0 / total

            def _on_file(pct: float, _base=base, _span=span):
                self._progress(_base + pct * _span / 100.0)

            self.on_log(f"[{idx + 1}/{total}] {filename}")
            path = download_to_file(
                payload["url"], self.downloads_dir / filename, payload["sha256"],
                on_progress=_on_file, is_cancelled=self.is_cancelled,
            )
            extract_zip_contents(path, self.msvc_dir, is_cancelled=self.is_cancelled)
        self._progress(55)

    # ------------------------------------------------------------------ SDK 下载
    def _download_sdk(self, catalog: VSCatalog, sdk_pid: str):
        self._stage("下载 Windows SDK")
        sdk_pkg = catalog.package(sdk_pid)
        if sdk_pkg is None:
            # SDK 与 MSVC 同为编译必需组件：缺失必须中断（与 _download_msvc
            # 的关键包逻辑一致），避免白等 MSVC 下载后报误导性的"目录为空"。
            raise RuntimeError(
                f"缺少 Windows SDK 包: {sdk_pid}，无法构建可用工具链"
            )
        deps = sdk_pkg.get("dependencies", {})
        if deps:
            # dependencies 为 dict(id -> version)；按特征挑选真正的 SDK 安装包，
            # 避免随意取第一个键（可能是不相关的组件依赖）
            dep_ids = list(deps) if isinstance(deps, dict) else list(deps)
            key = max(dep_ids, key=_sdk_dep_score)
            sdk_pkg = catalog.package(key) or sdk_pkg

        sdk_packages = [
            "Windows SDK for Windows Store Apps Tools-x86_en-us.msi",
            "Windows SDK for Windows Store Apps Headers-x86_en-us.msi",
            "Windows SDK for Windows Store Apps Headers OnecoreUap-x86_en-us.msi",
            "Windows SDK for Windows Store Apps Libs-x86_en-us.msi",
            "Universal CRT Headers Libraries and Sources-x86_en-us.msi",
        ]
        for target in ("x64", "x86", "arm", "arm64"):
            sdk_packages += [
                f"Windows SDK Desktop Headers {target}-x86_en-us.msi",
                f"Windows SDK OnecoreUap Headers {target}-x86_en-us.msi",
            ]
        for target in self.cfg.targets:
            sdk_packages.append(f"Windows SDK Desktop Libs {target}-x86_en-us.msi")

        def _find_payload(name: str) -> dict | None:
            key = f"Installers/{name}"
            return next(
                (p for p in sdk_pkg["payloads"] if p.get("fileName", "").replace("\\", "/") == key),
                None,
            )

        msi_files: list[Path] = []
        cab_names: list[str] = []
        jobs: list[tuple[str, dict]] = []
        for name in sorted(sdk_packages):
            payload = _find_payload(name)
            if payload is None:
                self.on_log(f"!! 该 SDK 版本未提供 {name}（跳过）")
                continue
            jobs.append((name, payload))

        total = len(jobs)
        for idx, (name, payload) in enumerate(jobs):
            self._check_cancel()
            base = 55 + idx * 25.0 / total
            span = 25.0 / total

            def _on_file(pct: float, _base=base, _span=span):
                self._progress(_base + pct * _span / 100.0)

            self.on_log(f"[{idx + 1}/{total}] {name}")
            path = download_to_file(
                payload["url"], self.downloads_dir / name, payload["sha256"],
                on_progress=_on_file, is_cancelled=self.is_cancelled,
            )
            msi_files.append(path)
            cab_names += list(get_msi_cabs(path))

        # 下载 cab：以清单中的 .cab payload 枚举为权威来源（不依赖 MSI 二进制解析，
        # 旧算法从 MSI 中截取文件名极易失败），再用 MSI 引用交叉校验补漏。
        # 进度区间 80-84（归属"下载"阶段），避免进度条长时间停滞。
        cab_jobs: dict[str, dict] = {}
        for p in sdk_pkg.get("payloads", []):
            fname = p.get("fileName", "").replace("\\", "/")
            if fname.lower().endswith(".cab"):
                cab_jobs.setdefault(fname.rsplit("/", 1)[-1], p)
        for name in sorted(set(cab_names)):
            payload = _find_payload(name)
            if payload is None:
                self.on_log(f"!! MSI 引用了清单中不存在的 cab: {name}（跳过）")
                continue
            cab_jobs.setdefault(name, payload)
        if not cab_jobs:
            raise RuntimeError("Windows SDK 清单中未找到任何 cab 文件，流程中止")
        cab_items = sorted(cab_jobs.items())
        cab_total = len(cab_items)
        for idx, (name, payload) in enumerate(cab_items):
            self._check_cancel()
            self.on_log(f"下载 {name}")
            base = 80 + idx * 4.0 / cab_total
            span = 4.0 / cab_total

            def _on_cab(pct: float, _base=base, _span=span):
                self._progress(_base + pct * _span / 100.0)

            download_to_file(payload["url"], self.downloads_dir / name, payload["sha256"],
                             on_progress=_on_cab, is_cancelled=self.is_cancelled)
        self._progress(84)

        self._stage("解包 Windows SDK")
        msi_total = len(msi_files)
        for idx, m in enumerate(msi_files):
            self._check_cancel()
            # 解包进度 84-86：每个 MSI 完成后前进一格（解包子进程本身不可分片）
            extract_msi(m, self.msvc_dir, log=self.on_log, is_cancelled=self.is_cancelled)
            if msi_total:
                self._progress(84 + (idx + 1) * 2.0 / msi_total)
            m.unlink(missing_ok=True)
            (self.msvc_dir / m.name).unlink(missing_ok=True)
        move_windows_kits_up(self.msvc_dir)
        self._progress(86)

    # ------------------------------------------------------------------ 整理瘦身
    def _post_process(self, msvc_ver: str):
        self._stage("整理与瘦身")
        host = self.cfg.host

        vc_tools = self.msvc_dir / "VC/Tools/MSVC"
        msvcv = self._pick_version_dir(vc_tools, "MSVC 工具")
        sdkv = self._pick_version_dir(self.msvc_dir / "Windows Kits/10/bin", "Windows SDK")

        # 本阶段进度区间 86-90，按子步骤推进；每一步前检查取消，
        # 避免 rmtree 大量目录期间无法中断（此前取消需等整个阶段结束）。
        steps = 5
        done = 0

        def _tick():
            nonlocal done
            done += 1
            self._progress(86 + done * 4.0 / steps)

        # 1) debug CRT DLL 放入 bin 目录（Target 架构）
        self._check_cancel()
        redist = self.msvc_dir / "VC/Redist"
        if redist.exists():
            redist_vs = list((redist / "MSVC").glob("*"))
            if redist_vs:
                src = redist / "MSVC" / redist_vs[0].name / "debug_nonredist"
                for target in self.cfg.targets:
                    for f in (src / target).glob("**/*.dll"):
                        dst = vc_tools / msvcv / f"bin/Host{host}" / target
                        dst.mkdir(parents=True, exist_ok=True)
                        f.replace(dst / f.name)
            shutil.rmtree(redist, ignore_errors=True)
        _tick()

        # 2) msdia140.dll（仅开发用途，始终为 Host 架构）。
        #    目录名历史上存在 "DIA SDK" 与 "DIA%20SDK" 两种写法，用通配匹配，
        #    避免硬编码 URL 编码名导致复制与清理静默失效。
        self._check_cancel()
        msdia_map = {
            "x86": "msdia140.dll",
            "x64": "amd64/msdia140.dll",
            "arm": "arm/msdia140.dll",
            "arm64": "arm64/msdia140.dll",
        }
        dia_root = next(self.msvc_dir.glob("DIA*SDK"), None)
        if dia_root is not None:
            dia_src = dia_root / "bin" / msdia_map[host]
            if dia_src.exists():
                for target in self.cfg.targets:
                    dst = vc_tools / msvcv / f"bin/Host{host}" / target
                    dst.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(dia_src, dst / dia_src.name)
                self.on_log(f"已部署 {dia_src.name}（{dia_root.name}）")
            else:
                self.on_log("!! 未找到 msdia140.dll（DIA SDK 目录结构不符），调试符号支持可能缺失")
            shutil.rmtree(dia_root, ignore_errors=True)
        else:
            self.on_log("!! 未找到 DIA SDK 目录，msdia140.dll 未部署")
        _tick()

        # 3) 清理无用目录（Common7 / Auxiliary / store 等子目录）
        self._check_cancel()
        shutil.rmtree(self.msvc_dir / "Common7", ignore_errors=True)
        shutil.rmtree(vc_tools / msvcv / "Auxiliary", ignore_errors=True)
        for target in self.cfg.targets:
            for sub in ("store", "uwp", "enclave", "onecore"):
                shutil.rmtree(vc_tools / msvcv / "lib" / target / sub, ignore_errors=True)
            shutil.rmtree(vc_tools / msvcv / f"bin/Host{host}" / target / "onecore", ignore_errors=True)
        for sub in ("Catalogs", "DesignTime", f"bin/{sdkv}/chpe", f"Lib/{sdkv}/ucrt_enclave"):
            shutil.rmtree(self.msvc_dir / "Windows Kits/10" / sub, ignore_errors=True)
        _tick()

        # 4) 删除未选架构的 Lib/bin 目录
        self._check_cancel()
        for arch in ("x86", "x64", "arm", "arm64"):
            if arch not in self.cfg.targets:
                shutil.rmtree(self.msvc_dir / f"Windows Kits/10/Lib/{sdkv}/ucrt/{arch}", ignore_errors=True)
                shutil.rmtree(self.msvc_dir / f"Windows Kits/10/Lib/{sdkv}/um/{arch}", ignore_errors=True)
            if arch != host:
                shutil.rmtree(vc_tools / msvcv / f"bin/Host{arch}", ignore_errors=True)
                shutil.rmtree(self.msvc_dir / f"Windows Kits/10/bin/{sdkv}/{arch}", ignore_errors=True)
        _tick()

        # 5) 移除遥测程序 vctip.exe + 生成 nvcc 兼容占位脚本
        self._check_cancel()
        for target in self.cfg.targets:
            (vc_tools / msvcv / f"bin/Host{host}" / target / "vctip.exe").unlink(missing_ok=True)
        build = self.msvc_dir / "VC/Auxiliary/Build"
        build.mkdir(parents=True, exist_ok=True)
        (build / "vcvarsall.bat").write_text(
            "rem both bat files are here only for nvcc, do not call them manually",
            encoding="utf-8",
        )
        (build / "vcvars64.bat").touch()
        _tick()

    # ------------------------------------------------------------------ 环境脚本
    def _write_setup_bats(self, msvc_ver: str, result: PipelineResult):
        self._stage("生成环境脚本")
        host = self.cfg.host

        vc_tools = self.msvc_dir / "VC/Tools/MSVC"
        msvcv = self._pick_version_dir(vc_tools, "MSVC 工具")
        sdkv = self._pick_version_dir(self.msvc_dir / "Windows Kits/10/bin", "Windows SDK")

        for target in self.cfg.targets:
            files = [
                (f"setup_{target}.bat",
                 _render_setup_bat(host, target, msvcv, sdkv)),
                (f"setup_{target}_install.bat",
                 _render_env_install_bat(host, target, msvcv, sdkv)),
                (f"setup_{target}_uninstall.bat",
                 _render_env_uninstall_bat(target, sdkv)),
            ]
            for name, content in files:
                bat = self.msvc_dir / name
                bat.write_text(content, encoding="utf-8")
                result.setup_bats.append(bat)
                self.on_log(f"生成 {bat.name}")

    # ------------------------------------------------------------------ 清理
    def cleanup_downloads(self):
        """删除下载缓存目录（不影响已生成的 MSVC 与压缩包）。"""
        shutil.rmtree(self.downloads_dir, ignore_errors=True)
