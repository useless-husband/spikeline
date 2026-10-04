import sys
from pathlib import Path

HERE = Path(__file__).parent
for p in (HERE, HERE.parents[1] / "model"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
