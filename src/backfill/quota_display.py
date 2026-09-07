"""Keep account quota separate from model-specific quota in the dashboard."""

FABLE_WINDOW = "extra.claude-weekly-scoped-fable"


def account_window(provider: str, name: str) -> bool:
    if provider == "codex":
        return "." not in name or name.split(".", 1)[0] in {"codex", "default"}
    return name != FABLE_WINDOW


def duration_label(seconds: float) -> str:
    if seconds == 604800:
        return "Weekly"
    if seconds == 86400:
        return "Daily"
    if seconds % 3600 == 0:
        return f"{seconds / 3600:g}-hour"
    return f"{seconds / 60:g}-minute"


def display_windows(provider: str, windows: list[dict]) -> list[dict]:
    groups = {}
    for window in windows:
        if window["name"] == FABLE_WINDOW and provider == "claude":
            label = "Fable weekly"
        elif account_window(provider, window["name"]):
            label = duration_label(window["duration_seconds"])
        else:
            continue
        remaining = round(max(0, 100 * (window["limit"] - window["used"]) / window["limit"]), 1)
        if label not in groups or remaining < groups[label]["remaining"]:
            groups[label] = {
                "label": label,
                "remaining": remaining,
                "resets_at": window["resets_at"],
            }
    if provider == "claude" and "Fable weekly" not in groups:
        groups["Fable weekly"] = {"label": "Fable weekly", "remaining": None, "resets_at": None}
    return list(groups.values())
