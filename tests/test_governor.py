from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from backfill.config import Settings
from backfill.governor import Governor
from backfill.meter import Meter
from backfill.quota import QuotaError
from backfill.schemas import Policy, ProjectBudget, RunStart, RunUsage, TaskBudget


@pytest.fixture
def governed(setup, observe):
    service, clock = setup
    meter = Meter(service, Settings(meter_enabled=False))
    meter.bind("account", "codex")
    governor = Governor(service)
    governor.set_project("history", ProjectBudget(token_limit=1000))
    governor.set_budget("bulk", TaskBudget(token_limit=800, run_token_limit=600, project="history"))
    governor.set_budget(
        "urgent", TaskBudget(token_limit=800, run_token_limit=600, project="history")
    )
    return governor, service, clock


def start(governor, key="bulk", request_id="one"):
    return governor.start(key, RunStart(provider="codex", request_id=request_id))


def test_atomic_concurrency_and_replayed_start_never_launch_twice(governed):
    g, _, _ = governed
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda i: start(g, request_id=str(i)), range(2)))
    assert sum(r["decision"] == "granted" for r in results) == 1
    accepted = next(i for i, r in enumerate(results) if r["decision"] == "granted")
    assert start(g, request_id=str(accepted))["reason"] == "request_already_used"


def test_project_shared_spending_and_caps_survive_restart(governed):
    g, service, _ = governed
    run = start(g)
    g.report(
        "bulk",
        run["run_id"],
        RunUsage(sequence=0, tokens=500, final=True, complete=True, reason="completed"),
    )
    other = start(Governor(service), "urgent")
    assert other["token_allowance"] == 500
    assert g.overview()["projects"][0]["tokens"] == 500


def test_lower_cap_pause_and_exhaustion_revoke_live_run(governed):
    g, _, clock = governed
    run = start(g)
    g.report("bulk", run["run_id"], RunUsage(sequence=0, tokens=150))
    g.set_project("history", ProjectBudget(token_limit=120))
    state = g.status("bulk", run["run_id"])
    assert not state["can_spend"] and state["overrun_tokens"] == 30
    g.set_project(
        "history",
        ProjectBudget(token_limit=1000, paused_until=datetime.fromtimestamp(clock[0] + 3, UTC)),
    )
    assert g.status("bulk", run["run_id"])["reason"] == "project_paused"
    clock[0] += 4
    assert g.status("bulk", run["run_id"])["can_spend"]


def test_lost_heartbeat_holds_unreported_allowance(governed):
    g, _, clock = governed
    run = start(g)
    g.report("bulk", run["run_id"], RunUsage(sequence=0, tokens=10))
    clock[0] += 11
    assert g.status("bulk", run["run_id"])["reason"] == "heartbeat_lost"
    task = next(t for t in g.overview()["tasks"] if t["key"] == "bulk")
    assert task["held_tokens"] == 590
    retry = g.report("bulk", run["run_id"], RunUsage(sequence=1, tokens=10))
    assert not retry["can_spend"]
    g.report("bulk", run["run_id"], RunUsage(sequence=2, tokens=90, final=True, complete=True))
    assert next(t for t in g.overview()["tasks"] if t["key"] == "bulk")["held_tokens"] == 0


def test_native_tokens_do_not_get_subtracted_from_quota_percent(governed):
    g, service, _ = governed
    run = start(g)
    g.report("bulk", run["run_id"], RunUsage(sequence=0, tokens=250, final=True, complete=True))
    assert service.status("bulk")["windows"]["session"]["unconfirmed_usage"] == 0
    assert g.overview()["tasks"][0]["tokens"] == 250


def test_provider_binding_and_budget_reparenting_are_enforced(governed):
    g, _, _ = governed
    with pytest.raises(QuotaError, match="provider"):
        g.start("bulk", RunStart(provider="claude", request_id="wrong"))
    start(g)
    with pytest.raises(QuotaError, match="cannot move"):
        g.set_budget("bulk", TaskBudget(token_limit=900))


def test_pause_account_and_failure_of_meter_stop_existing_run(governed):
    g, service, clock = governed
    run = start(g)
    service.set_account("account", Policy(paused_until=datetime.fromtimestamp(clock[0] + 60, UTC)))
    assert g.status("bulk", run["run_id"])["reason"] == "paused"
    service.set_account("account", Policy())
    with service.database.transaction() as db:
        db.execute("UPDATE meters SET error='unavailable'")
    assert g.status("bulk", run["run_id"])["reason"] == "meter_unavailable"


def test_report_idempotency_counter_monotonicity_and_baselines(governed):
    g, _, _ = governed
    run = start(g)
    event = RunUsage(sequence=1, tokens=100, counters={"thread-1": 100})
    assert g.report("bulk", run["run_id"], event) == g.report("bulk", run["run_id"], event)
    with pytest.raises(QuotaError):
        g.report("bulk", run["run_id"], RunUsage(sequence=1, tokens=101))
    with pytest.raises(QuotaError):
        g.report("bulk", run["run_id"], RunUsage(sequence=2, tokens=99))
    g.report(
        "bulk",
        run["run_id"],
        RunUsage(sequence=2, tokens=100, final=True, complete=True, counters={"thread-1": 100}),
    )
    assert start(g, request_id="two")["baselines"] == {"thread-1": 100}


def test_native_background_intervals_do_not_train_user_forecast(governed, observe):
    g, service, clock = governed
    run = start(g)
    g.report("bulk", run["run_id"], RunUsage(sequence=1, tokens=150, final=True, complete=True))
    clock[0] += 90
    observe(used=5)
    with service.database.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM demand").fetchone()[0] == 0
