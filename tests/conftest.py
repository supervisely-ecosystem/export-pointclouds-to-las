import sys
from pathlib import Path

# The app imports its modules as `src.<module>` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
