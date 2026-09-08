"""Parse native counters without storing prompts, tool arguments, or response text."""

import math

from backfill.schemas import InferenceUsage


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
        self.raw_counters: dict[str, int] = {}
        self.counter_offsets: dict[str, int] = {}
        self.details: dict[str, InferenceUsage] = {}
        self.message_models: dict[str, str | None] = {}

    @property
    def inference(self) -> list[dict]:
        groups: dict[tuple[str | None, str], InferenceUsage] = {}
        for value in self.details.values():
            group = groups.setdefault(
                (value.model, value.mode), InferenceUsage(model=value.model, mode=value.mode)
            )
            group.input_tokens += value.input_tokens
            group.output_tokens += value.output_tokens
            group.cache_read_tokens += value.cache_read_tokens
            group.cache_write_tokens += value.cache_write_tokens
        return [value.model_dump() for value in groups.values()]

    def consume(self, event: dict) -> None:
        if self.provider == "codex":
            params = event.get("params") or {}
            method = event.get("method")
            if method == "thread/tokenUsage/updated":
                # Counters accumulate within this process, including resumed threads.
                # Previous processes are already charged by the governor.
                key = params["threadId"]
                total = count(params["tokenUsage"]["total"]["totalTokens"])
                if total < self.raw_counters.get(key, 0):
                    # A native reset must not erase already metered work or kill compaction.
                    self.counter_offsets[key] = self.counters[key]
                    if key in self.details:
                        self.details[f"{key}:epoch:{self.counters[key]}"] = self.details.pop(key)
                self.raw_counters[key] = total
                self.counters[key] = self.counter_offsets.get(key, 0) + total
                self.tokens = sum(self.counters.values())
                self.seen = True
                raw = params["tokenUsage"]["total"]
                # Total-only events cannot be priced: missing categories are not zero.
                if all(k in raw for k in ("inputTokens", "outputTokens", "cachedInputTokens")):
                    inputs = count(raw["inputTokens"])
                    cached = count(raw["cachedInputTokens"])
                    if cached > inputs:
                        raise UsageError("cached input exceeds total input")
                    self.details[key] = InferenceUsage(
                        model=params.get("model"),
                        mode=params.get("serviceTier") or "unknown",
                        input_tokens=inputs - cached,
                        cache_read_tokens=cached,
                        output_tokens=count(raw["outputTokens"]),
                    )
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
                self.message_models[message["id"]] = message.get("model")
                self._message(message["id"], message.get("usage", {}))
            elif frame.get("type") == "message_delta":
                message_id = self.streams.get(namespace)
                if message_id:
                    self._message(message_id, frame.get("usage", {}))
        elif kind == "assistant":
            message = event.get("message") or {}
            if message.get("id") and message.get("usage"):
                self.message_models[message["id"]] = message.get("model")
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
                self.details = {
                    model: InferenceUsage(
                        model=model,
                        mode=event.get("service_tier") or "unknown",
                        input_tokens=count(g.get("inputTokens", 0)),
                        output_tokens=count(g.get("outputTokens", 0)),
                        cache_read_tokens=count(g.get("cacheReadInputTokens", 0)),
                        cache_write_tokens=count(g.get("cacheCreationInputTokens", 0)),
                    )
                    for model, g in groups.items()
                }
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
        self.details[key] = InferenceUsage(
            model=self.message_models.get(key),
            input_tokens=prior.get("input_tokens", 0),
            output_tokens=prior.get("output_tokens", 0),
            cache_read_tokens=prior.get("cache_read_input_tokens", 0),
            cache_write_tokens=prior.get("cache_creation_input_tokens", 0),
        )
        self.tokens = max(self.tokens, sum(sum(u.values()) for u in self.messages.values()))
        self.seen = True
