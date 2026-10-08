"""数据模型定义。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# 可选架构常量（与 portable-msvc 保持一致）
ALL_HOSTS = ("x64", "x86", "arm64")
ALL_TARGETS = ("x64", "x86", "arm", "arm64")
ALL_VS = ("latest", "2026", "2022", "2019")


class CancelledError(Exception):
    """用户取消操作（downloader / extractor / packager 共用，流水线统一捕获）。"""


@dataclass
class PipelineConfig:
    """一次完整执行所需的全部参数。"""

    vs: str = "latest"                # VS 通道：latest / 2026 / 2022 / 2019
    host: str = "x64"                 # 主机架构
    targets: list[str] = field(default_factory=lambda: ["x64", "x86"])
    msvc_version: str = ""            # 留空 = 使用清单中最新版本
    sdk_version: str = ""             # 留空 = 使用清单中最新版本
    preview: bool = False             # 是否允许预览版 MSVC
    accept_license: bool = True       # 是否已接受 VS 许可协议
    output_dir: Path = Path(".")      # 输出根目录（MSVC 与压缩包都放在这里）
    pack: bool = True                 # 结束后是否打包为压缩包
    pack_level: int = 5               # 7z 压缩级别 1-9（默认 5 均衡；仅 pack 时有效）

    def validate(self) -> list[str]:
        """返回配置校验错误列表（空 = 合法）。"""
        errors: list[str] = []
        if self.vs not in ALL_VS:
            errors.append(f"未知的 VS 版本: {self.vs}")
        if self.host not in ALL_HOSTS:
            errors.append(f"未知的主机架构: {self.host}")
        if not self.targets:
            errors.append("至少需要选择一个目标架构")
        for t in self.targets:
            if t not in ALL_TARGETS:
                errors.append(f"未知的目标架构: {t}")
        if not str(self.output_dir).strip():
            errors.append("输出目录不能为空")
        if not 1 <= self.pack_level <= 9:
            errors.append(f"压缩级别必须在 1-9 之间: {self.pack_level}")
        return errors


@dataclass
class PipelineResult:
    """流水线执行结果。"""

    ok: bool = False
    error: str = ""                   # 失败原因（ok=False 时有效）
    cancelled: bool = False
    output_dir: Path = Path(".")
    msvc_dir: Path | None = None      # 生成的 MSVC 目录
    msvc_version: str = ""
    sdk_version: str = ""
    archive: Path | None = None       # 打包产物（未打包时为 None）
    setup_bats: list[Path] = field(default_factory=list)


@dataclass
class EnvCheck:
    """单项环境检查结果。level: error（阻断）/ warn（建议）/ info（提示）。"""

    name: str
    level: str
    detail: str
