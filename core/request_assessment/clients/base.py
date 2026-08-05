from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from core.request_assessment.models import ExternalCallTrace


@dataclass(frozen=True)
class HttpClientConfig:
    url: str
    timeout_seconds: float = 10.0
    retries: int = 1
    enabled: bool = True


class ExternalServiceError(RuntimeError):
    def __init__(self, message: str, trace: ExternalCallTrace) -> None:
        super().__init__(message)
        self.trace = trace


class HttpJsonClient:
    service_name = "external"

    def __init__(self, config: HttpClientConfig) -> None:
        self.config = config

    @property
    def safe_url(self) -> str:
        return safe_url(self.config.url)

    def post_json(self, payload: dict[str, Any]) -> tuple[dict[str, Any], ExternalCallTrace]:
        if not self.config.enabled:
            trace = self._trace("disabled", attempts=0, warning="client_disabled")
            raise ExternalServiceError("client disabled", trace)
        if not is_allowed_internal_url(self.config.url):
            trace = self._trace("blocked", attempts=0, warning="url_not_allowed")
            raise ExternalServiceError("url not allowed", trace)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.config.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        attempts = max(0, min(self.config.retries, 1)) + 1
        last_warning = "unavailable"
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                    raw = response.read()
                    payload_out = json.loads(raw.decode("utf-8"))
                if not isinstance(payload_out, dict):
                    trace = self._trace("invalid_response", response.status, started, attempt + 1, "invalid_json_shape")
                    raise ExternalServiceError("invalid response", trace)
                return payload_out, self._trace("success", response.status, started, attempt + 1)
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500:
                    trace = self._trace("http_error", exc.code, started, attempt + 1, f"http_{exc.code}")
                    raise ExternalServiceError(f"http {exc.code}", trace)
                last_warning = f"http_{exc.code}"
                if attempt >= attempts - 1:
                    trace = self._trace("http_error", exc.code, started, attempt + 1, last_warning)
                    raise ExternalServiceError(last_warning, trace)
            except TimeoutError:
                last_warning = "timeout"
                if attempt >= attempts - 1:
                    trace = self._trace("timeout", None, started, attempt + 1, last_warning)
                    raise ExternalServiceError(last_warning, trace)
            except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                last_warning = exc.__class__.__name__
                if attempt >= attempts - 1:
                    trace = self._trace("unavailable", None, started, attempt + 1, last_warning)
                    raise ExternalServiceError(last_warning, trace)
        trace = self._trace("unavailable", None, started, attempts, last_warning)
        raise ExternalServiceError(last_warning, trace)

    def _trace(
        self,
        status: str,
        http_status: int | None = None,
        started: float | None = None,
        attempts: int = 0,
        warning: str | None = None,
    ) -> ExternalCallTrace:
        elapsed = int((time.monotonic() - started) * 1000) if started else 0
        return ExternalCallTrace(
            service=self.service_name,
            safe_url=self.safe_url,
            status=status,
            http_status=http_status,
            elapsed_ms=elapsed,
            attempts=attempts,
            warning=warning,
        )


def safe_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def is_allowed_internal_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if host.endswith(".trafic.rujv"):
        return True
    return bool(re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host))

