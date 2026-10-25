"""pytest 共享引导：src/ 入 path，测试文件不再各自复制样板。"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = str(BASE / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
