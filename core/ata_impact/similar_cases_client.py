from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


DISCLAIMER = (
    "Исторический статус похожей заявки является справочной информацией и сам по себе "
    "не подтверждает применимость документации, сертификационную возможность или решение по новой заявке."
)


@dataclass(frozen=True)
class SimilarCasesClientConfig:
    enabled: bool
    url: str
    timeout_seconds: float
    retries: int

    @classmethod
    def from_env(cls) -> "SimilarCasesClientConfig":
        enabled = str(os.environ.get("MRO_ATA_SIMILAR_CASES_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}
        timeout = _float_env("MRO_ATA_SIMILAR_CASES_TIMEOUT_SECONDS", 4.0)
        retries = _int_env("MRO_ATA_SIMILAR_CASES_RETRIES", 1)
        return cls(
            enabled=enabled,
            url=os.environ.get("MRO_ATA_SIMILAR_CASES_URL", "http://127.0.0.1:8121/api/similar-cases/search"),
            timeout_seconds=max(0.5, min(timeout, 120.0)),
            retries=max(0, min(retries, 1)),
        )


class SimilarCasesClient:
    def __init__(self, config: SimilarCasesClientConfig | None = None) -> None:
        self.config = config or SimilarCasesClientConfig.from_env()

    def search(self, request_text: str, ata_result: dict[str, object], fields: dict[str, object] | None = None) -> tuple[dict[str, object], dict[str, object]]:
        request_id = str(ata_result.get("request_id") or "")
        trace = self._base_trace(request_id)
        if not self.config.enabled:
            trace.update({"result": "disabled", "elapsed_ms": 0, "http_status": None})
            return _disabled_result(), trace
        if not _is_allowed_internal_url(self.config.url):
            trace.update({"result": "unavailable", "elapsed_ms": 0, "http_status": None})
            return _unavailable_result("similar_cases_url_not_allowed"), trace
        body = json.dumps(
            {
                "request_id": request_id or None,
                "query": request_text,
                "context": build_similar_cases_context(request_text, ata_result, fields or {}),
                "limits": {"accepted": 5, "not_accepted": 5, "intermediate": 5},
                "retrieval_mode": "legacy_ranked_query",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.config.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        attempts = self.config.retries + 1
        started = time.monotonic()
        last_warning = "similar_cases_unavailable"
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                    raw = response.read()
                    payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    trace.update({"result": "invalid_response", "http_status": response.status})
                    return _unavailable_result("similar_cases_invalid_response"), self._finish_trace(trace, started, payload={})
                trace.update({"result": "success", "http_status": response.status})
                return payload, self._finish_trace(trace, started, payload=payload)
            except urllib.error.HTTPError as exc:
                trace["http_status"] = exc.code
                if 400 <= exc.code < 500:
                    trace["result"] = "invalid_response"
                    return _unavailable_result(f"similar_cases_http_{exc.code}"), self._finish_trace(trace, started, payload={})
                last_warning = f"similar_cases_http_{exc.code}"
                if attempt >= attempts - 1:
                    break
            except TimeoutError:
                trace["result"] = "timeout"
                last_warning = "similar_cases_timeout"
                if attempt >= attempts - 1:
                    return _unavailable_result(last_warning), self._finish_trace(trace, started, payload={})
            except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError):
                last_warning = "similar_cases_unavailable"
                if attempt >= attempts - 1:
                    break
        trace.setdefault("result", "unavailable")
        return _unavailable_result(last_warning), self._finish_trace(trace, started, payload={})

    def _base_trace(self, request_id: str) -> dict[str, object]:
        return {
            "step": "similar_cases_search",
            "request_id": request_id,
            "tool": "mro-similar-cases.search",
            "url": _safe_url(self.config.url),
        }

    @staticmethod
    def _finish_trace(trace: dict[str, object], started: float, payload: dict[str, object]) -> dict[str, object]:
        trace["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        trace["qualified_count"] = len(payload.get("accepted") or []) + len(payload.get("not_accepted") or []) + len(payload.get("intermediate") or [])
        trace["accepted_count"] = len(payload.get("accepted") or [])
        trace["not_accepted_count"] = len(payload.get("not_accepted") or [])
        trace["intermediate_count"] = len(payload.get("intermediate") or [])
        trace["threshold_version"] = payload.get("threshold_version", "")
        return trace


def attach_similar_cases(
    ata_result: dict[str, object],
    request_text: str,
    fields: dict[str, object] | None = None,
    client: SimilarCasesClient | None = None,
) -> dict[str, object]:
    similar_cases, trace = (client or SimilarCasesClient()).search(request_text, ata_result, fields or {})
    result = dict(ata_result)
    trace_items = list(result.get("agent_trace") or []) if isinstance(result.get("agent_trace"), list) else []
    trace_items.append(trace)
    result["agent_trace"] = trace_items
    answer = str(result.get("answer") or "")
    if similar_cases.get("status") != "disabled":
        answer = _append_similar_cases_answer(answer, similar_cases)
    return {"ata_impact": result, "similar_cases": similar_cases, "answer": answer}


def build_similar_cases_context(request_text: str, ata_result: dict[str, object], fields: dict[str, object]) -> dict[str, object]:
    facts = ata_result.get("engineering_facts") if isinstance(ata_result.get("engineering_facts"), dict) else {}
    identifiers = _unique(_extract_identifier_like(request_text) + _collect_strings(ata_result.get("identifiers")))
    components = _unique(_collect_by_key(facts, {"component", "components", "object", "objects", "damaged_object", "part"}))
    zones = _unique(_extract_zones(request_text) + _collect_by_key(facts, {"zone", "zones", "location", "position"}))
    defect_type = _first_nonempty(_collect_by_key(facts, {"defect", "defect_type", "damage", "damage_type"})) or _infer_defect(request_text)
    work_type = _first_nonempty(_collect_by_key(facts, {"work_type", "required_work", "task"})) or _infer_work_type(request_text)
    aircraft_type = normalize_context_text(str(fields.get("aircraft_type") or fields.get("aircraft_model") or ""))
    return {
        "aircraft_type": aircraft_type,
        "ata": _unique(_extract_user_ata(request_text)),
        "components": components,
        "defect_type": defect_type,
        "zones": zones,
        "work_type": work_type,
        "identifiers": identifiers,
    }


def _append_similar_cases_answer(answer: str, similar_cases: dict[str, object]) -> str:
    lines = [answer.rstrip(), "", "Похожие заявки, принятые в работу"]
    lines.extend(_case_lines(similar_cases.get("accepted")))
    lines.extend(["", "Похожие заявки, не принятые в работу"])
    lines.extend(_case_lines(similar_cases.get("not_accepted")))
    lines.extend(["", "Похожие заявки с промежуточным статусом"])
    lines.extend(_case_lines(similar_cases.get("intermediate")))
    warnings = [str(item) for item in similar_cases.get("warnings", []) if str(item).strip()] if isinstance(similar_cases.get("warnings"), list) else []
    if similar_cases.get("similarity_status") == "no_qualified_matches":
        lines.append("")
        lines.append("Подходящие похожие заявки не найдены.")
    elif similar_cases.get("status") == "unavailable":
        lines.append("")
        lines.append("Поиск похожих заявок недоступен; аналоги не подставлены.")
    lines.extend(["", DISCLAIMER])
    for warning in warnings[:3]:
        lines.append(f"Предупреждение: {warning}")
    return "\n".join(lines).strip()


def _case_lines(value: object) -> list[str]:
    cases = value if isinstance(value, list) else []
    if not cases:
        return ["- Подходящие заявки не найдены."]
    lines = []
    for item in cases[:5]:
        if not isinstance(item, dict):
            continue
        reasons = [str(reason) for reason in item.get("reasons", []) if str(reason).strip()] if isinstance(item.get("reasons"), list) else []
        description = normalize_context_text(str(item.get("request_description") or ""))[:180]
        lines.append(
            f"- {item.get('case_id', '')}: {item.get('status_normalized', '')}; "
            f"{item.get('aircraft_type', '')}; сходство {item.get('similarity_confidence', 'low')}; "
            f"{description}; причины: {'; '.join(reasons[:3]) or item.get('similarity_reason_class', '')}"
        )
    return lines or ["- Подходящие заявки не найдены."]


def _disabled_result() -> dict[str, object]:
    return {
        "status": "disabled",
        "similarity_status": "unavailable",
        "threshold_version": "",
        "accepted": [],
        "not_accepted": [],
        "intermediate": [],
        "coverage": {"accepted_available": 0, "not_accepted_available": 0, "intermediate_available": 0, "unknown_status_excluded": 0},
        "warnings": ["similar_cases_integration_disabled"],
    }


def _unavailable_result(warning: str) -> dict[str, object]:
    return {
        "status": "unavailable",
        "similarity_status": "unavailable",
        "threshold_version": "",
        "accepted": [],
        "not_accepted": [],
        "intermediate": [],
        "coverage": {"accepted_available": 0, "not_accepted_available": 0, "intermediate_available": 0, "unknown_status_excluded": 0},
        "warnings": [warning],
    }


def _safe_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _is_allowed_internal_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if host.endswith(".trafic.rujv"):
        return True
    return bool(re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host))


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def normalize_context_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = normalize_context_text(value)
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _collect_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_collect_strings(item))
        return result
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_collect_strings(item))
        return result
    return []


def _collect_by_key(value: object, keys: set[str]) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            if str(key).lower() in keys:
                result.extend(_collect_strings(item))
            else:
                result.extend(_collect_by_key(item, keys))
        return result
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_collect_by_key(item, keys))
        return result
    return []


def _first_nonempty(values: list[str]) -> str:
    return next((normalize_context_text(value) for value in values if normalize_context_text(value)), "")


def _extract_identifier_like(text: str) -> list[str]:
    patterns = [
        r"\b(?:FR|FRAME|STGR|STRINGER|RIB)\s*[.#№-]?\s*\d{1,3}[A-ZА-Я]?\b",
        r"\b(?:P/N|PN|MSN)\s*[:#-]?\s*[A-Z0-9_.-]{3,}\b",
    ]
    result: list[str] = []
    for pattern in patterns:
        result.extend(re.findall(pattern, text, flags=re.IGNORECASE))
    return result


def _extract_user_ata(text: str) -> list[str]:
    return re.findall(r"\bATA\s*[-:]?\s*\d{2}(?:-\d{2})?\b", text, flags=re.IGNORECASE)


def _extract_zones(text: str) -> list[str]:
    return re.findall(r"\b(?:FR|FRAME|RIB|STGR|STRINGER|ZONE)\s*[.#№-]?\s*\d{1,3}[A-ZА-Я]?\b", text, flags=re.IGNORECASE)


def _infer_defect(text: str) -> str:
    lookup = text.lower()
    if any(token in lookup for token in ("crack", "трещ")):
        return "crack"
    if any(token in lookup for token in ("corrosion", "корроз")):
        return "corrosion"
    if any(token in lookup for token in ("dent", "вмят")):
        return "dent"
    if any(token in lookup for token in ("scratch", "царап")):
        return "scratch"
    return ""


def _infer_work_type(text: str) -> str:
    lookup = text.lower()
    if any(token in lookup for token in ("repair", "ремонт")):
        return "repair"
    if any(token in lookup for token in ("inspect", "inspection", "осмотр", "инспек")):
        return "inspection"
    if any(token in lookup for token in ("replace", "замен")):
        return "replacement"
    return ""
