"""Reset and reseed the Duebell demo database (issue #13)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.seed import main

if __name__ == "__main__":
    sys.argv.append("--reset")
    main()
