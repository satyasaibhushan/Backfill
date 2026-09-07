import json
from pathlib import Path

from pydantic import BaseModel, Field

from backfill.config import Settings


class DevelopmentAccess(BaseModel):
    directories: list[str] = Field(default_factory=list)
    allow: list[str] = Field(default_factory=list)
    network_access: bool = False


def development_access(settings: Settings) -> DevelopmentAccess:
    path = settings.task_access_file.expanduser()
    if not path.exists():
        return DevelopmentAccess()
    return DevelopmentAccess.model_validate(json.loads(path.read_text()))


def task_directories(settings: Settings) -> list[str]:
    return [
        str(p)
        for value in development_access(settings).directories
        if (p := Path(value).expanduser()).is_dir()
    ]


def claude_permissions(access: str, settings: Settings) -> list[str]:
    tools = "Read,Glob,Grep,WebFetch,WebSearch"
    if access == "edit":
        tools += ",Write,Edit,Bash"
    granted = tools.split(",") if access == "read" else development_access(settings).allow
    arguments = [
        "--tools",
        tools,
        "--allowedTools",
        ",".join(granted),
        "--permission-mode",
        "acceptEdits" if access == "edit" else "dontAsk",
    ]
    if access == "read":
        denied = [v for v in development_access(settings).allow if v.startswith("mcp__")]
        if denied:
            arguments += ["--disallowedTools", ",".join(denied)]
    for directory in task_directories(settings):
        arguments += ["--add-dir", directory]
    return arguments


def codex_permissions(access: str, settings: Settings) -> dict:
    if access != "edit":
        return {
            "mcp_servers": {
                grant.removeprefix("mcp__"): {"enabled": False}
                for grant in development_access(settings).allow
                if grant.startswith("mcp__") and "__" not in grant.removeprefix("mcp__")
            }
        }
    return {
        "sandbox_workspace_write": {
            "writable_roots": task_directories(settings),
            "network_access": development_access(settings).network_access,
        }
    }
