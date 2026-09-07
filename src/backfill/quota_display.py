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


def quota_status(
    connected: bool, error: str | None, observed: dict | None, accepted: dict | None
) -> dict:
    from datetime import datetime

    if not connected:
        return {"code": "unavailable", "label": "Quota reading unavailable"}
    windows = {w["name"]: w for w in observed["windows"]} if observed else {}
    previous = {w["name"]: w for w in accepted["windows"]} if accepted else {}
    if error == "observation omitted a known quota window":
        missing = previous.keys() - windows.keys()
        label = "Fable quota unavailable" if FABLE_WINDOW in missing else "Quota reading incomplete"
        return {"code": "missing_window", "label": label}
    if error in {"usage decreased before a confirmed reset", "usage decreased before reset"}:
        # A reset is pending only when it has happened at the provider but the
        # conservative coverage timestamp still precedes it. Other drops disagree.
        if observed:
            observed_at = datetime.fromisoformat(observed["observed_at"])
            covered = datetime.fromisoformat(observed["covered_through"])
            for name, old in previous.items():
                current = windows.get(name)
                reset = datetime.fromisoformat(old["resets_at"])
                if (
                    current
                    and current["used"] < old["used"]
                    and covered < reset <= observed_at
                    and datetime.fromisoformat(current["resets_at"]) > reset
                ):
                    return {"code": "reset_pending", "label": "Waiting for reset confirmation"}
        return {"code": "inconsistent", "label": "Quota readings disagree"}
    if error:
        return {"code": "rejected", "label": "Quota check failed"}
    if any(w["used"] >= w["limit"] for w in windows.values()):
        return {"code": "exhausted", "label": "Quota exhausted"}
    return {"code": "ready", "label": ""}
