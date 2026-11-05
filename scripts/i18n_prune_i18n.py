"""Delete the dead i18n keys from router/console/i18n.js.

The console dictionary is a plain object literal; a key that no page reads is
dead weight. ``scripts/i18n_dead_keys.py`` finds them, this script removes
them from both the zh and the en dictionary.

Usage:  python scripts/i18n_prune_i18n.py [--dry-run]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n_dead_keys import dead_keys  # noqa: E402

I18N = Path(__file__).resolve().parent.parent / "router" / "console" / "i18n.js"


def entry_pattern(key: str) -> re.Pattern:
    """``'key': 'value',`` — the value may contain escaped quotes.

    Only spaces/tabs match the padding: a newline must never be consumed, or
    two dictionary lines would be merged into one.
    """
    return re.compile(r"'" + re.escape(key)
                      + r"'[ \t]*:[ \t]*'(?:[^'\\]|\\.)*'[ \t]*,?[ \t]?")


def prune(text: str, keys: list[str]) -> str:
    """Remove every occurrence of the given keys (zh and en dictionaries)."""
    for key in keys:
        pattern = entry_pattern(key)
        while True:
            match = pattern.search(text)
            if not match:
                break
            start, end = match.span()
            line_start = text.rfind("\n", 0, start) + 1
            line_end = text.find("\n", end)
            if line_end == -1:
                line_end = len(text)
            head = text[line_start:start]
            tail = text[end:line_end]
            if not (head.strip() or tail.strip()):
                # the entry was alone on its line -> drop the whole line
                text = (text[:line_start]
                        + (text[line_end + 1:] if line_end < len(text) else ""))
            else:
                text = text[:start] + text[end:]
    return text


def main() -> int:
    dead, _ = dead_keys()
    if not dead:
        print("no dead keys")
        return 0
    text = I18N.read_text(encoding="utf-8")
    new = prune(text, dead)
    if "--dry-run" in sys.argv:
        print(f"would remove {len(dead)} keys")
        return 0
    I18N.write_text(new, encoding="utf-8")
    print(f"removed {len(dead)} dead keys")
    leftover, _ = dead_keys()
    if leftover:
        print(f"still dead after prune: {len(leftover)}", file=sys.stderr)
        for key in leftover:
            print(f"  {key}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
