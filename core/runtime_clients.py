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
        "qwen_completion",
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
    requested_structured_output_mode: str | None = None
    schema_enforcement_requested: bool = False
    server_profile_accepted: bool = False
    local_schema_valid: bool = False
    repair_attempted: bool = False
    error: str | None = None
    primary_error: str | None = None
    repair_error: str | None = None
    validation_errors: list[str] | None = None
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
        self._ata_structured_profile: str | None = None

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
        requested_mode = self.settings.ata_structured_output_mode or "auto"
        mode = self._resolve_ata_structured_profile(requested_mode)
        if mode not in {
            "json_schema",
            "json_schema_no_strict",
            "json_object",
            "prompt_only",
            "qwen_completion",
        }:
            return StructuredLLMResponse(
                parsed=None,
                content="",
                finish_reason=None,
                structured_output_mode=mode,
                requested_structured_output_mode=requested_mode,
                schema_enforced=False,
                error=f"unsupported_structured_output_mode:{mode}",
                primary_error=f"unsupported_structured_output_mode:{mode}",
            )
        started = time.monotonic()
        try:
            if mode == "qwen_completion":
                payload = self._qwen_completion_payload(
                    stage=stage,
                    system_prompt=system_prompt,
                    input_payload=input_payload,
                    response_schema=response_schema,
                )
                endpoint = "completions"
            else:
                payload = self._structured_payload(
                    stage=stage,
                    system_prompt=system_prompt,
                    input_payload=input_payload,
                    response_schema=response_schema,
                    mode=mode,
                )
                endpoint = "chat/completions"
            request = urllib.request.Request(
                f"{self.settings.llm_base_url}/{endpoint}",
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
            if mode == "qwen_completion":
                content = str(choice.get("text") or "").strip()
                parsed = None
                reasoning_present = False
            else:
                message = choice.get("message") or {}
                content = str(message.get("content") or "").strip()
                parsed = message.get("parsed")
                if not isinstance(parsed, dict):
                    parsed = None
                reasoning_present = bool(
                    str(message.get("reasoning_content") or "").strip()
                )
            finish_reason = str(choice.get("finish_reason") or "") or None
            parsed, error, validation_errors = self._parse_structured_message(
                content=content,
                parsed=parsed,
                response_schema=response_schema,
                finish_reason=finish_reason,
                reasoning_present=reasoning_present,
            )
            if not content and parsed is None:
                content = ""
            return StructuredLLMResponse(
                parsed=parsed,
                content=content,
                finish_reason=finish_reason,
                structured_output_mode=mode,
                requested_structured_output_mode=requested_mode,
                schema_enforced=False,
                schema_enforcement_requested=mode
                in {"json_schema", "json_schema_no_strict", "qwen_completion"},
                server_profile_accepted=True,
                local_schema_valid=parsed is not None and not validation_errors and error is None,
                error=error,
                primary_error=error,
                validation_errors=validation_errors,
                latency_ms=(time.monotonic() - started) * 1000,
            )
        except Exception as exc:
            return StructuredLLMResponse(
                parsed=None,
                content="",
                finish_reason=None,
                structured_output_mode=mode,
                requested_structured_output_mode=requested_mode,
                schema_enforced=False,
                schema_enforcement_requested=mode
                in {"json_schema", "json_schema_no_strict", "qwen_completion"},
                server_profile_accepted=False,
                error=f"transport_error:{type(exc).__name__}",
                primary_error=f"transport_error:{type(exc).__name__}",
                latency_ms=(time.monotonic() - started) * 1000,
            )

    def _resolve_ata_structured_profile(self, requested_mode: str) -> str:
        requested = (requested_mode or "auto").strip()
        if requested != "auto":
            return requested
        if self._ata_structured_profile:
            return self._ata_structured_profile
        self._ata_structured_profile = self._probe_ata_structured_profile()
        return self._ata_structured_profile

    def _probe_ata_structured_profile(self) -> str:
        schema: dict[str, object] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["ok", "value"],
            "properties": {
                "ok": {"type": "boolean"},
                "value": {"type": "string"},
            },
        }
        input_payload: dict[str, object] = {
            "task": "Return ok=true and value='probe'.",
        }
        for mode in (
            "json_schema",
            "json_schema_no_strict",
            "json_object",
            "qwen_completion",
            "prompt_only",
        ):
            try:
                if mode == "qwen_completion":
                    payload = self._qwen_completion_payload(
                        stage="ata_structured_capability_probe",
                        system_prompt="Return one JSON object only. No markdown.",
                        input_payload=input_payload,
                        response_schema=schema,
                    )
                    endpoint = "completions"
                else:
                    payload = self._structured_payload(
                        stage="ata_structured_capability_probe",
                        system_prompt="Return one JSON object only. No markdown.",
                        input_payload=input_payload,
                        response_schema=schema,
                        mode=mode,
                    )
                    endpoint = "chat/completions"
                request = urllib.request.Request(
                    f"{self.settings.llm_base_url}/{endpoint}",
                    data=json.dumps(payload).encode("utf-8"),
                    headers=self._headers(),
                    method="POST",
                )
                with urllib.request.urlopen(
                    request,
                    timeout=min(20, self.settings.llm_timeout_seconds),
                ) as response:
                    body = json.loads(response.read().decode("utf-8"))
                choices = body.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if mode == "qwen_completion":
                    content = str(choice.get("text") or "").strip()
                    parsed = None
                    reasoning_present = False
                else:
                    message = choice.get("message") or {}
                    content = str(message.get("content") or "").strip()
                    parsed = message.get("parsed")
                    parsed = parsed if isinstance(parsed, dict) else None
                    reasoning_present = bool(
                        str(message.get("reasoning_content") or "").strip()
                    )
                parsed, error, validation_errors = self._parse_structured_message(
                    content=content,
                    parsed=parsed,
                    response_schema=schema,
                    finish_reason=str(choice.get("finish_reason") or "") or None,
                    reasoning_present=reasoning_present,
                )
                if parsed is not None and error is None and not validation_errors:
                    return mode
            except Exception:
                continue
        return "unsupported:auto_probe_failed"

    def _structured_payload(
        self,
        *,
        stage: str,
        system_prompt: str,
        input_payload: dict[str, object],
        response_schema: dict[str, object],
        mode: str,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.resolve_model(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        (
                            {
                                "input": input_payload,
                                "response_schema": response_schema,
                                "instruction": "Return only one JSON object that validates against response_schema.",
                            }
                            if mode == "prompt_only"
                            else input_payload
                        ),
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": self.settings.llm_temperature,
            "stream": False,
        }
        if self.settings.llm_max_tokens > 0:
            payload["max_tokens"] = self.settings.llm_max_tokens
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
        elif mode == "prompt_only":
            payload["response_format"] = {"type": "text"}
        return payload

    def _qwen_completion_payload(
        self,
        *,
        stage: str,
        system_prompt: str,
        input_payload: dict[str, object],
        response_schema: dict[str, object],
    ) -> dict[str, object]:
        user_contract = json.dumps(
            {
                "input": input_payload,
                "instruction": (
                    "Return only one JSON object matching the server-provided "
                    "JSON schema."
                ),
            },
            ensure_ascii=False,
        ).replace("<|", "\\u003c|")
        safe_system = system_prompt.replace("<|", "\\u003c|")
        prompt = (
            f"<|im_start|>system\n{safe_system}<|im_end|>\n"
            f"<|im_start|>user\n{user_contract}<|im_end|>\n"
            "<|im_start|>assistant\n<think>\n\n</think>\n\n"
        )
        payload: dict[str, object] = {
            "model": self.resolve_model(),
            "prompt": prompt,
            "temperature": self.settings.llm_temperature,
            "stream": False,
            "stop": ["<|im_end|>"],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": (
                        re.sub(r"[^a-zA-Z0-9_-]", "_", stage)[:64]
                        or "structured_response"
                    ),
                    "strict": True,
                    "schema": _strict_transport_schema(response_schema),
                },
            },
        }
        if self.settings.llm_max_tokens > 0:
            payload["max_tokens"] = self.settings.llm_max_tokens
        return payload

    @staticmethod
    def _parse_structured_message(
        *,
        content: str,
        parsed: dict[str, object] | None,
        response_schema: dict[str, object],
        finish_reason: str | None,
        reasoning_present: bool,
    ) -> tuple[dict[str, object] | None, str | None, list[str]]:
        if finish_reason == "length":
            return None, "truncated_response", []
        value = parsed
        if value is None:
            if not content.strip():
                return (
                    None,
                    "reasoning_content_without_content" if reasoning_present else "empty_content",
                    [],
                )
            try:
                value = _parse_json_object_content(content)
            except ValueError as exc:
                return None, str(exc), []
        value = _drop_optional_nulls(value, response_schema)
        errors = _schema_errors(value, response_schema, "response")
        return value, "schema_validation_failed" if errors else None, errors

    def health(self) -> dict[str, Any]:
        try:
            model = self.resolve_model()
            return {"ok": True, "model": model, "provider": self.settings.llm_provider, "base_url": self.settings.llm_base_url}
        except Exception as exc:
            return {"ok": False, "error": repr(exc), "provider": self.settings.llm_provider, "base_url": self.settings.llm_base_url}


def _parse_json_object_content(content: str) -> dict[str, object]:
    stripped = content.strip()
    if stripped.startswith("```"):
        fenced = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```",
            stripped,
            re.DOTALL | re.IGNORECASE,
        )
        if fenced is None:
            raise ValueError("invalid_fenced_json")
        stripped = fenced.group(1).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        raise ValueError("json_parse_error") from None
    if not isinstance(value, dict):
        raise ValueError("llm_response_not_object")
    return value


def _schema_errors(value: dict[str, object], schema: dict[str, object], label: str) -> list[str]:
    errors: list[str] = []
    _validate_schema_value(value, schema, label, errors)
    return errors


def _drop_optional_nulls(
    value: object,
    schema: dict[str, object],
) -> object:
    if isinstance(value, dict):
        required = {
            str(item)
            for item in schema.get("required", [])
            if isinstance(item, str)
        }
        properties = (
            schema.get("properties")
            if isinstance(schema.get("properties"), dict)
            else {}
        )
        return {
            key: _drop_optional_nulls(
                item,
                properties.get(key)
                if isinstance(properties.get(key), dict)
                else {},
            )
            for key, item in value.items()
            if item is not None or key in required
        }
    if isinstance(value, list):
        item_schema = (
            schema.get("items")
            if isinstance(schema.get("items"), dict)
            else {}
        )
        return [
            _drop_optional_nulls(item, item_schema)
            for item in value
        ]
    return value


def _validate_schema_value(
    value: object,
    schema: dict[str, object],
    path: str,
    errors: list[str],
) -> None:
    expected = schema.get("type")
    if expected and not _matches_schema_type(value, expected):
        errors.append(f"schema_type_error:{path}:{expected}")
        return
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"schema_enum_error:{path}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"schema_minimum_error:{path}")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"schema_maximum_error:{path}")
    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    errors.append(f"schema_missing_required:{path}:{key}")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            if schema.get("additionalProperties") is False:
                for key in value:
                    if key not in properties:
                        errors.append(f"schema_additional_property:{path}:{key}")
            for key, definition in properties.items():
                if key in value and isinstance(definition, dict):
                    _validate_schema_value(value[key], definition, f"{path}:{key}", errors)
    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"schema_min_items_error:{path}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_value(item, item_schema, f"{path}:{index}", errors)


def _matches_schema_type(value: object, expected: object) -> bool:
    if isinstance(expected, list):
        return any(_matches_schema_type(value, item) for item in expected)
    return {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }.get(str(expected), lambda: True)()
