"""Run the local regression checks without Appium or an iPhone."""

import runpy
import sys
from pathlib import Path

tests_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(tests_dir.parent))

for test in sorted(tests_dir.glob("test_*.py")):
    print(f"Running {test.name}", flush=True)
    runpy.run_path(str(test))
