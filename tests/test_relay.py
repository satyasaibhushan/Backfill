from concurrent.futures import ThreadPoolExecutor

from backfill.config import Settings
from backfill.database import Database
from backfill.quota import QuotaService
from backfill.relay import apply_command
from backfill.tasks import Tasks


def test_lost_ack_and_concurrent_redelivery_create_exactly_one_task(tmp_path):
    settings = Settings(data_dir=tmp_path)
    tasks = Tasks(QuotaService(Database(tmp_path / "quota.db")), settings)
    command = {
        "id": "stable-command-id",
        "payload": {
            "method": "POST",
            "path": "/v2/tasks",
            "body": {"title": "One task", "instructions": "Read something"},
        },
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: apply_command(tasks, command), range(4)))
    assert all(result == results[0] for result in results)
    assert len(tasks.list()["tasks"]) == 1
    restarted = Tasks(QuotaService(Database(tmp_path / "quota.db")), settings)
    assert apply_command(restarted, command) == results[0]
    assert len(restarted.list()["tasks"]) == 1


def test_failed_change_is_receipted_without_mutating_task(tmp_path):
    tasks = Tasks(QuotaService(Database(tmp_path / "quota.db")), Settings(data_dir=tmp_path))
    command = {
        "id": "invalid-command",
        "payload": {
            "method": "POST",
            "path": "/v2/tasks",
            "body": {"title": "Task", "instructions": "Read", "folder": str(tmp_path / "missing")},
        },
    }
    first = apply_command(tasks, command)
    assert first["status"] == 422
    assert apply_command(tasks, command) == first
    assert tasks.list()["tasks"] == []
