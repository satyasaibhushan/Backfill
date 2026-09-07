import pytest

from backfill.usage import Usage, UsageError


def native(total, thread="one"):
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": thread,
            "turnId": "turn",
            "tokenUsage": {"total": {"totalTokens": total}, "last": {"totalTokens": 25}},
        },
    }


def test_cumulative_native_counters_resume_and_duplicates():
    usage = Usage("codex", {"one": 100})
    for n in (120, 120, 200):
        usage.consume(native(n))
    usage.consume(native(30, "child"))
    assert usage.tokens == 130
    with pytest.raises(UsageError):
        usage.consume(native(190))


def test_claude_message_id_deduplication_and_whole_tree_result():
    usage = Usage("claude")
    message = {
        "type": "assistant",
        "message": {
            "id": "a",
            "usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 5},
        },
    }
    usage.consume(message)
    usage.consume(message)
    assert usage.tokens == 16
    result = {
        "type": "result",
        "session_id": "session",
        "modelUsage": {
            "large": {
                "inputTokens": 40,
                "outputTokens": 20,
                "cacheReadInputTokens": 30,
                "cacheCreationInputTokens": 10,
            }
        },
        "total_cost_usd": 0.12,
    }
    usage.consume(result)
    usage.consume(result)
    assert usage.tokens == 100 and usage.cost == 0.12 and usage.complete


def test_partial_output_accumulates_before_result():
    usage = Usage("claude")
    usage.consume(
        {
            "type": "stream_event",
            "event": {
                "type": "message_start",
                "message": {"id": "a", "usage": {"input_tokens": 40}},
            },
        }
    )
    usage.consume(
        {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 15}}}
    )
    assert usage.tokens == 55
    assert not usage.complete


def test_reset_or_invalid_counters_fail_closed():
    usage = Usage("claude")
    with pytest.raises(UsageError):
        usage.consume({"type": "system", "subtype": "conversation_reset"})
    with pytest.raises(UsageError):
        usage.consume({"type": "result", "total_cost_usd": float("nan")})
