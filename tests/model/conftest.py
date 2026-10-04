import sys
from pathlib import Path

MODEL = Path(__file__).resolve().parents[2] / "model"
if str(MODEL) not in sys.path:
    sys.path.insert(0, str(MODEL))
