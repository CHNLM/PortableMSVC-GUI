# 便携版 MSVC 工具链（GUI）

基于 [mmozeiko/portable-msvc](https://github.com/mmozeiko/portable-msvc) 思路的一键工具：
下载 MSVC 编译器 + Windows SDK，无需安装 Visual Studio，自动生成绿色工具链与 `setup_*.bat` 环境脚本，并可打包为压缩包分发。

## 功能

- 可视化配置：VS 通道（latest / 2026 / 2022 / 2019）、主机/目标架构、MSVC/SDK 版本
- 下载全程 SHA256 校验 + 断点续传 + 自动重试（中断/失败后自动复用缓存续传；
  执行成功后清理下载缓存以释放空间）
- 解包 zip / MSI / CAB，自动整理瘦身（移除遥测 vctip.exe、无用目录等）
- 生成环境脚本：`setup_<target>.bat`（会话级）、`setup_<target>_install.bat` /
  `setup_<target>_uninstall.bat`（写入 / 移除当前用户环境变量；PATH / INCLUDE / LIB
  及 WindowsSDKVersion 等）
- 打包 7z → tar → PowerShell 三级回退，GUI 可选压缩级别（快速 / 均衡 / 高压缩）
- 环境自检（平台 / msiexec / 7z / 磁盘空间 / 网络）

## 目录结构

```
PortableMSVC-GUI/
├── app/
│   ├── main.py             # 入口（含全局异常钩子）
│   ├── core/               # 纯业务逻辑，无 GUI 依赖
│   │   ├── common.py       # 公共小工具（User-Agent / 版本比较 / URL 转义）
│   │   ├── models.py       # 配置/结果/环境检查数据模型
│   │   ├── manifest.py     # VS / Windows SDK 清单解析
│   │   ├── downloader.py   # 流式下载 + 断点续传 + SHA256 校验
│   │   ├── extractor.py    # zip / msi / cab 解包（含 Zip Slip 防护）
│   │   ├── environment.py  # 运行环境自检
│   │   ├── packager.py     # 7z / tar / PowerShell 打包
│   │   └── pipeline.py     # 流水线编排
│   ├── ui/                 # PySide6 界面（main_window.py + theme.qss）
│   ├── utils/logger.py     # 日志落盘（线程安全 + 轮转）
│   └── workers/            # QThread 后台任务
├── scripts/                # CI 脚本（check_update.py / build_headless.py）
├── tests/                  # 回归测试（仅标准库，core 层无需 PySide6）
├── build_nuitka.py         # Nuitka 一键打包脚本
├── requirements.txt
└── requirements-dev.txt
```

## 开发运行

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.main            # 需在代码目录（含 app/ 的目录）下运行
```

## 测试

```bash
# 全部回归测试（仅标准库，无需 PySide6）
# 覆盖：CAB 提取、下载校验重试、流水线备份/取消/进度、脚本渲染、
#       打包子进程超时与取消、清单防御、zip 解包取消
python -m unittest discover -s tests -q

# 指定模块
python -m unittest tests.test_extractor tests.test_downloader tests.test_pipeline \
                  tests.test_packager tests.test_manifest -v
```

## 打包为 exe（Nuitka）

```bash
pip install -r requirements-dev.txt
python build_nuitka.py            # 完整打包；--clean 清理缓存后重打
# 产物: dist/PortableMSVC-GUI.dist/  （分发整个目录，勿只拷 exe）
```

说明：

- 使用 **Nuitka** 编译打包（Python 源码编译为机器码，非解释器打包），
  产物体积更小、性能更好、不易反编译；PySide6 插件自动收集 Qt 运行库。
- 需要可用的 C 编译器：本机 MSVC 工具链（cl.exe）或 MinGW，Nuitka 自动检测。
- 图标 / 版本信息 / 主题样式已由 `build_nuitka.py` 自动带上。
- 更详细的参数见 `build_nuitka.py` 顶部注释（`--clean`、`--jobs`）。

## 自动发布（GitHub Actions）

每月自动检测 MSVC 最新版本，有更新即自动构建便携版并发布到 GitHub Release：

- `.github/workflows/release.yml` —— 每月 1 日 02:00 UTC 触发（也可手动触发）
- `PortableMSVC-GUI/scripts/check_update.py` —— 解析 VS 清单取最新 MSVC 版本，
  与已发布的 `msvc-*` Release 对比，无更新时直接结束，不消耗 Windows runner
- `PortableMSVC-GUI/scripts/build_headless.py` —— 无 GUI 运行核心流水线
  （复用 `app/core/pipeline.py`），归档按版本号命名：`PortableMSVC-<版本>.7z`

Release tag 格式：`msvc-<MSVC完整版本>`（如 `msvc-14.51.36252`）。
发布成功后仅保留最近 5 个 `msvc-*` Release，更旧的自动删除（连带 tag）。

手动触发（Actions → 每月 MSVC 便携版自动发布 → Run workflow）可指定：

| 参数 | 说明 | 默认 |
|------|------|------|
| `vs` | VS 通道：latest / 2026 / 2022 / 2019 | latest |
| `targets` | 目标架构组合：x64,x86 / x64 / +arm64 / 全架构 | x64,x86 |

手动触发会忽略"是否有更新"的判断、强制构建（适用于更换架构组合或补发版本）。

首次部署：将仓库推送到 GitHub 后，在 Actions 页面手动触发一次。

> 注意：GitHub 对 60 天无提交的仓库会暂停 schedule 触发，届时手动触发一次即可。

## 许可与注意事项

- 使用前需接受 Visual Studio 许可协议（界面内勾选）
- 下载内容来自微软官方 aka.ms 渠道，全程 https + SHA256 校验
