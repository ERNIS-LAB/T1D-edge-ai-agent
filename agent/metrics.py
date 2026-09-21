from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any


@dataclass
class LLMRunMetrics:
    latency_seconds: float
    time_to_first_token_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None

    @property
    def tokens_per_second(self) -> float | None:
        if self.output_tokens is None:
            return None
        base = self.latency_seconds
        if self.time_to_first_token_seconds is not None:
            base = max(1e-9, self.latency_seconds - self.time_to_first_token_seconds)
        if base <= 0:
            return None
        return self.output_tokens / base


def now() -> float:
    return perf_counter()


def usage_from_message(message: Any) -> tuple[int | None, int | None, int | None]:
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict):
        input_tokens = _to_int(usage.get("input_tokens"))
        output_tokens = _to_int(usage.get("output_tokens"))
        total_tokens = _to_int(usage.get("total_tokens"))
        return input_tokens, output_tokens, total_tokens

    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        token_usage = response_metadata.get("token_usage")
        if isinstance(token_usage, dict):
            input_tokens = _to_int(token_usage.get("prompt_tokens"))
            output_tokens = _to_int(token_usage.get("completion_tokens"))
            total_tokens = _to_int(token_usage.get("total_tokens"))
            return input_tokens, output_tokens, total_tokens

    return None, None, None


def usage_from_chunk(chunk: Any) -> tuple[int | None, int | None, int | None]:
    usage = getattr(chunk, "usage_metadata", None)
    if isinstance(usage, dict):
        return (
            _to_int(usage.get("input_tokens")),
            _to_int(usage.get("output_tokens")),
            _to_int(usage.get("total_tokens")),
        )

    response_metadata = getattr(chunk, "response_metadata", None)
    if isinstance(response_metadata, dict):
        token_usage = response_metadata.get("token_usage")
        if isinstance(token_usage, dict):
            return (
                _to_int(token_usage.get("prompt_tokens")),
                _to_int(token_usage.get("completion_tokens")),
                _to_int(token_usage.get("total_tokens")),
            )

    return None, None, None


def format_metrics(metrics: LLMRunMetrics) -> str:
    ttft = "n/a" if metrics.time_to_first_token_seconds is None else f"{metrics.time_to_first_token_seconds:.2f}s"
    input_tokens = "n/a" if metrics.input_tokens is None else str(metrics.input_tokens)
    output_tokens = "n/a" if metrics.output_tokens is None else str(metrics.output_tokens)
    total_tokens = "n/a" if metrics.total_tokens is None else str(metrics.total_tokens)
    tps = metrics.tokens_per_second
    tps_text = "n/a" if tps is None else f"{tps:.1f} tok/s"
    return (
        f"latency={metrics.latency_seconds:.2f}s, "
        f"ttft={ttft}, "
        f"tokens(in/out/total)={input_tokens}/{output_tokens}/{total_tokens}, "
        f"throughput={tps_text}"
    )


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None
