"""核心业务层：纯 Python 逻辑，不依赖任何 GUI 框架。

模块职责：
- common      公共小工具（User-Agent / 版本比较 / URL 转义）
- models      数据模型（配置 / 结果 / 环境检查项）
- manifest    Visual Studio 与 Windows SDK 清单解析
- downloader  带 SHA256 校验与缓存的文件下载
- extractor   zip / msi / cab 解包
- packager    7z / tar / PowerShell 打包
- environment 运行环境自检
- pipeline    整体流水线编排（状态机 + 进度回调）
"""
