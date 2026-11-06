"""Keep the offline experiment runnable from either the repository root or this directory.

`python -m pytest` from the repository root already exposes the root package; this
conftest makes the in-directory command independent of that working directory and
of the root conftest.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]

for entry in (str(HERE), str(REPOSITORY_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)
