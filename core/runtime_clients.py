from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
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
        with urllib.request.urlopen(request, timeout=20) as response:
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
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("LLM response has no choices")
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "").strip()
        if content or not allow_reasoning_fallback:
            return content
        return str(message.get("reasoning_content") or "").strip()

    def health(self) -> dict[str, Any]:
        try:
            model = self.resolve_model()
            return {"ok": True, "model": model, "provider": self.settings.llm_provider, "base_url": self.settings.llm_base_url}
        except Exception as exc:
            return {"ok": False, "error": repr(exc), "provider": self.settings.llm_provider, "base_url": self.settings.llm_base_url}
