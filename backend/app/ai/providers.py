"""AI provider adapters for the gateway (RR Trading One Phase 9).

Every adapter does one thing: send a system prompt and a JSON user message, and return the JSON object the model
produced plus usage. It never decides anything: schema validation, boundaries, retries, fallback and accounting are
the gateway's job (`app.ai.gateway`), identical for every provider.

* ``anthropic``: the Claude call through the official Anthropic SDK (`ClaudeAgent._call`: structured JSON-schema
  output, prompt caching). It is the only provider kind.
"""
from __future__ import annotations

from dataclasses import dataclass

KINDS = ("anthropic",)

# The only provider: the Anthropic API (owner's decision, 2026-11-25: Qwen, OpenAI, Ollama and other OpenAI-compatible
# connectors were removed). The models offered come from the API itself (``list_models``), never a hard-coded list.
PRESETS = [
    {"id": "anthropic", "label": "Anthropic Claude (API)", "kind": "anthropic", "base_url": None, "key_required": True,
     "note": "Needs an API key from the Claude Console (platform.claude.com → API keys) with API credits. A Claude.ai "
             "Pro or Max subscription does not include API access."},
]


class ProviderError(Exception):
    """kind: rate_limited | local_limit (Phase 14: our own per-minute limit) | connection | api | refusal | truncated |
    invalid_output | error."""

    def __init__(self, message: str, kind: str = "error", retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in ("rate_limited", "connection")


@dataclass
class Completion:
    data: dict
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cache_read_tokens: int = 0       # prompt cache (Anthropic); 0 where the provider does not report it
    cache_write_tokens: int = 0


class AnthropicProvider:
    kind = "anthropic"

    def __init__(self, api_key: str | None, timeout_s: float, client=None):
        import anthropic
        self.client = client or anthropic.Anthropic(api_key=api_key or None, max_retries=0, timeout=timeout_s)

    def list_models(self) -> list[str]:
        try:
            return sorted(m.id for m in self.client.models.list(limit=100).data)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as a provider error
            raise ProviderError(f"model list unavailable: {type(exc).__name__}: {str(exc)[:160]}", "api") from exc

    def complete_json(self, system: str, user: dict, schema: dict, model: str, max_tokens: int) -> Completion:
        from app.agents.claude_agent import AgentError, ClaudeAgent
        agent = ClaudeAgent(client=self.client)
        agent.model = model
        try:
            data, meta = agent._call(system, user, schema, max_tokens=max_tokens)
        except AgentError as exc:
            raise ProviderError(str(exc), getattr(exc, "kind", "error"), getattr(exc, "retry_after", None)) from exc
        return Completion(data, meta["model"], meta["input_tokens"], meta["output_tokens"], meta["latency_ms"],
                          meta.get("cache_read_tokens", 0), meta.get("cache_write_tokens", 0))
