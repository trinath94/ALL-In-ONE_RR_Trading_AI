"""AI provider adapters for the gateway (RR Trading One Phase 9).

Every adapter does one thing: send a system prompt and a JSON user message, and return the JSON object the model
produced plus usage. It never decides anything: schema validation, boundaries, retries, fallback and accounting are
the gateway's job (`app.ai.gateway`), identical for every provider.

* ``anthropic``: the original Claude call (`ClaudeAgent._call`: structured JSON-schema output, prompt caching).
* ``openai_compatible``: any `/chat/completions` API with JSON mode: Qwen (DashScope compatible mode), Ollama and
  other local servers (`http://host:11434/v1`), and compatible future providers. The schema is stated in the system
  prompt and the output is validated afterwards, because JSON mode alone does not guarantee the schema.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx

KINDS = ("anthropic", "openai_compatible")


class ProviderError(Exception):
    """kind: rate_limited | connection | api | refusal | truncated | invalid_output | error."""

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


class AnthropicProvider:
    kind = "anthropic"

    def __init__(self, api_key: str | None, timeout_s: float, client=None):
        import anthropic
        self.client = client or anthropic.Anthropic(api_key=api_key or None, max_retries=0, timeout=timeout_s)

    def complete_json(self, system: str, user: dict, schema: dict, model: str, max_tokens: int) -> Completion:
        from app.agents.claude_agent import AgentError, ClaudeAgent
        agent = ClaudeAgent(client=self.client)
        agent.model = model
        try:
            data, meta = agent._call(system, user, schema, max_tokens=max_tokens)
        except AgentError as exc:
            raise ProviderError(str(exc), getattr(exc, "kind", "error"), getattr(exc, "retry_after", None)) from exc
        return Completion(data, meta["model"], meta["input_tokens"], meta["output_tokens"], meta["latency_ms"])


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


class OpenAICompatibleProvider:
    kind = "openai_compatible"

    def __init__(self, base_url: str, api_key: str | None, timeout_s: float, transport: httpx.BaseTransport | None = None):
        if not base_url:
            raise ProviderError("base URL is required for an OpenAI-compatible provider", "api")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.http = httpx.Client(timeout=timeout_s, headers=headers, transport=transport)

    def complete_json(self, system: str, user: dict, schema: dict, model: str, max_tokens: int) -> Completion:
        sys = (system + "\n\nRespond with ONLY one JSON object that conforms to this JSON Schema. No prose, no code "
               "fences, no fields that are not in the schema:\n" + json.dumps(schema, separators=(",", ":")))
        body = {"model": model, "temperature": 0, "max_tokens": min(max_tokens, 8192),
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": sys},
                             {"role": "user", "content": json.dumps(user, default=str, separators=(",", ":"))}]}
        t0 = time.perf_counter()
        try:
            r = self.http.post(self.url, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"timeout: {exc}", "connection") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"connection error: {exc}", "connection") from exc
        latency = (time.perf_counter() - t0) * 1000
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            raise ProviderError("rate limited (HTTP 429)", "rate_limited", float(ra) if ra and ra.isdigit() else None)
        if r.status_code >= 500:
            raise ProviderError(f"provider error HTTP {r.status_code}", "connection")
        if r.status_code >= 400:
            raise ProviderError(f"request rejected HTTP {r.status_code}: {r.text[:200]}", "api")
        try:
            payload = r.json()
            choice = payload["choices"][0]
            text = choice["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected response shape: {exc}", "invalid_output") from exc
        if choice.get("finish_reason") == "length":
            raise ProviderError("response truncated (max_tokens)", "truncated")
        try:
            data = json.loads(_strip_fences(text))
        except ValueError as exc:
            raise ProviderError(f"response is not JSON: {exc}", "invalid_output") from exc
        if not isinstance(data, dict):
            raise ProviderError("response is not a JSON object", "invalid_output")
        usage = payload.get("usage") or {}
        return Completion(data, str(payload.get("model") or model), int(usage.get("prompt_tokens") or 0),
                          int(usage.get("completion_tokens") or 0), latency)
