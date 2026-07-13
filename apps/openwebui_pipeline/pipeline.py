from __future__ import annotations

import json
import os
import urllib.request
from typing import Iterator


class Pipe:
    def __init__(self) -> None:
        self.api_base_url = os.environ.get("MRO_KB_API_BASE_URL", "http://127.0.0.1:8120")

    def pipes(self) -> list[dict[str, str]]:
        return [{"id": "mro-kb", "name": "MRO_KB"}]

    def pipe(self, body: dict[str, object]) -> str | Iterator[str]:
        messages = body.get("messages") or []
        question = ""
        if isinstance(messages, list):
            for item in reversed(messages):
                if isinstance(item, dict) and item.get("role") == "user":
                    question = str(item.get("content") or "").strip()
                    break
        if not question:
            return "Вопрос не передан."
        payload = json.dumps({"q": question}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            return str(data.get("error") or "Ошибка MRO KB")
        answer = str(data.get("answer") or "")
        sources = data.get("sources") or []
        if not sources:
            return answer
        lines = [answer, "", "Источники:"]
        for source in sources[:5]:
            lines.append(f"- {source.get('title')}")
        return "\n".join(lines)
