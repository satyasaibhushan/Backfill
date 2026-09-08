from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backfill.database import Database
from backfill.demo import run_demo
from backfill.quota import QuotaError, QuotaService
from backfill.schemas import Acquire, Observation, Policy, Report, WorkloadInput


def request(service, key="bulk", request_id="one", session=10, weekly=1, ttl=60):
    return service.acquire(
        key,
        Acquire(
            request_id=request_id, costs={"session": session, "weekly": weekly}, ttl_seconds=ttl
        ),
    )


def report(service, grant, session, weekly=1, report_id="done", final=True):
    return service.report(
        "bulk",
        grant.grant_id,
        Report(report_id=report_id, consumed={"session": session, "weekly": weekly}, final=final),
    )


def window(service, key="bulk"):
    return service.status(key)["windows"]["session"]


def test_missing_and_stale_readings_fail_closed(setup, observe):
    service, clock = setup
    service.set_account("empty", Policy())
    service.set_workload("empty", WorkloadInput(account="empty"))
    assert request(service, "empty").reason == "missing_observation"
    clock[0] += 181
    assert request(service).reason == "stale_observation"
    clock[0] += 400
    observe(coverage=clock[0] - 400)
    assert request(service).reason == "stale_coverage"


def test_every_window_is_required_and_limiting(setup, observe):
    service, clock = setup
    with pytest.raises(QuotaError, match="every observed"):
        service.acquire("bulk", Acquire(request_id="bad", costs={"session": 1}))
    clock[0] += 1
    observe(weekly=80)
    assert request(service, "urgent").decision == "wait"


def test_priority_earmarks_capacity_without_spending_user_reserve(setup, observe):
    service, clock = setup
    clock[0] += 1
    observe(used=60)
    assert request(service, session=1).decision == "wait"
    assert request(service, "urgent", session=10).decision == "granted"
    assert request(service, "urgent", "next", session=6).decision == "wait"
    assert window(service, "urgent")["user_reserve"] == 20


def test_acquire_idempotency_including_expired_and_conflicts(setup, observe):
    service, clock = setup
    first = request(service)
    assert request(service).grant_id == first.grant_id
    assert window(service)["reserved"] == 10
    with pytest.raises(QuotaError, match="different parameters"):
        request(service, session=11)
    clock[0] += 61
    assert request(service).reason == "expired"
    assert window(service)["unconfirmed_usage"] == 10
    assert window(service)["reserved"] == 0


def test_partial_report_final_report_and_coverage_do_not_double_charge(setup, observe):
    service, clock = setup
    grant = request(service, session=20)
    report(service, grant, 4, final=False, report_id="part")
    state = window(service)
    assert state["reserved"] == 16 and state["unconfirmed_usage"] == 4
    clock[0] += 30
    observe(used=4, weekly=1)
    assert window(service)["unconfirmed_usage"] == 0
    report(service, grant, 8)
    state = window(service)
    assert state["reserved"] == 0 and state["unconfirmed_usage"] == 4
    assert state["available"] == 52
    clock[0] += 30
    observe(used=8, weekly=1)
    assert window(service)["available"] == 52


def test_reports_are_cumulative_idempotent_and_charge_overruns(setup, observe):
    service, _ = setup
    grant = request(service)
    result = report(service, grant, 15)
    assert result["overrun"]["session"] == 5
    assert report(service, grant, 15) == result
    assert window(service)["unconfirmed_usage"] == 15
    with pytest.raises(QuotaError, match="different parameters"):
        report(service, grant, 16)
    with pytest.raises(QuotaError, match="already finalized"):
        report(service, grant, 16, report_id="new")


def test_consumption_cannot_decrease_or_drop_a_window(setup, observe):
    service, _ = setup
    grant = request(service)
    report(service, grant, 5, final=False, report_id="part")
    with pytest.raises(QuotaError, match="cannot decrease"):
        report(service, grant, 4)
    with pytest.raises(QuotaError, match="every reserved"):
        service.report("bulk", grant.grant_id, Report(report_id="x", consumed={"session": 6}))


def test_expiry_keeps_uncertainty_until_late_final_report(setup, observe):
    service, clock = setup
    grant = request(service, session=20)
    report(service, grant, 4, final=False, report_id="part")
    clock[0] += 61
    assert window(service)["unconfirmed_usage"] == 20
    observe(used=4, weekly=1)
    assert window(service)["unconfirmed_usage"] == 16
    with pytest.raises(QuotaError, match="uncertain"):
        service.release("bulk", grant.grant_id)
    with pytest.raises(QuotaError, match="final report"):
        report(service, grant, 8, final=False)
    report(service, grant, 8)
    assert window(service)["unconfirmed_usage"] == 4


def test_release_keeps_already_spent_usage(setup, observe):
    service, _ = setup
    grant = request(service)
    report(service, grant, 4, final=False, report_id="part")
    assert service.release("bulk", grant.grant_id)["state"] == "released"
    assert service.release("bulk", grant.grant_id)["state"] == "released"
    assert window(service)["reserved"] == 0
    assert window(service)["unconfirmed_usage"] == 4


def test_pause_and_external_spending_revoke_permission_but_keep_reservations(setup, observe):
    service, clock = setup
    grant = request(service, session=50)
    service.set_workload("bulk", WorkloadInput(account="account", priority=0, paused=True))
    assert request(service, session=50).reason == "paused"
    assert not service.status("bulk")["grants"][0]["can_spend"]
    service.set_workload("bulk", WorkloadInput(account="account", priority=0))
    clock[0] += 1
    observe(used=20)
    assert service.status("bulk")["grants"][0]["reason"] == "headroom_changed"
    assert window(service)["reserved"] == 50
    service.release("bulk", grant.grant_id)


def test_resets_need_fresh_observation_and_do_not_reset_other_windows(setup, observe):
    service, clock = setup
    clock[0] += 3590
    observe(used=5, weekly=80)
    assert request(service, "urgent", weekly=0).expires_at.timestamp() == clock[0] + 10
    clock[0] += 11
    assert request(service, "urgent", "after").reason == "reset_needs_observation"
    observe(used=0, weekly=80, reset=clock[0] + 18000)
    assert request(service, "urgent", "after").reason == "insufficient_headroom"


def test_concurrent_process_connections_cannot_overbook(setup, observe):
    service, clock = setup
    path = service.database.path

    # Separate service/database objects exercise SQLite's cross-connection transaction boundary.
    def acquire(index):
        other = QuotaService(Database(path), lambda: clock[0])
        return request(other, "urgent", str(index), session=10, weekly=0).decision

    with ThreadPoolExecutor(max_workers=12) as pool:
        decisions = list(pool.map(acquire, range(20)))
    assert decisions.count("granted") == 7
    assert window(service, "urgent")["reserved"] == 70


def test_reopening_database_preserves_reservations_and_idempotency(setup, observe):
    service, clock = setup
    grant = request(service)
    restarted = QuotaService(Database(service.database.path), lambda: clock[0])
    assert request(restarted).grant_id == grant.grant_id
    assert window(restarted)["reserved"] == 10


def test_observation_rejects_identity_loss_window_loss_and_time_travel(setup, observe):
    service, clock = setup
    clock[0] += 1
    with pytest.raises(QuotaError, match="account changed"):
        observe(source_account="other")
    stored = service.overview()["accounts"][0]["observation"]
    observation = Observation.model_validate(stored)
    observation.observed_at = datetime.fromtimestamp(clock[0], UTC)
    observation.windows = observation.windows[:1]
    with pytest.raises(QuotaError, match="omitted"):
        service.observe("account", observation)
    observe(used=10)
    clock[0] += 1
    with pytest.raises(QuotaError, match="usage decreased"):
        observe(used=9)
    observation.observed_at = datetime.fromtimestamp(clock[0] + 100, UTC)
    with pytest.raises(QuotaError, match="future"):
        service.observe("account", observation)


def test_reported_spending_is_subtracted_from_historical_external_demand(setup, observe):
    service, clock = setup
    grant = request(service)
    report(service, grant, 10)
    clock[0] += 120
    observe(used=15, weekly=1)
    with service.database.transaction() as db:
        samples = list(db.execute("SELECT * FROM demand WHERE window='session'"))
    assert len(samples) == 1 and samples[0]["amount"] == 5


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1])
def test_invalid_costs_rejected(amount):
    with pytest.raises(ValidationError):
        Acquire(request_id="x", costs={"session": amount})


def test_demo_exercises_adaptation_and_is_reproducible():
    first = run_demo()
    assert run_demo() == first
    events = {event["step"]: event for event in first}
    assert events["quiet-history"]["user_reserve"] < events["busy-history"]["user_reserve"]
    assert events["complete"]["provider_requests"] == 0


def test_unused_model_window_does_not_block_an_existing_grant(setup, observe):
    service, clock = setup
    clock[0] += 1
    observe(weekly=90)
    grant = request(service, weekly=0)
    assert grant.decision == "granted"
    assert service.status("bulk")["grants"][0]["can_spend"]


def test_moving_reset_cannot_erase_spending_or_reservations(setup, observe):
    service, clock = setup
    original = clock[0]
    grant = request(service, session=20)
    report(service, grant, 4, final=False, report_id="part")
    clock[0] += 10
    observe(reset=original + 3610, coverage=original - 1)
    assert window(service)["reserved"] == 16
    assert window(service)["unconfirmed_usage"] == 4
    assert window(service)["available"] == 40
    clock[0] += 60
    observe(reset=original + 3670, coverage=original - 1)
    assert window(service)["unconfirmed_usage"] == 20
    clock[0] += 1
    observe(used=4, reset=original + 3671)
    assert window(service)["unconfirmed_usage"] == 16


def test_changed_reset_does_not_accept_decreased_usage_before_coverage(setup, observe):
    service, clock = setup
    clock[0] += 10
    observe(used=20)
    clock[0] += 10
    with pytest.raises(QuotaError, match="confirmed reset"):
        observe(used=0, reset=clock[0] + 3600)


def test_same_quota_pool_cannot_be_double_registered(setup, observe):
    service, _ = setup
    service.set_account("alias", Policy())
    snapshot = Observation.model_validate(service.overview()["accounts"][0]["observation"])
    with pytest.raises(QuotaError, match="already registered"):
        service.observe("alias", snapshot)


def test_direct_grant_status_is_scoped_and_survives_many_later_grants(setup, observe):
    service, _ = setup
    first = request(service, session=1, weekly=0)
    for index in range(55):
        grant = request(service, request_id=str(index), session=1, weekly=0)
        service.release("bulk", grant.grant_id)
    assert service.grant_status("bulk", first.grant_id)["can_spend"]
    with pytest.raises(QuotaError, match="not found"):
        service.grant_status("urgent", first.grant_id)


def test_confirmed_correction_keeps_existing_reservations(setup, observe):
    from datetime import timedelta

    service, clock = setup
    clock[0] += 1
    observe(used=30)
    grant = request(service, session=5)
    before = window(service)["reserved"]
    old = next(a for a in service.overview()["accounts"] if a["key"] == "account")
    sample = Observation.model_validate(old["observation"])
    clock[0] += 30
    sample.observed_at = sample.covered_through = datetime.fromtimestamp(clock[0], UTC)
    sample.windows[0].used = 3
    sample.windows[0].resets_at += timedelta(minutes=5)
    with pytest.raises(QuotaError, match="decreased"):
        service.observe("account", sample)
    service.observe("account", sample, _corrections=frozenset({"session"}))
    assert window(service)["reserved"] == before
    assert service.status("bulk")["windows"]["session"]["available"] <= 62
    assert grant.grant_id
