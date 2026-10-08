"""检测 MSVC 最新版本，并与 GitHub 上已发布的最新 Release 对比，判断是否需要重新构建。

用于 GitHub Actions 的每月例行检查（.github/workflows/release.yml 的 check job）。
核心逻辑复用 app.core.manifest.VSCatalog，无 GUI / 平台依赖，可在任何 runner 上运行。

输出（写入 $GITHUB_OUTPUT；非 CI 环境打印到 stdout）：
  updated    三态：true（有新版本）/ false（无更新）/ error（检查失败）
  msvc_full  最新 MSVC 完整版本号（如 14.42.34433）
  sdk_build  Windows SDK build 号（如 26100）

判定逻辑：
  1. 解析 VS 稳定通道清单（aka.ms/vs/stable/channel），取最新 MSVC 工具链包，
     以其包的 version 字段作为"完整版本号"（前三段，如 14.42.34433）；
  2. 查询本仓库所有 Release 的 tag，筛选 msvc-<版本> 格式并取数值最大者；
  3. 清单最新版本 > 已发布最大版本 => updated=true；相等或更旧 => updated=false；
     没有任何 msvc-* Release => updated=true（首次发布）；
  4. 网络 / 清单解析 / 未知异常 => updated=error 并打印原因（不误判为"无更新"），
     同时以非零退出码结束，让 Actions 的 check job 标红，避免故障被静默掩盖。
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

# Windows 控制台默认编码可能是 cp1252/GBK，print 中文会抛 UnicodeEncodeError；
# 统一重配为 UTF-8（errors=replace 兜底）。虽然 check job 默认跑 ubuntu，
# 保持一致防御，避免未来换平台/本地调试踩坑。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# 项目根（scripts/ 的上一级）加入 import 路径，复用 app.core 模块
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.common import USER_AGENT, version_key  # noqa: E402
from app.core.manifest import ManifestError, VSCatalog  # noqa: E402
from app.core.models import ALL_VS  # noqa: E402

TAG_PREFIX = "msvc-"          # Release tag 前缀，如 msvc-14.42.34433
API = "https://api.github.com"


def emit(key: str, value: str):
    """写入 GITHUB_OUTPUT（CI）或打印（本地调试）。"""
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


def latest_msvc_full(catalog: VSCatalog) -> str:
    """取清单中最新 MSVC 的完整版本号（如 14.42.34433），解析不到时回退两段号。"""
    display, pid = catalog.resolve_msvc("")
    pkg = catalog.package(pid)
    if pkg:
        raw = pkg.get("version") or ""
        digits = [seg for seg in raw.split(".") if seg.isdigit()]
        if digits:
            return ".".join(digits[:3])
    return display  # 防御：包 version 字段缺失时回退到两段显示版本


def latest_sdk_build(catalog: VSCatalog) -> str:
    """取清单中最新 Windows SDK 的 build 号（如 26100）。"""
    pid = catalog.resolve_sdk("")
    return pid.split(".")[-1]


def fetch_releases(repo: str, token: str) -> list[dict]:
    """拉取仓库 Release 列表（最近 100 个）。

    每月发布 1 个 release 的情况下，100 条上限可覆盖约 8 年，足够；
    若未来 release 数逼近上限，可改为按页遍历或改用 releases/latest。
    公开仓库匿名可读，CI 带上 token 以避免限流。
    """
    req = urllib.request.Request(
        f"{API}/repos/{repo}/releases?per_page=100",
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.loads(res.read().decode("utf-8"))


def max_published_version(repo: str, token: str) -> str:
    """返回已发布 msvc-* Release 中最大的版本号；无匹配时返回空串。"""
    releases = fetch_releases(repo, token)
    if not isinstance(releases, list):
        # GitHub API 异常响应（如限流 403）是 dict 而非 list，显式报错而非裸崩
        raise RuntimeError(f"GitHub API 返回了非预期的响应类型: {type(releases).__name__}")
    best = ""
    for rel in releases:
        m = re.match(rf"^{TAG_PREFIX}(\d+(?:\.\d+)+)$", rel.get("tag_name") or "")
        if m and (not best or version_key(m.group(1)) > version_key(best)):
            best = m.group(1)
    return best


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="检测 MSVC 版本更新（CI 用）")
    # 通道选项取自 models.ALL_VS（单一事实来源，与 workflow 的 inputs 保持一致）
    parser.add_argument("--vs", default="latest", choices=list(ALL_VS),
                        help="VS 通道（默认 latest）")
    args = parser.parse_args(argv)

    try:
        catalog = VSCatalog(args.vs, preview=False)
        msvc_full = latest_msvc_full(catalog)
        sdk_build = latest_sdk_build(catalog)
    except (ManifestError, urllib.error.URLError) as exc:
        # 预期异常：清单获取/解析失败，信息清晰，不误判为"无更新"
        print(f"[check_update] 清单解析失败: {exc}", file=sys.stderr)
        emit("updated", "error")
        return 1   # 非零退出让 job 标红，避免每月 cron 静默失败
    except Exception:  # noqa: BLE001 —— 未知异常（如清单结构变化）打印完整堆栈
        traceback.print_exc()
        emit("updated", "error")
        return 1

    print(f"[check_update] 清单最新 MSVC v{msvc_full} / SDK build {sdk_build}")

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        # 非 CI 环境（本地调试）：没有可对比的 Release，视为有新版本
        print("[check_update] 非 CI 环境（无 GITHUB_REPOSITORY），按首次发布处理")
        emit("updated", "true")
        emit("msvc_full", msvc_full)
        emit("sdk_build", sdk_build)
        return 0

    try:
        published = max_published_version(repo, os.environ.get("GITHUB_TOKEN", ""))
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"[check_update] 查询 GitHub Releases 失败: {exc}", file=sys.stderr)
        emit("updated", "error")
        emit("msvc_full", msvc_full)
        emit("sdk_build", sdk_build)
        return 1   # 同上：非零退出，让故障可见
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        emit("updated", "error")
        emit("msvc_full", msvc_full)
        emit("sdk_build", sdk_build)
        return 1

    if published:
        print(f"[check_update] 已发布最新版本: {TAG_PREFIX}{published}")
        if version_key(msvc_full) <= version_key(published):
            print("[check_update] 无更新，跳过构建")
            emit("updated", "false")
            emit("msvc_full", msvc_full)
            emit("sdk_build", sdk_build)
            return 0

    print("[check_update] 检测到新版本，需要构建发布")
    emit("updated", "true")
    emit("msvc_full", msvc_full)
    emit("sdk_build", sdk_build)
    return 0


if __name__ == "__main__":
    sys.exit(main())
