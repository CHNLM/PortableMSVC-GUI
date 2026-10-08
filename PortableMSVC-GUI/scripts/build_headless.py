"""在无 GUI 环境（GitHub Actions / CI）中运行完整流水线，生成便携版并重命名归档。

核心逻辑复用 app.core.pipeline.Pipeline —— 该模块不依赖任何 GUI 框架，
GUI 只是它的包装层，因此可以原样在 CI 上执行：
清单解析 → MSVC 下载解包 → Windows SDK 下载解包 → 整理瘦身 → 生成环境脚本 → 打包。

用法：
  python scripts/build_headless.py
  python scripts/build_headless.py --vs latest --host x64 --targets x64,x86 --pack-level 5

产物：
  输出目录为 <工作目录>/output/（*.7z / *.zip 已被 .gitignore 忽略）。
  归档按版本号重命名：PortableMSVC-<MSVC完整版本>.7z（如 PortableMSVC-14.42.34433.7z），
  完整版本号取自生成的 VC/Tools/MSVC/<version> 目录名（含 build 号，非两段显示版本）。

CI 集成：
  成功后将归档绝对路径写入 $GITHUB_OUTPUT（key: archive），供 workflow 上传 Release；
  非 CI 环境打印到 stdout。失败时退出码非 0，并打印 result.error。

注意：
  accept_license 在此固定为 True —— 自动化流程无法弹窗勾选许可协议，
  触发者（仓库所有者）须确保已阅读并同意 Visual Studio 许可协议。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Windows 控制台（含 CI runner）默认编码可能是 cp1252/GBK，print 中文会抛
# UnicodeEncodeError（已实测在 windows-latest runner 上复现）。这里统一把
# stdout/stderr 重配为 UTF-8（errors=replace 兜底，任何字符都不会再崩）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.models import ALL_VS, PipelineConfig  # noqa: E402
from app.core.pipeline import Pipeline  # noqa: E402


def emit(key: str, value: str):
    out = os.environ.get("GITHUB_OUTPUT")
    line = f"{key}={value}"
    if out:
        try:
            with open(out, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            print(line)
    else:
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="无 GUI 构建便携版 MSVC")
    # 通道选项取自 models.ALL_VS（单一事实来源，与 workflow 的 inputs 保持一致）
    parser.add_argument("--vs", default="latest", choices=list(ALL_VS))
    parser.add_argument("--host", default="x64", choices=["x64", "x86", "arm64"])
    parser.add_argument("--targets", default="x64,x86", help="逗号分隔，如 x64,x86,arm64")
    parser.add_argument("--pack-level", type=int, default=5, help="7z 压缩级别 1-9")
    parser.add_argument("--output-dir", default="output", help="输出根目录（默认 ./output）")
    args = parser.parse_args()

    cfg = PipelineConfig(
        vs=args.vs,
        host=args.host,
        targets=[t.strip() for t in args.targets.split(",") if t.strip()],
        msvc_version="",            # 留空 = 清单中最新版本
        sdk_version="",             # 留空 = 清单中最新版本
        preview=False,
        accept_license=True,        # 见模块 docstring 的说明
        output_dir=Path(args.output_dir),
        pack=True,
        pack_level=args.pack_level,
    )

    def log(msg: str):
        print(f"[build] {msg}", flush=True)

    print(f"[build] 开始构建: VS={cfg.vs} host={cfg.host} targets={cfg.targets} "
          f"level={cfg.pack_level} output={cfg.output_dir}")
    result = Pipeline(cfg, on_log=log).run()

    if not result.ok:
        print(f"[build] 构建失败: {result.error}", file=sys.stderr)
        return 1
    if result.archive is None:
        print("[build] 流水线未生成归档（pack 未开启？）", file=sys.stderr)
        return 1

    # 完整版本号取自 MSVC 版本目录名（含 build 号），而非两段显示版本
    try:
        msvc_full = Pipeline._pick_version_dir(
            result.msvc_dir / "VC/Tools/MSVC", "MSVC 工具"
        )
    except RuntimeError as exc:
        print(f"[build] 解析 MSVC 版本目录失败: {exc}", file=sys.stderr)
        return 1

    # 按版本号重命名归档，保留扩展名（.7z / .zip）
    renamed = result.archive.with_name(f"PortableMSVC-{msvc_full}{result.archive.suffix}")
    result.archive.replace(renamed)
    print(f"[build] 归档已重命名: {renamed}")

    print(f"[build] 完成: MSVC v{msvc_full} / SDK v{result.sdk_version}")
    # 输出绝对路径（正斜杠），后续 step 的工作目录可能不同
    emit("archive", str(renamed.resolve()).replace("\\", "/"))
    emit("msvc_version", result.msvc_version)
    emit("sdk_version", result.sdk_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
