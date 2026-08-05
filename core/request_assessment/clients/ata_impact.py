from __future__ import annotations

from typing import Any

from .base import HttpClientConfig, HttpJsonClient


class AtaImpactClient(HttpJsonClient):
    service_name = "mro-ata-impact"

    def analyze(self, request_text: str, fields: dict[str, Any]) -> tuple[dict[str, Any], object]:
        return self.post_json({"request": request_text, "fields": fields, "mode": "auto"})

