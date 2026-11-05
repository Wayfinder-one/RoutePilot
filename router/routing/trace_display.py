"""Display metadata attached to routing events without changing machine fields."""

from __future__ import annotations


def bilingual(zh: str, en: str) -> dict[str, str]:
    return {"zh": str(zh), "en": str(en)}


def described(
    event: dict,
    *,
    phase: str,
    label: str | dict[str, str],
    cell: str | dict[str, str],
    summary: str | dict[str, str],
    facts=(),
    tone: str = "",
) -> dict:
    if tone not in {"", "hit", "warn", "bad"}:
        raise ValueError(f"invalid trace tone {tone!r}")
    return {
        **event,
        "phase": phase,
        "label": label,
        "cell": cell,
        "summary": summary,
        "facts": [[key, value] for key, value in facts],
        "tone": tone,
    }
