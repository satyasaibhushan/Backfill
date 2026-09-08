import pytest

from backfill.inference import CalibrationCycle, ModelPrice, ReferenceBudget, capacity
from backfill.schemas import InferenceUsage, RunUsage
from backfill.usage import Usage


def test_prices_include_cache_and_require_exact_model_and_mode():
    price = ModelPrice(
        model="fixture",
        mode="standard",
        version="v1",
        source="test fixture",
        input_per_million=10,
        output_per_million=30,
        cache_read_per_million=1,
        cache_write_per_million=12,
    )
    usage = InferenceUsage(
        model="fixture",
        mode="standard",
        input_tokens=1000000,
        output_tokens=100000,
        cache_read_tokens=2000000,
        cache_write_tokens=100000,
    )
    assert price.cost(usage) == pytest.approx(16.2)
    assert price.cost(usage.model_copy(update={"mode": "fast"})) is None
    assert price.cost(usage.model_copy(update={"model": None})) is None


def test_account_calibration_excludes_other_consumers_and_requires_history():
    samples = [
        CalibrationCycle(
            account="a",
            window="weekly",
            reset=i,
            mode="standard",
            price_version="v1",
            reference_cost=20,
            used_percent=10,
            coverage="complete",
        )
        for i in range(3)
    ]
    args = dict(account="a", window="weekly", mode="standard", price_version="v1")
    assert capacity(samples[:2], **args) is None
    assert capacity([samples[0]] * 3, **args) is None
    with pytest.raises(ValueError, match="conflicting"):
        capacity([*samples, samples[0].model_copy(update={"reference_cost": 21})], **args)
    assert capacity(samples, **args) == pytest.approx(170)
    contaminated = samples[0].model_copy(update={"coverage": "partial", "reference_cost": 9000})
    assert capacity([*samples, contaminated], **args) == pytest.approx(170)
    assert capacity(samples, **(args | {"mode": "fast"})) is None
    assert capacity(samples, **(args | {"price_version": "v2"})) is None
    assert capacity(samples, **(args | {"account": "b"})) is None


def test_project_reference_allowance_boundary():
    budget = ReferenceBudget(capacity=170, allowance_percent=20, consumed=33.99)
    assert not budget.exhausted
    assert budget.model_copy(update={"consumed": 34}).exhausted


def test_detailed_native_usage_excludes_cached_input_from_regular_price():
    usage = Usage("codex")
    event = {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "one",
            "tokenUsage": {
                "total": {
                    "totalTokens": 120,
                    "inputTokens": 100,
                    "cachedInputTokens": 80,
                    "outputTokens": 20,
                    "reasoningOutputTokens": 10,
                }
            },
        },
    }
    usage.consume(event)
    usage.consume(event)
    assert len(usage.inference) == 1
    item = InferenceUsage.model_validate(usage.inference[0])
    assert (item.input_tokens, item.cache_read_tokens, item.output_tokens) == (20, 80, 20)
    assert item.model is None and item.mode == "unknown"
    # Reasoning output is already part of output; never charge it twice.
    assert usage.tokens == 120


def test_total_only_native_events_are_not_priced_as_zero():
    usage = Usage("codex")
    usage.consume(
        {
            "method": "thread/tokenUsage/updated",
            "params": {"threadId": "one", "tokenUsage": {"total": {"totalTokens": 120}}},
        }
    )
    assert usage.tokens == 120
    assert usage.inference == []


def test_result_replaces_stream_details_without_double_charging():
    usage = Usage("claude")
    usage.consume(
        {
            "type": "assistant",
            "message": {
                "id": "a",
                "model": "fixture",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }
    )
    usage.consume(
        {
            "type": "result",
            "modelUsage": {
                "fixture": {"inputTokens": 20, "outputTokens": 10, "cacheReadInputTokens": 30}
            },
        }
    )
    assert len(usage.inference) == 1
    assert usage.inference[0]["input_tokens"] == 20
    assert usage.inference[0]["cache_read_tokens"] == 30
    assert usage.inference[0]["mode"] == "unknown"


def test_history_survives_restart_and_report_replay(setup, observe):
    from backfill.config import Settings
    from backfill.database import Database
    from backfill.governor import Governor
    from backfill.meter import Meter
    from backfill.schemas import RunStart, TaskBudget

    service, clock = setup
    clock[0] += 90
    observe(used=1)
    clock[0] += 90
    observe(used=2)
    Meter(service, Settings(meter_enabled=False)).bind("account", "codex")
    governor = Governor(service)
    governor.set_budget("bulk", TaskBudget(token_limit=1000))
    run = governor.start("bulk", RunStart(provider="codex", request_id="history"))
    report = RunUsage(sequence=0, tokens=10, inference=[InferenceUsage(input_tokens=10)])
    governor.report("bulk", run["run_id"], report)
    governor.report("bulk", run["run_id"], report)
    db = Database(service.database.path)
    with db.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM quota_history").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM inference_history").fetchone()[0] == 1
