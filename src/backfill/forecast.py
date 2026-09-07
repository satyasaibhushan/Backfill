import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from backfill.schemas import Policy


def reserve(
    samples: list[dict], limit: float, now: float, reset: float, policy: Policy
) -> tuple[float, str]:
    # Require several observed days before treating a quiet afternoon as a habit.
    days = {datetime.fromtimestamp(s["at"], UTC).date() for s in samples}
    if len(samples) < policy.history_min_samples or len(days) < 3:
        return limit * policy.cold_start_user_percent / 100, "cold_start"
    zone = ZoneInfo(policy.timezone)

    def bucket(at: float) -> tuple[int, bool]:
        local = datetime.fromtimestamp(at, zone)
        return local.hour, local.weekday() >= 5

    rates = [s["amount"] / s["duration"] for s in samples]
    grouped: dict[tuple[int, bool], list[float]] = {}
    for sample, rate in zip(samples, rates, strict=True):
        grouped.setdefault(bucket(sample["at"]), []).append(rate)

    def upper_quartile(values: list[float]) -> float:
        return sorted(values)[math.ceil(len(values) * 0.75) - 1]

    baseline = upper_quartile(rates)
    total = 0.0
    cursor = now
    while cursor < reset:
        duration = min(3600, reset - cursor)
        matching = grouped.get(bucket(cursor), [])
        rate = upper_quartile(matching) if len(matching) >= 3 else baseline
        total += rate * duration
        cursor += duration
    return min(
        limit, max(limit * policy.minimum_user_percent / 100, total * policy.forecast_multiplier)
    ), "history"
