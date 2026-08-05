from __future__ import annotations

from typing import Any

from ._pydantic import BaseModel, Field


class ProgressEvent(BaseModel):
    stage: str
    message: str
    status: str
    service: str | None = None
    request_id: str
    safe_url: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


def event_to_reasoning(event: ProgressEvent) -> str:
    lines = [event.message]
    if event.service:
        lines.append(f"Сервис: {event.service}.")
    if event.safe_url:
        lines.append(f"Endpoint: {event.safe_url}")
    details = _safe_details(event.details)
    if details:
        for key, value in details.items():
            lines.append(f"- {key}: {value}")
    return "\n".join(lines).strip() + "\n"


def _safe_details(details: dict[str, Any]) -> dict[str, Any]:
    blocked = ("key", "token", "authorization", "password", "secret", "credential")
    return {
        key: value
        for key, value in details.items()
        if not any(part in key.lower() for part in blocked)
    }

