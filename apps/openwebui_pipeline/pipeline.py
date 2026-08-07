from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Iterator


CASE_ID_RE = re.compile(r"\b(?:MRO|MP|WO|МР)-\d+\b", flags=re.IGNORECASE)


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
        request_payload: dict[str, object] | None = None
        if question.startswith("{"):
            parsed = json.loads(question)
            if isinstance(parsed, dict):
                request_payload = parsed
        if request_payload is None:
            match = CASE_ID_RE.search(question)
            if not match:
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
            request_payload = {"case_id": match.group(0).upper().replace("МР-", "MRO-")}
        request_payload.setdefault("categories", ["problem", "activity", "calculation", "document"])
        request_payload.setdefault("max_evidence_per_category", 5)
        request_payload.setdefault("include_references", True)
        payload = json.dumps(request_payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_base_url}/api/case-facts",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
        lines = [
            f"Статус: {data.get('status')}",
            f"Запрошенный ID: {data.get('requested_case_id')}",
            f"Внутренний ID: {data.get('resolved_case_id') or '-'}",
            f"Метод разрешения ID: {data.get('resolution_method')}",
        ]
        corpus = data.get("corpus") if isinstance(data.get("corpus"), dict) else {}
        lines.extend(
            [
                "",
                "Корпус:",
                f"- documents: {corpus.get('document_count', 0)}",
                f"- chunks: {corpus.get('chunk_count', 0)}",
                f"- references: {corpus.get('reference_count', 0)}",
            ]
        )
        facts = data.get("facts") if isinstance(data.get("facts"), list) else []
        lines.extend(["", "Подтвержденные historical facts:"])
        if not facts:
            lines.append("нет")
        for idx, fact in enumerate(facts, start=1):
            if isinstance(fact, dict):
                lines.append(f"{idx}. [{fact.get('category')}] {fact.get('value')}")
                lines.append(f"   evidence: {fact.get('evidence_text')}")
                lines.append(f"   document_id: {fact.get('document_id')}")
                lines.append(f"   chunk_id: {fact.get('chunk_id')}")
        warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else []
        if warnings:
            lines.extend(["", "Warnings:", *[f"- {warning}" for warning in warnings]])
        return "\n".join(lines)
