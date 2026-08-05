from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.request_assessment.models import AssessmentState


class AssessmentStore:
    def __init__(self, path: str | Path = "data_runtime/request_assessment.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS assessments (
                    request_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def save(self, state: AssessmentState) -> AssessmentState:
        now = datetime.now(timezone.utc).isoformat()
        if not state.created_at:
            state.created_at = now
        state.updated_at = now
        payload = json.dumps(state.model_dump(mode="json"), ensure_ascii=False)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """
                INSERT INTO assessments(request_id, payload, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    payload=excluded.payload,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (state.request_id, payload, state.status.value, state.created_at, state.updated_at),
            )
        return state

    def get(self, request_id: str) -> AssessmentState | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT payload FROM assessments WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            return None
        return AssessmentState.model_validate(json.loads(row[0]))  # type: ignore[return-value]

