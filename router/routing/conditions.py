from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from router.routing.model import When


def _minutes(value: str) -> int:
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (ValueError, TypeError) as exc:
        raise ValueError("time must use HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("time must use HH:MM")
    return hour * 60 + minute


@dataclass(frozen=True)
class TimePeriod:
    name: str
    days: tuple[int, ...]
    start: str
    end: str

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("period name must be nonempty")
        if not self.days or any(isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in self.days):
            raise ValueError("period days must contain weekdays in [0,6]")
        if len(set(self.days)) != len(self.days):
            raise ValueError("period days must be unique")
        _minutes(self.start)
        _minutes(self.end)

    @classmethod
    def from_dict(cls, raw: dict) -> "TimePeriod":
        if not isinstance(raw, dict) or set(raw) != {"name", "days", "start", "end"}:
            raise ValueError("period needs exactly name/days/start/end")
        days = raw.get("days")
        if not isinstance(days, list):
            raise ValueError("period days must be a list")
        return cls(raw["name"], tuple(days), raw["start"], raw["end"])

    def to_dict(self) -> dict:
        return {"name": self.name, "days": list(self.days), "start": self.start, "end": self.end}


def matching_periods(now: datetime, periods: list[TimePeriod]) -> list[str]:
    matched = []
    weekday = now.weekday()
    minute = now.hour * 60 + now.minute
    for period in periods:
        if weekday not in period.days:
            continue
        start = _minutes(period.start)
        end = _minutes(period.end)
        if start == end:
            hit = True
        elif start < end:
            hit = start <= minute < end
        else:
            hit = minute >= start or minute < end
        if hit:
            matched.append(period.name)
    return matched


def when_matches(when: When | None, active_periods: list[str]) -> bool:
    return when is None or any(period in active_periods for period in when.periods)
