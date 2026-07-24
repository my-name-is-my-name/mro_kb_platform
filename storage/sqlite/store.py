from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
import re
import threading
from typing import Iterator

from core.models.entities import CaseSummary, DocumentChunk
from storage.sqlite.schema import SCHEMA_SQL


SEARCH_STOP_TOKENS = {
    "в",
    "во",
    "на",
    "по",
    "при",
    "для",
    "как",
    "что",
    "где",
    "какой",
    "каких",
    "заявке",
    "заявках",
    "есть",
    "были",
    "была",
    "ли",
    "выполнять",
    "выполнить",
    "выполнение",
    "выполните",
    "провести",
    "проведите",
    "сделать",
    "сделайте",
}


class SQLiteStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._write_lock = threading.RLock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            with self.connect() as conn:
                conn.executescript(SCHEMA_SQL)
                self._migrate_chunks_schema(conn)
                self._migrate_documents_schema(conn)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_trust_level ON documents(trust_level)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_aircraft_type ON documents(aircraft_type)")

    @staticmethod
    def _migrate_chunks_schema(conn: sqlite3.Connection) -> None:
        existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
        migrations = {
            "unit_kind": "ALTER TABLE chunks ADD COLUMN unit_kind TEXT NOT NULL DEFAULT 'chunk'",
            "chunk_level": "ALTER TABLE chunks ADD COLUMN chunk_level TEXT NOT NULL DEFAULT ''",
            "document_family": "ALTER TABLE chunks ADD COLUMN document_family TEXT NOT NULL DEFAULT ''",
            "section_label": "ALTER TABLE chunks ADD COLUMN section_label TEXT NOT NULL DEFAULT ''",
            "heading_path_json": "ALTER TABLE chunks ADD COLUMN heading_path_json TEXT NOT NULL DEFAULT '[]'",
            "search_text": "ALTER TABLE chunks ADD COLUMN search_text TEXT NOT NULL DEFAULT ''",
            "table_refs_json": "ALTER TABLE chunks ADD COLUMN table_refs_json TEXT NOT NULL DEFAULT '[]'",
            "metadata_json": "ALTER TABLE chunks ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'",
        }
        for column, statement in migrations.items():
            if column not in existing:
                conn.execute(statement)

    @staticmethod
    def _migrate_documents_schema(conn: sqlite3.Connection) -> None:
        existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
        migrations = {
            "document_type": "ALTER TABLE documents ADD COLUMN document_type TEXT NOT NULL DEFAULT ''",
            "issuer": "ALTER TABLE documents ADD COLUMN issuer TEXT NOT NULL DEFAULT ''",
            "aircraft_type": "ALTER TABLE documents ADD COLUMN aircraft_type TEXT NOT NULL DEFAULT ''",
            "effectivity": "ALTER TABLE documents ADD COLUMN effectivity TEXT NOT NULL DEFAULT ''",
            "ata": "ALTER TABLE documents ADD COLUMN ata TEXT NOT NULL DEFAULT ''",
            "revision": "ALTER TABLE documents ADD COLUMN revision TEXT NOT NULL DEFAULT ''",
            "issue_date": "ALTER TABLE documents ADD COLUMN issue_date TEXT NOT NULL DEFAULT ''",
            "section_reference": "ALTER TABLE documents ADD COLUMN section_reference TEXT NOT NULL DEFAULT ''",
            "source_url": "ALTER TABLE documents ADD COLUMN source_url TEXT NOT NULL DEFAULT ''",
            "trust_level": "ALTER TABLE documents ADD COLUMN trust_level TEXT NOT NULL DEFAULT 'internal_reference'",
            "source_origin": "ALTER TABLE documents ADD COLUMN source_origin TEXT NOT NULL DEFAULT 'internal'",
        }
        for column, statement in migrations.items():
            if column not in existing:
                conn.execute(statement)

    def replace_cases(self, items: list[CaseSummary]) -> None:
        with self._write_lock:
            with self.connect() as conn:
                conn.execute("DELETE FROM cases")
                conn.executemany(
                    """
                    INSERT INTO cases (
                        case_id, aircraft_type, msn, subject, problem_summary,
                        ata_list_json, applicable_ap_refs_json, source_document_id, source_system, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.case_id,
                            item.aircraft_type,
                            item.msn,
                            item.subject,
                            item.problem_summary,
                            json.dumps(item.ata_list, ensure_ascii=False),
                            json.dumps(item.applicable_ap_refs, ensure_ascii=False),
                            item.source_document_id,
                            item.source_system,
                            json.dumps(item.metadata, ensure_ascii=False),
                        )
                        for item in items
                    ],
                )

    def replace_documents_and_chunks(
        self,
        documents: list[dict[str, str]],
        chunks: list[DocumentChunk],
        links: list[tuple[str, str, str]],
    ) -> None:
        with self._write_lock:
            with self.connect() as conn:
                conn.execute("DELETE FROM case_document_links")
                conn.execute("DELETE FROM chunks")
                conn.execute("DELETE FROM documents")
                conn.executemany(
                    """
                    INSERT INTO documents (
                        document_id, case_id, source_document_id, title, subject, document_family, source_file, source_system,
                        document_type, issuer, aircraft_type, effectivity, ata, revision, issue_date, section_reference,
                        source_url, trust_level, source_origin
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            row["document_id"],
                            row["case_id"],
                            row.get("source_document_id", ""),
                            row.get("title", ""),
                            row.get("subject", ""),
                            row.get("document_family", ""),
                            row.get("source_file", ""),
                            row.get("source_system", "mro_rag"),
                            row.get("document_type", row.get("document_family", "")),
                            row.get("issuer", row.get("oem_or_regulator", "")),
                            row.get("aircraft_type", ""),
                            row.get("effectivity", row.get("msn_effectivity", "")),
                            row.get("ata", ""),
                            row.get("revision", ""),
                            row.get("issue_date", row.get("date", "")),
                            row.get("section_reference", row.get("section", "")),
                            row.get("source_url", ""),
                            row.get("trust_level", "internal_reference"),
                            row.get("source_origin", "internal"),
                        )
                        for row in documents
                    ],
                )
                conn.executemany(
                    """
                    INSERT INTO chunks (
                        chunk_id, case_id, document_id, chunk_kind, unit_kind, chunk_level, document_family,
                        section_label, section_title, heading_path_json, text, search_text,
                        table_refs_json, metadata_json, page_number,
                        source_file, source_system, block_id, vault_note_path, page_image_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            chunk.chunk_id,
                            chunk.case_id,
                            chunk.document_id,
                            chunk.chunk_kind,
                            "table" if chunk.chunk_kind == "table" else ("case" if chunk.document_id.endswith("::case") else "chunk"),
                            chunk.chunk_level,
                            chunk.document_family,
                            chunk.section_label,
                            chunk.section_title,
                            json.dumps(chunk.heading_path, ensure_ascii=False),
                            chunk.text,
                            chunk.search_text,
                            json.dumps(chunk.table_refs, ensure_ascii=False),
                            json.dumps(chunk.metadata, ensure_ascii=False),
                            chunk.page_number,
                            chunk.source_file,
                            chunk.source_system,
                            chunk.block_id,
                            chunk.vault_note_path,
                            chunk.page_image_path,
                        )
                        for chunk in chunks
                    ],
                )
                conn.executemany(
                    "INSERT INTO case_document_links (case_id, document_id, link_status) VALUES (?, ?, ?)",
                    links,
                )

    def write_snapshot(self, source_root: str, content_hash: str, source_label: str) -> int:
        with self._write_lock:
            with self.connect() as conn:
                cur = conn.execute(
                    "INSERT INTO corpus_snapshots (created_at, source_root, content_hash, source_label) VALUES (?, ?, ?, ?)",
                    (datetime.now(UTC).isoformat(), source_root, content_hash, source_label),
                )
                return int(cur.lastrowid)

    def record_failure(self, source_label: str, source_path: str, error_text: str) -> None:
        with self._write_lock:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO ingest_failures (source_label, source_path, error_text, created_at) VALUES (?, ?, ?, ?)",
                    (source_label, source_path, error_text, datetime.now(UTC).isoformat()),
                )

    def fetch_cases(self, limit: int = 200) -> list[dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cases ORDER BY case_id LIMIT ?",
                (limit,),
            ).fetchall()
            payloads: list[dict[str, object]] = []
            for row in rows:
                payload = dict(row)
                payload["ata_list"] = json.loads(str(payload.pop("ata_list_json", "[]")))
                payload["applicable_ap_refs"] = json.loads(str(payload.pop("applicable_ap_refs_json", "[]")))
                payload["metadata"] = json.loads(str(payload.pop("metadata_json", "{}")))
                payloads.append(payload)
            return payloads

    def fetch_case(self, case_id: str) -> dict[str, object] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
            if row is None:
                return None
            payload = dict(row)
            payload["ata_list"] = json.loads(str(payload.pop("ata_list_json", "[]")))
            payload["applicable_ap_refs"] = json.loads(str(payload.pop("applicable_ap_refs_json", "[]")))
            payload["metadata"] = json.loads(str(payload.pop("metadata_json", "{}")))
            payload["documents"] = [
                dict(item)
                for item in conn.execute(
                    """
                    SELECT document_id, source_document_id, title, subject, document_family, source_file
                    FROM documents
                    WHERE case_id = ?
                    ORDER BY document_id
                    """,
                    (case_id,),
                ).fetchall()
            ]
            return payload

    def fetch_document(self, document_id: str) -> dict[str, object] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()
            if row is None:
                return None
            payload = dict(row)
            payload["chunks"] = [
                dict(item)
                for item in conn.execute(
                    """
                    SELECT chunk_id, chunk_kind, unit_kind, chunk_level, document_family, section_label,
                           section_title, heading_path_json, text, search_text, table_refs_json,
                           page_number, block_id, vault_note_path, page_image_path
                    FROM chunks
                    WHERE document_id = ?
                    ORDER BY chunk_id
                    """,
                    (document_id,),
                ).fetchall()
            ]
            return payload

    def fetch_chunk(self, chunk_id: str) -> dict[str, object] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT h.chunk_id, h.case_id, h.document_id, h.chunk_kind, h.unit_kind, h.chunk_level,
                       h.section_label, h.section_title, h.heading_path_json,
                       h.text, h.search_text, h.table_refs_json, h.metadata_json, h.page_number,
                       h.source_file, h.block_id, h.vault_note_path, h.page_image_path,
                       d.source_document_id, d.title AS document_title, d.document_type, d.issuer,
                       d.aircraft_type AS document_aircraft_type, d.effectivity, d.ata AS document_ata,
                       d.revision, d.issue_date, d.section_reference, d.source_url, d.trust_level, d.source_origin
                FROM chunks h
                JOIN documents d ON d.document_id = h.document_id
                WHERE h.chunk_id = ?
                """,
                (chunk_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def search_text(self, query: str, limit: int = 8, case_ids: list[str] | None = None) -> list[dict[str, object]]:
        tokens = self._search_terms(query)
        if not tokens:
            fallback = (query or "").strip().lower().replace("ё", "е")
            tokens = [fallback] if fallback else []
        if not tokens:
            return []
        conditions = []
        params: list[object] = []
        score_terms = []
        coverage_terms = []
        for token in tokens:
            like = f"%{token}%"
            conditions.append(
                "("
                "lower(h.search_text) LIKE lower(?) OR "
                "lower(h.text) LIKE lower(?) OR "
                "lower(h.section_title) LIKE lower(?) OR "
                "lower(h.section_label) LIKE lower(?) OR "
                "lower(d.title) LIKE lower(?) OR "
                "lower(d.source_document_id) LIKE lower(?) OR "
                "lower(c.subject) LIKE lower(?) OR "
                "lower(c.problem_summary) LIKE lower(?) OR "
                "lower(c.aircraft_type) LIKE lower(?) OR "
                "lower(c.msn) LIKE lower(?)"
                ")"
            )
            score_terms.append(
                "("
                "CASE WHEN lower(h.search_text) LIKE lower(?) THEN 8 ELSE 0 END + "
                "CASE WHEN lower(h.text) LIKE lower(?) THEN 5 ELSE 0 END + "
                "CASE WHEN lower(h.section_title) LIKE lower(?) THEN 4 ELSE 0 END + "
                "CASE WHEN lower(h.section_label) LIKE lower(?) THEN 4 ELSE 0 END + "
                "CASE WHEN lower(d.title) LIKE lower(?) THEN 3 ELSE 0 END + "
                "CASE WHEN lower(d.source_document_id) LIKE lower(?) THEN 3 ELSE 0 END + "
                "CASE WHEN lower(c.subject) LIKE lower(?) THEN 2 ELSE 0 END + "
                "CASE WHEN lower(c.problem_summary) LIKE lower(?) THEN 2 ELSE 0 END + "
                "CASE WHEN lower(c.aircraft_type) LIKE lower(?) THEN 1 ELSE 0 END + "
                "CASE WHEN lower(c.msn) LIKE lower(?) THEN 1 ELSE 0 END"
                ")"
            )
            coverage_terms.append(
                "CASE WHEN ("
                "lower(h.search_text) LIKE lower(?) OR "
                "lower(h.text) LIKE lower(?) OR "
                "lower(h.section_title) LIKE lower(?) OR "
                "lower(h.section_label) LIKE lower(?) OR "
                "lower(d.title) LIKE lower(?) OR "
                "lower(d.source_document_id) LIKE lower(?) OR "
                "lower(c.subject) LIKE lower(?) OR "
                "lower(c.problem_summary) LIKE lower(?)"
                ") THEN 1 ELSE 0 END"
            )
            params.extend([like, like, like, like, like, like, like, like, like, like])
            params.extend([like, like, like, like, like, like, like, like, like, like])
            params.extend([like, like, like, like, like, like, like, like])
        where_clauses = [f"({' OR '.join(conditions)})"]
        if case_ids:
            case_placeholders = ",".join("?" for _ in case_ids)
            where_clauses.append(f"c.case_id IN ({case_placeholders})")
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT c.case_id, c.subject, c.problem_summary, c.aircraft_type, c.msn,
                       d.document_id, d.source_document_id, d.title, d.document_family,
                       h.chunk_id, h.chunk_kind, h.unit_kind, h.chunk_level,
                       h.section_label, h.section_title, h.heading_path_json, h.text, h.search_text,
                       h.table_refs_json, h.metadata_json, h.source_file,
                       h.vault_note_path, h.block_id, h.page_image_path,
                       ({' + '.join(score_terms)}) AS lexical_score,
                       ({' + '.join(coverage_terms)}) AS token_coverage
                FROM chunks h
                JOIN documents d ON d.document_id = h.document_id
                JOIN cases c ON c.case_id = h.case_id
                WHERE {' AND '.join(where_clauses)}
                ORDER BY token_coverage DESC, lexical_score DESC, LENGTH(h.text) DESC
                LIMIT ?
                """,
                (*params, *(case_ids or []), limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def search_documents(self, query: str, limit: int = 8) -> list[dict[str, object]]:
        return self.search_documents_for_cases(query, [], limit=limit)

    def search_cases(self, query: str, limit: int = 8) -> list[dict[str, object]]:
        tokens = self._search_terms(query)
        if not tokens:
            fallback = (query or "").strip().lower().replace("ё", "е")
            tokens = [fallback] if fallback else []
        if not tokens:
            return []
        conditions = []
        params: list[object] = []
        score_terms = []
        coverage_terms = []
        for token in tokens:
            like = f"%{token}%"
            conditions.append(
                "("
                "lower(c.subject) LIKE lower(?) OR "
                "lower(c.problem_summary) LIKE lower(?) OR "
                "lower(c.aircraft_type) LIKE lower(?) OR "
                "lower(c.source_document_id) LIKE lower(?) OR "
                "lower(c.case_id) LIKE lower(?)"
                ")"
            )
            score_terms.append(
                "("
                "CASE WHEN lower(c.subject) LIKE lower(?) THEN 3 ELSE 0 END + "
                "CASE WHEN lower(c.problem_summary) LIKE lower(?) THEN 3 ELSE 0 END + "
                "CASE WHEN lower(c.aircraft_type) LIKE lower(?) THEN 1 ELSE 0 END + "
                "CASE WHEN lower(c.source_document_id) LIKE lower(?) THEN 2 ELSE 0 END + "
                "CASE WHEN lower(c.case_id) LIKE lower(?) THEN 2 ELSE 0 END"
                ")"
            )
            coverage_terms.append(
                "CASE WHEN ("
                "lower(c.subject) LIKE lower(?) OR "
                "lower(c.problem_summary) LIKE lower(?) OR "
                "lower(c.source_document_id) LIKE lower(?) OR "
                "lower(c.case_id) LIKE lower(?)"
                ") THEN 1 ELSE 0 END"
            )
            params.extend([like, like, like, like, like])
            params.extend([like, like, like, like, like])
            params.extend([like, like, like, like])
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT c.case_id, c.source_document_id, c.subject, c.problem_summary, c.aircraft_type, c.msn,
                       ({' + '.join(score_terms)}) AS lexical_score,
                       ({' + '.join(coverage_terms)}) AS token_coverage
                FROM cases c
                WHERE {" OR ".join(conditions)}
                ORDER BY token_coverage DESC, lexical_score DESC, LENGTH(c.subject) DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def search_documents_for_cases(self, query: str, case_ids: list[str], limit: int = 8) -> list[dict[str, object]]:
        tokens = self._search_terms(query)
        if not tokens:
            fallback = (query or "").strip().lower().replace("ё", "е")
            tokens = [fallback] if fallback else []
        if not tokens:
            return []
        conditions = []
        params: list[object] = []
        score_terms = []
        coverage_terms = []
        for token in tokens:
            like = f"%{token}%"
            conditions.append(
                "("
                "lower(d.title) LIKE lower(?) OR "
                "lower(d.source_document_id) LIKE lower(?) OR "
                "lower(d.subject) LIKE lower(?) OR "
                "lower(c.subject) LIKE lower(?) OR "
                "lower(c.problem_summary) LIKE lower(?) OR "
                "lower(c.aircraft_type) LIKE lower(?)"
                ")"
            )
            score_terms.append(
                "("
                "CASE WHEN lower(d.title) LIKE lower(?) THEN 6 ELSE 0 END + "
                "CASE WHEN lower(d.source_document_id) LIKE lower(?) THEN 5 ELSE 0 END + "
                "CASE WHEN lower(d.subject) LIKE lower(?) THEN 4 ELSE 0 END + "
                "CASE WHEN lower(c.subject) LIKE lower(?) THEN 3 ELSE 0 END + "
                "CASE WHEN lower(c.problem_summary) LIKE lower(?) THEN 3 ELSE 0 END + "
                "CASE WHEN lower(c.aircraft_type) LIKE lower(?) THEN 1 ELSE 0 END"
                ")"
            )
            coverage_terms.append(
                "CASE WHEN ("
                "lower(d.title) LIKE lower(?) OR "
                "lower(d.source_document_id) LIKE lower(?) OR "
                "lower(d.subject) LIKE lower(?) OR "
                "lower(c.subject) LIKE lower(?) OR "
                "lower(c.problem_summary) LIKE lower(?)"
                ") THEN 1 ELSE 0 END"
            )
            params.extend([like, like, like, like, like, like])
            params.extend([like, like, like, like, like, like])
            params.extend([like, like, like, like, like])
        where_clauses = [f"({' OR '.join(conditions)})"]
        if case_ids:
            case_placeholders = ",".join("?" for _ in case_ids)
            where_clauses.append(f"d.case_id IN ({case_placeholders})")
            params.extend(case_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT d.document_id, d.case_id, d.source_document_id, d.title, d.subject, d.document_family, d.source_file,
                       d.document_type, d.issuer, d.aircraft_type AS document_aircraft_type, d.effectivity,
                       d.ata AS document_ata, d.revision, d.issue_date, d.section_reference, d.source_url,
                       d.trust_level, d.source_origin,
                       c.problem_summary, c.aircraft_type,
                       ({' + '.join(score_terms)}) AS lexical_score,
                       ({' + '.join(coverage_terms)}) AS token_coverage
                FROM documents d
                JOIN cases c ON c.case_id = d.case_id
                WHERE {' AND '.join(where_clauses)}
                ORDER BY token_coverage DESC, lexical_score DESC, LENGTH(d.title) DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def fetch_document_chunks(self, document_id: str, limit: int = 10) -> list[dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT h.chunk_id, h.case_id, h.document_id, h.chunk_kind, h.unit_kind, h.chunk_level,
                       h.section_label, h.section_title, h.heading_path_json,
                       h.text, h.search_text, h.table_refs_json, h.metadata_json, h.page_number,
                       h.source_file, h.block_id, h.vault_note_path, h.page_image_path,
                       d.source_document_id, d.title, d.document_family, d.document_type, d.issuer,
                       d.aircraft_type AS document_aircraft_type, d.effectivity, d.ata AS document_ata,
                       d.revision, d.issue_date, d.section_reference, d.source_url, d.trust_level, d.source_origin,
                       c.subject, c.problem_summary, c.aircraft_type, c.msn
                FROM chunks h
                JOIN documents d ON d.document_id = h.document_id
                JOIN cases c ON c.case_id = h.case_id
                WHERE h.document_id = ?
                ORDER BY
                    CASE WHEN h.chunk_kind = 'table' THEN 1 ELSE 0 END,
                    LENGTH(COALESCE(NULLIF(h.search_text, ''), h.text)) DESC
                LIMIT ?
                """,
                (document_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def fetch_section_chunks(self, document_id: str, section_title: str, limit: int = 80) -> list[dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT h.chunk_id, h.case_id, h.document_id, h.chunk_kind, h.unit_kind, h.chunk_level,
                       h.section_label, h.section_title, h.heading_path_json,
                       h.text, h.search_text, h.table_refs_json, h.metadata_json, h.page_number,
                       h.source_file, h.block_id, h.vault_note_path, h.page_image_path,
                       d.source_document_id, d.title, d.document_family, d.document_type, d.issuer,
                      c.subject, c.problem_summary, c.aircraft_type, c.msn
                FROM chunks h
                JOIN documents d ON d.document_id = h.document_id
                JOIN cases c ON c.case_id = h.case_id
                WHERE h.document_id = ? AND h.section_title = ?
                ORDER BY h.rowid
                LIMIT ?
                """,
                (document_id, section_title, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _search_terms(query: str) -> list[str]:
        normalized = (query or "").lower().replace("ё", "е")
        raw_tokens = re.findall(r"[a-zа-я0-9.-]+", normalized)
        terms: list[str] = []
        seen: set[str] = set()
        for token in raw_tokens:
            if token in SEARCH_STOP_TOKENS:
                continue
            if token.startswith(("выполн", "провед", "сдела")):
                continue
            if token.isdigit():
                continue
            if len(token) < 3:
                continue
            candidates = [token]
            if token.isalpha() and len(token) >= 6:
                candidates.append(token[:6])
            elif token.isalpha() and len(token) == 5:
                candidates.append(token[:5])
            for candidate in candidates:
                if candidate not in seen:
                    seen.add(candidate)
                    terms.append(candidate)
            if len(terms) >= 8:
                break
        return terms

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "cases": int(conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]),
                "documents": int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]),
                "chunks": int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]),
                "failures": int(conn.execute("SELECT COUNT(*) FROM ingest_failures").fetchone()[0]),
            }
