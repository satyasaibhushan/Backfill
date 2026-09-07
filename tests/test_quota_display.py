from backfill.quota_display import display_windows


def window(name, seconds, used=10):
    return {
        "name": name,
        "duration_seconds": seconds,
        "used": used,
        "limit": 100,
        "resets_at": "2026-09-14T00:00:00Z",
    }


def test_main_weekly_quota_does_not_inherit_model_five_hour_window():
    result = display_windows(
        "codex",
        [
            window("codex_bengalfox.primary", 18000, 0),
            window("codex_bengalfox.secondary", 604800, 0),
            window("codex.primary", 604800, 11),
        ],
    )
    assert [(w["label"], w["remaining"]) for w in result] == [("Weekly", 89)]


def test_account_durations_follow_provider_response():
    result = display_windows(
        "codex", [window("codex.primary", 7200), window("codex.secondary", 86400)]
    )
    assert [w["label"] for w in result] == ["2-hour", "Daily"]
    assert display_windows("codex", []) == []


def test_model_quota_does_not_replace_account_weekly_quota():
    result = display_windows(
        "claude",
        [
            window("primary", 18000),
            window("secondary", 604800),
            window("extra.claude-weekly-scoped-fable", 604800, 70),
        ],
    )
    assert [(w["label"], w["remaining"]) for w in result] == [
        ("5-hour", 90),
        ("Weekly", 90),
        ("Fable weekly", 30),
    ]


def test_rejection_states_do_not_call_every_failure_missing_data():
    from backfill.quota_display import quota_status

    current = {"windows": [window("primary", 18000)]}
    accepted = {
        "windows": [window("primary", 18000), window("extra.claude-weekly-scoped-fable", 604800)]
    }
    assert quota_status(True, "observation omitted a known quota window", current, accepted) == {
        "code": "missing_window",
        "label": "Fable quota unavailable",
    }
    assert quota_status(False, "Reader unavailable", None, accepted)["code"] == "unavailable"
    assert quota_status(True, "window contract changed", current, accepted)["code"] == "rejected"
    current["windows"][0]["used"] = 100
    assert quota_status(True, None, current, accepted)["code"] == "exhausted"


def test_usage_drop_before_reset_is_inconsistent_not_reset_pending():
    from backfill.quota_display import quota_status

    current = {
        "windows": [window("primary", 18000, 2)],
        "observed_at": "2026-09-13T23:00:00Z",
        "covered_through": "2026-09-13T22:58:00Z",
    }
    accepted = {"windows": [window("primary", 18000, 3)]}
    assert quota_status(True, "usage decreased before reset", current, accepted) == {
        "code": "inconsistent",
        "label": "Quota readings disagree",
    }
