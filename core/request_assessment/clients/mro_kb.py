from __future__ import annotations

from .base import HttpJsonClient


class MroKbClient(HttpJsonClient):
    service_name = "mro-kb"

    def chat(self, query: str) -> tuple[dict[str, object], object]:
        return self.post_json({"q": query})

