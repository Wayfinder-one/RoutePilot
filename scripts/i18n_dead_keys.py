"""Find i18n keys that no console page references (dead dictionary entries).

The console dictionary is a plain object literal; a key that no page reads is
dead weight (and a lie: it looks like a translated string but nothing shows
it). This script lists the unused keys so they can be deleted.

Usage:  python scripts/i18n_dead_keys.py [--list]
Exit code 1 when dead keys exist (usable as a CI lint).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CONSOLE = Path(__file__).resolve().parent.parent / "router" / "console"

#: t('key') / tf('key') with a literal key.
LITERAL = re.compile(r"\b(?:t|tf)\(\s*'([^']+)'")
#: t('prefix' + x) — every key below the prefix counts as used.
DYNAMIC = re.compile(r"\b(?:t|tf)\(\s*'([^']*)'\s*\+")
#: data-i18n="key" / data-i18n-aria="key" in the HTML pages.
ATTR = re.compile(r"data-i18n(?:-aria)?=\"([^\"]+)\"")
#: data-i18n="prefix${expr}" template literals (nav.js builds the nav this way).
ATTR_DYNAMIC = re.compile(r'data-i18n(?:-aria)?="([^"$]*)\$\{')


#: One dictionary entry: 'key': value. The key sits at the start of a line
#: (after the indentation) or right after a comma — a value that happens to
#: contain a colon is not a key.
ENTRY = re.compile(r"(?:^|[,{])\s*'([^']+)'\s*:", re.MULTILINE)


def dictionary_keys() -> list[str]:
    """Every key of the zh dictionary (the en one mirrors it)."""
    text = (CONSOLE / "i18n.js").read_text(encoding="utf-8")
    zh = text.split("  zh: {", 1)[1].split("\n  },\n  en: {", 1)[0]
    return ENTRY.findall(zh)


def page_sources() -> str:
    parts = []
    paths = (list(CONSOLE.glob("*.js")) + list(CONSOLE.glob("*.html")) +
             list(CONSOLE.glob("routing/*.js")))
    for path in sorted(paths):
        if path.name == "i18n.js":
            continue
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def dead_keys() -> tuple[list[str], list[str]]:
    """Keys that no page mentions at all.

    A key counts as used when its literal appears anywhere in the page
    sources — not only in a ``t('key')`` call: keys are also passed around as
    plain strings (``['ov.total', value]`` -> ``t(k)``). Being generous here
    only keeps a few extra keys; being strict would delete live strings.
    """
    source = page_sources()
    prefixes = DYNAMIC.findall(source) + ATTR_DYNAMIC.findall(source)
    dead = []
    for key in dictionary_keys():
        if f"'{key}'" in source or f'"{key}"' in source:
            continue
        if any(key.startswith(p) for p in prefixes):
            continue
        dead.append(key)
    return dead, sorted(prefixes)


def main() -> int:
    dead, prefixes = dead_keys()
    total = len(dictionary_keys())
    print(f"i18n keys: {total}, dynamic prefixes: {len(prefixes)}")
    if "--list" in sys.argv:
        for key in dead:
            print(" ", key)
    else:
        print(f"dead keys: {len(dead)}")
    return 1 if dead else 0


if __name__ == "__main__":
    raise SystemExit(main())
