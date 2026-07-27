from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    reranker_enabled: bool = _bool_env("MRO_KB_RERANKER_ENABLED", True)
    reranker_model: str = os.getenv("MRO_KB_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
    reranker_url: str = os.getenv("MRO_KB_RERANKER_URL", "").strip()
    reranker_batch_size: int = int(os.getenv("MRO_KB_RERANKER_BATCH_SIZE", "8"))
    llm_enabled: bool = _bool_env("MRO_KB_LLM_ENABLED", True)
    llm_provider: str = os.getenv("MRO_KB_LLM_PROVIDER", "openai").strip()
    llm_model: str = os.getenv("MRO_KB_LLM_MODEL", "").strip()
    llm_base_url: str = os.getenv("MRO_KB_LLM_BASE_URL", "http://10.100.112.71:1234/v1").strip().rstrip("/")
    llm_api_key: str = os.getenv("MRO_KB_LLM_API_KEY", "local").strip()
    llm_temperature: float = float(os.getenv("MRO_KB_LLM_TEMPERATURE", "0"))
    llm_max_tokens: int = int(os.getenv("MRO_KB_LLM_MAX_TOKENS", "1200"))
    ata_structured_output_mode: str = os.getenv(
        "MRO_KB_ATA_STRUCTURED_OUTPUT_MODE",
        "json_schema",
    ).strip()
    # The intake agent must fail closed to the ontology result instead of leaving
    # an OpenWebUI stream in a perpetual "thinking" state.
    llm_timeout_seconds: float = float(os.getenv("MRO_KB_LLM_TIMEOUT_SECONDS", "30"))


@dataclass(frozen=True, slots=True)
class StructuredLLMResponse:
    parsed: dict[str, object] | None
    content: str
    finish_reason: str | None
    structured_output_mode: str
    schema_enforced: bool
    repair_attempted: bool = False
    error: str | None = None
    latency_ms: float = 0.0


def _strict_transport_schema(schema: dict[str, object]) -> dict[str, object]:
    """Adapt the portable business schema to strict structured-output rules."""

    result = deepcopy(schema)

    def visit(node: object) -> None:
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            originally_required = {
                str(item) for item in node.get("required", [])
            }
            for name, definition in properties.items():
                visit(definition)
                if name not in originally_required and isinstance(definition, dict):
                    value_type = definition.get("type")
                    if isinstance(value_type, str):
                        definition["type"] = [value_type, "null"]
                    elif isinstance(value_type, list) and "null" not in value_type:
                        definition["type"] = [*value_type, "null"]
                    enum = definition.get("enum")
                    if isinstance(enum, list) and None not in enum:
                        definition["enum"] = [*enum, None]
            node["required"] = list(properties)
        items = node.get("items")
        visit(items)

    visit(result)
    return result


class ExternalReranker:
    def __init__(self, url: str, batch_size: int = 8, timeout: int = 120) -> None:
        self.url = url.rstrip("/")
        self.batch_size = max(1, int(batch_size))
        self.timeout = timeout

    def _request_scores(self, pairs: list[list[str]], normalize: bool = True) -> list[float]:
        payload = json.dumps({"pairs": pairs, "normalize": normalize}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}/rerank",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        scores = body.get("scores")
        if not isinstance(scores, list):
            raise RuntimeError("Reranker response does not contain scores list")
        return [float(item) for item in scores]

    def _compute_score_adaptive(self, pairs: list[list[str]], normalize: bool = True) -> list[float]:
        try:
            return self._request_scores(pairs, normalize=normalize)
        except Exception:
            if len(pairs) <= 1:
                raise
            midpoint = max(1, len(pairs) // 2)
            return self._compute_score_adaptive(pairs[:midpoint], normalize=normalize) + self._compute_score_adaptive(
                pairs[midpoint:], normalize=normalize
            )

    def compute_score(self, pairs: list[list[str]], normalize: bool = True) -> list[float]:
        scores: list[float] = []
        for start in range(0, len(pairs), self.batch_size):
            batch = pairs[start : start + self.batch_size]
            scores.extend(self._compute_score_adaptive(batch, normalize=normalize))
        return scores

    def health(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return {"ok": True, "payload": payload}
        except Exception as exc:
            return {"ok": False, "error": repr(exc)}


class OpenAICompatibleLLM:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self._resolved_model: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        return headers

    def resolve_model(self) -> str:
        if self.settings.llm_model:
            return self.settings.llm_model
        if self._resolved_model:
            return self._resolved_model
        request = urllib.request.Request(
            f"{self.settings.llm_base_url}/models",
            headers=self._headers(),
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=min(20, self.settings.llm_timeout_seconds)) as response:
            payload = json.loads(response.read().decode("utf-8"))
        items = payload.get("data") or []
        if not items:
            raise RuntimeError("LLM /models response is empty")
        model_id = str(items[0].get("id") or "").strip()
        if not model_id:
            raise RuntimeError("LLM model id is empty")
        self._resolved_model = model_id
        return model_id

    def chat(self, system_prompt: str, user_prompt: str, allow_reasoning_fallback: bool = False) -> str:
        result = self.chat_response(system_prompt, user_prompt, allow_reasoning_fallback)
        return str(result.get("content") or "")

    def chat_response(
        self,
        system_prompt: str,
        user_prompt: str,
        allow_reasoning_fallback: bool = False,
    ) -> dict[str, object]:
        payload = {
            "model": self.resolve_model(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.settings.llm_temperature,
            "stream": False,
        }
        if self.settings.llm_max_tokens > 0:
            payload["max_tokens"] = self.settings.llm_max_tokens
        request = urllib.request.Request(
            f"{self.settings.llm_base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.settings.llm_timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("LLM response has no choices")
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "").strip()
        if not content and allow_reasoning_fallback:
            content = str(message.get("reasoning_content") or "").strip()
        result: dict[str, object] = {
            "content": content,
            "finish_reason": choices[0].get("finish_reason"),
        }
        if isinstance(message.get("parsed"), dict):
            result["parsed"] = message["parsed"]
        return result

    def structured_chat(
        self,
        *,
        stage: str,
        system_prompt: str,
        input_payload: dict[str, object],
        response_schema: dict[str, object],
    ) -> StructuredLLMResponse:
        mode = self.settings.ata_structured_output_mode
        if mode not in {
            "json_schema",
            "json_schema_no_strict",
            "json_object",
            "prompt_only",
        }:
            return StructuredLLMResponse(
                parsed=None,
                content="",
                finish_reason=None,
                structured_output_mode=mode,
                schema_enforced=False,
                error=f"unsupported_structured_output_mode:{mode}",
            )
        schema_enforced = mode == "json_schema"
        started = time.monotonic()
        try:
            payload: dict[str, object] = {
                "model": self.resolve_model(),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(input_payload, ensure_ascii=False),
                    },
                ],
                "temperature": self.settings.llm_temperature,
                "stream": False,
            }
            schema_name = (
                re.sub(r"[^a-zA-Z0-9_-]", "_", stage)[:64]
                or "structured_response"
            )
            if mode in {"json_schema", "json_schema_no_strict"}:
                json_schema: dict[str, object] = {
                    "name": schema_name,
                    "schema": (
                        _strict_transport_schema(response_schema)
                        if mode == "json_schema"
                        else response_schema
                    ),
                }
                if mode == "json_schema":
                    json_schema["strict"] = True
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": json_schema,
                }
            elif mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
            if self.settings.llm_max_tokens > 0:
                payload["max_tokens"] = self.settings.llm_max_tokens
            request = urllib.request.Request(
                f"{self.settings.llm_base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers=self._headers(),
                method="POST",
            )
            with urllib.request.urlopen(
                request,
                timeout=self.settings.llm_timeout_seconds,
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
            choices = body.get("choices") or []
            if not choices:
                raise RuntimeError("LLM response has no choices")
            choice = choices[0]
            message = choice.get("message") or {}
            content = str(message.get("content") or "").strip()
            parsed = message.get("parsed")
            if not isinstance(parsed, dict):
                parsed = None
            error = None
            if not content and parsed is None:
                error = (
                    "reasoning_content_without_content"
                    if str(message.get("reasoning_content") or "").strip()
                    else "empty_content"
                )
            return StructuredLLMResponse(
                parsed=parsed,
                content=content,
                finish_reason=str(choice.get("finish_reason") or "") or None,
                structured_output_mode=mode,
                schema_enforced=schema_enforced,
                error=error,
                latency_ms=(time.monotonic() - started) * 1000,
            )
        except Exception as exc:
            return StructuredLLMResponse(
                parsed=None,
                content="",
                finish_reason=None,
                structured_output_mode=mode,
                schema_enforced=schema_enforced,
                error=f"transport_error:{type(exc).__name__}",
                latency_ms=(time.monotonic() - started) * 1000,
            )

    def health(self) -> dict[str, Any]:
        try:
            model = self.resolve_model()
            return {"ok": True, "model": model, "provider": self.settings.llm_provider, "base_url": self.settings.llm_base_url}
        except Exception as exc:
            return {"ok": False, "error": repr(exc), "provider": self.settings.llm_provider, "base_url": self.settings.llm_base_url}
