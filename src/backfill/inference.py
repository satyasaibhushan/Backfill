"""Reference inference prices and subscription estimates, never subscription invoices."""

from typing import Literal

from pydantic import Field

from backfill.schemas import Amount, Contract, InferenceUsage, PositiveAmount


class ModelPrice(Contract):
    model: str
    mode: str
    version: str
    source: str
    input_per_million: Amount
    output_per_million: Amount
    cache_read_per_million: Amount
    cache_write_per_million: Amount

    def cost(self, usage: InferenceUsage) -> float | None:
        # Fast and standard pricing must never silently share a rate.
        if usage.model is None or usage.mode == "unknown":
            return None
        if (usage.model, usage.mode) != (self.model, self.mode):
            return None
        return (
            usage.input_tokens * self.input_per_million
            + usage.output_tokens * self.output_per_million
            + usage.cache_read_tokens * self.cache_read_per_million
            + usage.cache_write_tokens * self.cache_write_per_million
        ) / 1_000_000


class CalibrationCycle(Contract):
    account: str
    window: str
    reset: float
    mode: str
    price_version: str
    reference_cost: Amount
    used_percent: float = Field(ge=0, le=100)
    coverage: Literal["complete", "partial", "unknown"]


def capacity(
    samples: list[CalibrationCycle],
    *,
    account: str,
    window: str,
    mode: str,
    price_version: str,
    buffer: float = 0.15,
) -> float | None:
    """Conservative capacity from complete observations of distinct reset cycles.

    Callers must establish coverage across ALL consumers of the account. A run's
    own tokens do not establish account coverage. Small quota movements are too
    sensitive to rounded and delayed provider readings to train on.
    """
    if not 0 <= buffer < 1:
        raise ValueError("buffer must be between zero and one")
    cycles: dict[float, tuple[float, float]] = {}
    for sample in samples:
        if (
            (sample.account, sample.window, sample.mode, sample.price_version)
            != (account, window, mode, price_version)
            or sample.coverage != "complete"
            or sample.mode == "unknown"
        ):
            continue
        value = (sample.reference_cost, sample.used_percent)
        if sample.reset in cycles and cycles[sample.reset] != value:
            raise ValueError("conflicting observations for a reset cycle")
        cycles[sample.reset] = value
    estimates = [
        cost * 100 / movement
        for cost, movement in cycles.values()
        if cost > 0 and 5 <= movement <= 100
    ]
    if len(estimates) < 3:
        return None
    # Use the lower quartile instead of a generous mean that outliers can inflate.
    estimates.sort()
    return estimates[(len(estimates) - 1) // 4] * (1 - buffer)


class ReferenceBudget(Contract):
    capacity: PositiveAmount
    allowance_percent: float = Field(gt=0, le=100)
    consumed: Amount = 0

    @property
    def exhausted(self) -> bool:
        return self.consumed >= self.capacity * self.allowance_percent / 100
