"""Apply a hosted command once, atomically with its local task changes."""

import json
import re

from pydantic import ValidationError

from backfill.quota import QuotaError
from backfill.tasks import Preferences, ProjectInput, TaskAction, TaskInput, Tasks


def validate_command(method: str, path: str, body: dict):
    if method == "POST" and path == "/v2/tasks":
        return "create", None, TaskInput.model_validate(body)
    if method == "POST" and path == "/v2/projects":
        return "project", None, ProjectInput.model_validate(body)
    if method == "PUT" and path == "/v2/preferences":
        return "preferences", None, Preferences.model_validate(body)
    match = re.fullmatch(r"/v2/(tasks|projects)/([a-zA-Z0-9_-]{1,80})(/actions)?", path)
    if match:
        kind, key, action = match.groups()
        if kind == "tasks" and action and method == "POST":
            return "action", key, TaskAction.model_validate(body)
        if not action and method == "PUT":
            return (
                ("update", key, TaskInput.model_validate(body))
                if kind == "tasks"
                else ("project", key, ProjectInput.model_validate(body))
            )
    raise QuotaError("Unsupported worker operation", 422)


def apply_command(tasks: Tasks, command: dict) -> dict:
    payload = json.dumps(command["payload"], sort_keys=True)
    database = tasks.quota.database
    with database.atomic() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS relay_commands (id TEXT PRIMARY KEY, payload TEXT NOT"
            " NULL, response TEXT NOT NULL)"
        )
        old = db.execute(
            "SELECT payload,response FROM relay_commands WHERE id=?", (command["id"],)
        ).fetchone()
        if old:
            if old["payload"] != payload:
                raise QuotaError("Command identity conflict", 409)
            return json.loads(old["response"])
        # A savepoint keeps validation failures from leaving partial task changes.
        db.execute("SAVEPOINT command")
        try:
            p = command["payload"]
            operation, key, value = validate_command(p["method"], p["path"], p["body"])
            if operation == "create":
                result = tasks.create(value)
            elif operation == "project":
                result = tasks.project(value, key)
            elif operation == "preferences":
                result = tasks.set_preferences(value)
            elif operation == "update":
                result = tasks.update(key, value)
            else:
                result = tasks.action(key, value)
            response = {"status": 200, "body": result}
        except (QuotaError, ValidationError) as error:
            db.execute("ROLLBACK TO command")
            response = {
                "status": error.status if isinstance(error, QuotaError) else 422,
                "body": {
                    "error": error.message
                    if isinstance(error, QuotaError)
                    else "Check the task fields"
                },
            }
        db.execute("RELEASE command")
        db.execute(
            "INSERT INTO relay_commands VALUES (?,?,?)",
            (command["id"], payload, json.dumps(response)),
        )
        return response
