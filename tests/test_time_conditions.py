from datetime import datetime

from router.routing.conditions import TimePeriod, matching_periods, when_matches
from router.routing.model import When


def test_overnight_period_preserves_current_weekday_semantics():
    period = TimePeriod("夜间", days=(0,), start="22:00", end="06:00")
    assert matching_periods(datetime(2026, 10, 26, 23, 0), [period]) == ["夜间"]
    assert matching_periods(datetime(2026, 10, 26, 5, 0), [period]) == ["夜间"]
    assert matching_periods(datetime(2026, 10, 26, 12, 0), [period]) == []


def test_period_end_is_exclusive_and_equal_start_end_means_all_day():
    work = TimePeriod("白天", days=(0, 1, 2, 3, 4), start="09:00", end="18:00")
    weekend = TimePeriod("周末", days=(5, 6), start="00:00", end="00:00")
    assert matching_periods(datetime(2026, 10, 26, 9, 0), [work]) == ["白天"]
    assert matching_periods(datetime(2026, 10, 26, 18, 0), [work]) == []
    assert matching_periods(datetime(2026, 10, 31, 18, 0), [weekend]) == ["周末"]


def test_when_periods_are_or_and_empty_condition_matches():
    assert when_matches(When(periods=("白天", "夜间")), ["夜间"]) is True
    assert when_matches(When(periods=("白天",)), ["周末"]) is False
    assert when_matches(None, []) is True


def test_invalid_period_name_raises_value_error():
    import pytest

    with pytest.raises(ValueError):
        TimePeriod.from_dict({"name": 3, "days": [0], "start": "09:00", "end": "18:00"})
