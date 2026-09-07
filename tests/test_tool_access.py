import json

from backfill.config import Settings
from backfill.tool_access import claude_permissions, codex_permissions


def test_read_task_does_not_inherit_development_mcp_grants(tmp_path):
    policy = tmp_path / "development.json"
    policy.write_text(
        json.dumps(
            {
                "allow": ["Bash", "mcp__development"],
                "directories": [str(tmp_path)],
                "network_access": True,
            }
        )
    )
    settings = Settings(task_access_file=policy)
    args = claude_permissions("read", settings)
    assert args[args.index("--disallowedTools") + 1] == "mcp__development"
    assert "Bash" not in args[args.index("--allowedTools") + 1]
    assert codex_permissions("read", settings)["mcp_servers"]["development"]["enabled"] is False
    assert "--disallowedTools" not in claude_permissions("edit", settings)
    assert codex_permissions("edit", settings)["sandbox_workspace_write"]["writable_roots"] == [
        str(tmp_path)
    ]
