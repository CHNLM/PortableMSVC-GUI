"""回归测试包。

运行方式（在项目根 PortableMSVC-GUI/ 下）：
    python -m unittest discover -s tests -q
仅依赖标准库（unittest / unittest.mock），core 层无 GUI 依赖，无需 PySide6。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
