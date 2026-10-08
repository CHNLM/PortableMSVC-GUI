# -*- coding: utf-8 -*-
"""Nuitka 一键打包脚本。

本脚本将 app/ 下的 Python 源码用 Nuitka 编译为原生机器码并产出可分发目录，
等价于 PyInstaller 的 onedir 模式，但产物体积更小、性能更好、不易反编译。

用法:
    python build_nuitka.py               # 完整打包（standalone + 无控制台 + 图标 + 版本信息）
    python build_nuitka.py --clean       # 清理 Nuitka 编译缓存与旧产物后重新打包
    python build_nuitka.py --jobs 8      # 指定并行编译线程数（默认 = CPU 核数）

产物:
    dist/PortableMSVC-GUI.dist/          # 分发整个目录（内含 exe 与全部依赖）
    dist/PortableMSVC-GUI.dist/PortableMSVC-GUI.exe

前置条件:
    1. 安装打包依赖:  pip install -r requirements-dev.txt  （nuitka）
    2. 可用的 C 编译器（Nuitka 自动检测）:
       - 本机 MSVC 工具链（cl.exe 在 PATH，且 INCLUDE/LIB 环境变量已配置），或
       - MinGW-w64（Nuitka 会提示自动下载）
    3. 若构建报错 "Windows SDK must be installed in Visual Studio"：
       说明 Nuitka 的 Scons 未找到 SDK 头文件，需确保 INCLUDE/LIB 环境变量
       已指向本机 Windows Kits（本程序自身正是为此类环境设计的）。

说明:
    - 版本号单一来源：app/__init__.py 的 __version__。
      注意 Nuitka 要求版本号为 1~4 段纯数字（如 1.0 或 1.0.0.0），
      带预发布后缀（如 1.0.0-beta）会报错，发布前请留意。
    - 增量编译：Nuitka 有编译缓存，仅当源码/依赖变更时才重编译对应模块；
      需要全量重编时加 --clean。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 项目根目录（脚本所在目录），所有路径均基于它，保证从任意 cwd 运行都正确。
ROOT = Path(__file__).resolve().parent
# Nuitka 输出目录：dist/ 下会有 <exe名>.build/（C 编译中间产物）与 <exe名>.dist/（最终产物）
DIST = ROOT / "dist"
# 最终可执行文件名（--output-filename 指定），dist 目录名取其去掉扩展名 + ".dist"
EXE_NAME = "PortableMSVC-GUI.exe"
EXE_DIST_DIR = DIST / "PortableMSVC-GUI.dist"

# 版本号单一来源：app/__init__.py 的 __version__（避免与 version_info 重复维护）
sys.path.insert(0, str(ROOT))
from app import __version__  # noqa: E402


def _clean_old_dist() -> None:
    """删除上次构建的 dist 产物目录，保证 --clean 时从零产出、不混淆。"""
    if EXE_DIST_DIR.exists():
        shutil.rmtree(EXE_DIST_DIR, ignore_errors=True)
        print(f">> 已清理旧产物 {EXE_DIST_DIR.relative_to(ROOT)}")


def build(clean: bool, jobs: int) -> int:
    """执行 Nuitka 打包，返回进程退出码（0 = 成功）。"""
    if clean:
        _clean_old_dist()

    cmd: list[str] = [
        sys.executable, "-m", "nuitka",
        # ---- 编译模式 ----
        "--standalone",                     # 产出独立可分发目录（等价 PyInstaller onedir）
        "--enable-plugin=pyside6",          # PySide6 插件：自动收集 Qt 依赖（插件/翻译/DLL）
        "--nofollow-import-to=tkinter",     # 不跟随 tkinter（本程序用 Qt，排除可缩短编译时间）
        # ---- 窗口与外观 ----
        "--windows-console-mode=disable",   # GUI 程序：不创建/使用控制台窗口
        f"--windows-icon-from-ico={ROOT / 'portable_msvc.ico'}",
        # ---- 版本信息（Nuitka 4.x 独立参数；4.x 无 --windows-version-info-file）----
        "--company-name=PortableMSVC",
        "--product-name=便携版 MSVC 工具链",
        "--file-description=便携版 MSVC 工具链",
        f"--file-version={__version__}",    # 必须为 1~4 段纯数字
        f"--product-version={__version__}",
        # ---- 运行时数据文件 ----
        # main.py 打包态从 exe 同目录（dist 根）加载资源，
        # 因此源路径 app/ui/theme.qss 平铺为 dist 根的 theme.qss / portable_msvc.ico。
        f"--include-data-files={ROOT / 'app/ui/theme.qss'}=theme.qss",
        f"--include-data-files={ROOT / 'portable_msvc.ico'}=portable_msvc.ico",
        # ---- 输出控制 ----
        f"--output-dir={DIST}",             # 中间产物与最终产物均放在 dist/ 下
        f"--output-filename={EXE_NAME}",    # 重命名最终 exe（默认跟随入口模块名）
        f"--jobs={jobs}",                   # 并行编译线程数
        # ---- 编译入口 ----
        str(ROOT / "app" / "main.py"),      # 主程序文件
    ]
    # Nuitka 的选项须放在 "python -m nuitka" 之后：
    # cmd = [python, -m, nuitka, ...]，因此 --clean 应插入索引 3
    # （放在 nuitka 模块名之后、其余选项之前）
    if clean:
        cmd.insert(3, "--clean")            # 清理 Nuitka 编译缓存（ccache/字节码等）

    print(">> Nuitka 打包开始（并行线程:", jobs, "）")
    print(">> 编译耗时与代码量相关，PySide6 项目通常需 10~30 分钟，请耐心等待…")
    print(">> 命令:", " ".join(cmd), "\n")
    rc = subprocess.call(cmd)
    if rc != 0:
        # Nuitka 的具体报错已输出到终端，这里只做汇总提示
        print(f"\n!! 打包失败（退出码 {rc}），请查看上方 Nuitka 输出的错误信息")
        return rc

    # 产物定位：按预期路径查找，找不到时 glob 兜底（防止目录命名与预期不符）
    exe = EXE_DIST_DIR / EXE_NAME
    if not exe.exists():
        hits = sorted(DIST.glob("*.dist/*.exe")) if DIST.exists() else []
        if hits:
            exe = hits[0]
            print(f"\n!! 产物位置与预期不同，实际位于: {exe}")
        else:
            print("\n!! 打包完成但未找到产物，请检查 dist/ 目录")
            return 1
    size = exe.stat().st_size / 2**20
    print(f"\n✅ 打包成功: {exe}（{size:.1f} MiB）")
    print(f"   分发整个目录: {exe.parent}（含 Qt 运行库与插件，勿只拷贝 exe）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nuitka 一键打包 PortableMSVC-GUI（产物: dist/PortableMSVC-GUI.dist/）",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="清理 Nuitka 编译缓存并删除旧产物后，全量重新打包",
    )
    parser.add_argument(
        "--jobs", type=int, default=os.cpu_count() or 4, metavar="N",
        help="并行编译线程数（默认 = CPU 核数；内存紧张时可调低）",
    )
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs 必须为正整数")
    return build(args.clean, args.jobs)


if __name__ == "__main__":
    sys.exit(main())
