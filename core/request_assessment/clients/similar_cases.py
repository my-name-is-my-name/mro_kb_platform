from __future__ import annotations

from typing import Any

from .base import HttpJsonClient


class SimilarCasesClient(HttpJsonClient):
    service_name = "mro-similar-cases"

    def search(self, request_text: str, context: dict[str, Any]) -> tuple[dict[str, Any], object]:
        return self.post_json({"query": request_text, "context": context})

