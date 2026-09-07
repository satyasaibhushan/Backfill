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
