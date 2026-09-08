"""Put every project package on sys.path.

The project folders are hyphenated (`p01-structured-output/`) because the
assignment asks for one readable subfolder per project; the importable package
inside each is underscored (`p01_structured_output/`). This bridges the two so
tests and the FastAPI server can import them without an install step per project.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for path in [ROOT, ROOT / "core", *sorted(ROOT.glob("p[0-9][0-9]-*"))]:
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))
