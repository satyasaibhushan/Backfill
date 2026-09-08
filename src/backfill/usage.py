"""Parse native counters without storing prompts, tool arguments, or response text."""

import math


class UsageError(ValueError):
    pass


def count(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise UsageError("invalid native usage counter")
    return int(value)


class Usage:
    def __init__(self, provider: str):
        self.provider = provider
        self.tokens = 0
        self.cost = 0.0
        self.session_id = None
        self.complete = False
        self.failed = False
        self.seen = False
        self.messages: dict[str, dict] = {}
        self.streams: dict[str, str] = {}
        self.counters: dict[str, int] = {}

    def consume(self, event: dict) -> None:
        if self.provider == "codex":
            params = event.get("params") or {}
            method = event.get("method")
            if method == "thread/tokenUsage/updated":
                # Counters accumulate within this process, including resumed threads.
                # Previous processes are already charged by the governor.
                key = params["threadId"]
                total = count(params["tokenUsage"]["total"]["totalTokens"])
                if total < self.counters.get(key, 0):
                    raise UsageError("native cumulative counter decreased")
                self.counters[key] = total
                self.tokens = sum(self.counters.values())
                self.seen = True
            if method == "turn/started":
                self.complete = False
            if method == "turn/completed":
                self.failed = params.get("turn", {}).get("status") != "completed"
                self.complete = not self.failed
            return
        kind = event.get("type")
        self.session_id = event.get("session_id") or self.session_id
        if kind == "system" and event.get("subtype") in ("conversation_reset", "compact_boundary"):
            if event.get("subtype") == "conversation_reset":
                raise UsageError("conversation reset requires a new guarded run")
        if kind == "stream_event":
            frame = event.get("event") or {}
            namespace = event.get("parent_tool_use_id") or "main"
            if frame.get("type") == "message_start":
                message = frame["message"]
                self.streams[namespace] = message["id"]
                self._message(message["id"], message.get("usage", {}))
            elif frame.get("type") == "message_delta":
                message_id = self.streams.get(namespace)
                if message_id:
                    self._message(message_id, frame.get("usage", {}))
        elif kind == "assistant":
            message = event.get("message") or {}
            if message.get("id") and message.get("usage"):
                self._message(message["id"], message["usage"])
        elif kind == "result":
            groups = event.get("modelUsage")
            if isinstance(groups, dict) and groups:
                total = sum(
                    sum(
                        count(g.get(k, 0))
                        for k in (
                            "inputTokens",
                            "outputTokens",
                            "cacheReadInputTokens",
                            "cacheCreationInputTokens",
                        )
                    )
                    for g in groups.values()
                )
                self.tokens = max(self.tokens, total)
                self.seen = True
                self.complete = True
            cost = event.get("total_cost_usd")
            if cost is not None:
                if (
                    isinstance(cost, bool)
                    or not isinstance(cost, int | float)
                    or not math.isfinite(cost)
                    or cost < 0
                ):
                    raise UsageError("invalid cost counter")
                self.cost = max(self.cost, cost)
            self.failed = bool(event.get("is_error"))

    def _message(self, key: str, usage: dict) -> None:
        prior = self.messages.setdefault(key, {})
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            if name in usage:
                prior[name] = max(prior.get(name, 0), count(usage[name]))
        self.tokens = max(self.tokens, sum(sum(u.values()) for u in self.messages.values()))
        self.seen = True
