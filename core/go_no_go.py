from __future__ import annotations

import json
import os
import re
import hashlib
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol

from core.config import PROJECT_ROOT
from core.runtime_clients import OpenAICompatibleLLM, RuntimeSettings
from storage.sqlite.store import SQLiteStore


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ATA_RE = re.compile(r"\b(?:ATA\s*)?(\d{2})(?:\s*[-:]\s*(\d{2}))?\b", re.IGNORECASE)
EXPLICIT_ATA_RE = re.compile(r"\bATA\s*(\d{2})(?:\s*[-:]\s*(\d{2}))?\b", re.IGNORECASE)
DOCUMENT_ATA_RE = re.compile(r"\b(?:AMM|SRM|NTM|CMM|IPC|ALS)\s*(\d{2})(?:\s*[-:]\s*(\d{2}))?\b", re.IGNORECASE)
IDENTIFIER_RE = re.compile(
    r"\b(?:ATA\s*[-:]?\s*\d{2}(?:[-:]\d{2})?|(?:AD|SB|AMOC|P/?N|MSN|RIB|FRAME|FR|STGR|STRINGER)\s*[A-Z0-9./#_-]+)\b",
    re.IGNORECASE,
)
AIRCRAFT_RE = re.compile(r"\b(?:airbus\s*)?(?:a|b|boeing)\s*[-/]?\s*\d{2,4}(?:\s*/\s*\d{2,4})?\b", re.IGNORECASE)
LOCAL_STOP_TOKENS = {
    "заказчик", "просит", "требуется", "оценить", "возможность", "подготовить", "разработать",
    "для", "и", "или", "по", "на", "в", "с", "из", "это", "работы", "работа", "система",
}


class EvidenceRetriever(Protocol):
    def retrieve(self, query: str, filters: dict[str, object], limit: int = 8) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class CertificateEntry:
    system: str
    subsystem: str
    name: str
    explanation: str

    @property
    def ata(self) -> str:
        system = self.system.zfill(2) if self.system.isdigit() else self.system
        subsystem = self.subsystem.replace("-", "") if self.subsystem else ""
        return f"ATA {system}" + (f"-{subsystem}" if subsystem else "")


class CertificateCatalog:
    """Reads the available certificate chapter list without making it a final approval."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(os.getenv("MRO_KB_CERTIFICATE_PATH", PROJECT_ROOT / "sertifikat_glavy_new.docx"))
        self.entries = self._load()
        self.by_system: dict[str, list[CertificateEntry]] = {}
        for entry in self.entries:
            self.by_system.setdefault(entry.system.zfill(2), []).append(entry)

    def _load(self) -> list[CertificateEntry]:
        if not self.path.exists():
            return []
        try:
            with zipfile.ZipFile(self.path) as archive:
                root = ET.fromstring(archive.read("word/document.xml"))
        except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile):
            return []
        entries: list[CertificateEntry] = []
        for table in root.findall(f".//{{{W_NS}}}tbl"):
            for row in table.findall(f"./{{{W_NS}}}tr"):
                cells = []
                for cell in row.findall(f"./{{{W_NS}}}tc"):
                    cells.append(" ".join((item.text or "") for item in cell.findall(f".//{{{W_NS}}}t")).strip())
                if len(cells) < 4:
                    continue
                system = self._code(cells[0])
                subsystem = self._code(cells[1])
                if not system or not (system.isdigit() and len(system) <= 2):
                    continue
                if subsystem and subsystem.startswith("-"):
                    subsystem = subsystem[1:]
                entries.append(CertificateEntry(system, subsystem, cells[2], cells[3]))
        return entries

    @staticmethod
    def _code(value: str) -> str:
        value = re.sub(r"\s+", "", value or "")
        if value.startswith("-"):
            return value
        return value if re.fullmatch(r"\d{1,2}", value) else ""

    def match(self, ata_codes: list[str]) -> dict[str, object]:
        normalized = sorted({self._normalize_ata(code) for code in ata_codes if self._normalize_ata(code)})
        if not normalized:
            return {"status": "unknown", "matched": [], "unmatched": [], "catalog_loaded": bool(self.entries)}
        matched: list[dict[str, str]] = []
        unmatched: list[str] = []
        for code in normalized:
            system = re.search(r"(\d{2})", code)
            system = system.group(1) if system else ""
            entries = self.by_system.get(system, [])
            if entries:
                matched.append({"ata": code, "certificate_ata": entries[0].ata, "name": entries[0].name})
            else:
                unmatched.append(code)
        status = "out_of_scope" if unmatched else "in_scope_candidate"
        return {"status": status, "matched": matched, "unmatched": unmatched, "catalog_loaded": bool(self.entries), "source": str(self.path)}

    def description_matches(self, text: str) -> dict[str, list[str]]:
        """Return conservative lexical matches against the certificate's text.

        The certificate is the primary local chapter description.  We only use
        distinctive component/structure words here; generic terms such as
        "equipment" or "components" would make a chapter match meaningless.
        """
        generic = {"элемент", "элементы", "система", "системы", "оборудование", "компонент", "компоненты", "детали", "деталь", "установка", "применяемые", "самолет", "самолета", "aircraft"}
        lowered = (text or "").lower()
        result: dict[str, list[str]] = {}
        for entry in self.entries:
            terms = re.findall(r"[a-zа-яё]{5,}", f"{entry.name} {entry.explanation}".lower(), re.IGNORECASE)
            hits: list[str] = []
            for term in terms:
                if term in generic:
                    continue
                stem = term[:6] if len(term) >= 7 else term[:5]
                if re.search(rf"(?<![a-zа-яё]){re.escape(stem)}[a-zа-яё]*(?![a-zа-яё])", lowered, re.IGNORECASE):
                    hits.append(term)
            if hits:
                result.setdefault(f"ATA {entry.system.zfill(2)}", []).extend(dict.fromkeys(hits))
        return result

    @staticmethod
    def _normalize_ata(value: str) -> str:
        match = re.search(r"(?:ATA\s*)?(\d{2})(?:\s*[-:]\s*(\d{2}))?", value or "", re.IGNORECASE)
        if not match:
            return ""
        return f"ATA {match.group(1)}" + (f"-{match.group(2)}" if match.group(2) else "")


class AtaDiscoveryCatalog:
    """Ranks certificate ATA chapters using an expert-managed vocabulary, not code rules."""

    def __init__(self, certificate: CertificateCatalog, path: Path | None = None) -> None:
        self.certificate = certificate
        self.path = path or PROJECT_ROOT / "config" / "ata_catalog_overrides.json"
        self.payload = self._load()
        self.version = str(self.payload.get("version") or "unversioned")
        self.aliases = {str(key).upper(): [str(item).lower() for item in value] for key, value in (self.payload.get("aliases") or {}).items() if isinstance(value, list)}
        self.alias_sources = {str(key).upper(): value for key, value in (self.payload.get("alias_sources") or {}).items() if isinstance(value, dict)}
        self.impact_rules = [item for item in (self.payload.get("impact_rules") or []) if isinstance(item, dict)]

    def _load(self) -> dict[str, object]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def candidates(self, text: str, declared_ata: list[str] | None = None, limit: int = 5) -> list[dict[str, object]]:
        """Return explainable ATA candidates from the certificate and approved vocabulary."""
        lowered = (text or "").lower()
        request_tokens = re.findall(r"[A-Za-zА-Яа-яЁё]{4,}", lowered)
        declared = {CertificateCatalog._normalize_ata(value) for value in (declared_ata or [])}
        declared.discard("")
        grouped = {f"ATA {system}": entries[0].name for system, entries in self.certificate.by_system.items() if entries}
        ranked: list[dict[str, object]] = []
        for ata, name in grouped.items():
            reasons: list[str] = []
            score = 0.0
            if ata in declared:
                score += 100.0
                reasons.append("ATA явно указана в заявке")
            aliases = self.aliases.get(ata, [])
            phrase_hits = [phrase for phrase in aliases if self._phrase_matches(phrase, lowered, request_tokens)]
            if phrase_hits:
                score += 20.0 + max(len(hit.split()) for hit in phrase_hits)
                reasons.append("совпадение со справочником компонентов: " + ", ".join(phrase_hits[:2]))
            name_tokens = [token.lower() for token in re.findall(r"[A-Za-zА-Яа-яЁё]{4,}", name)]
            token_hits = [token for token in name_tokens if token in lowered]
            if token_hits:
                score += float(len(token_hits))
                reasons.append("совпадение с названием ATA: " + ", ".join(token_hits[:3]))
            if score:
                source_meta = self.alias_sources.get(ata, {}) if phrase_hits else {}
                ranked.append({
                    "ata": ata, "name": name, "score": score, "reasons": reasons,
                    "source": "certificate_catalog" if not phrase_hits else "expert_catalog",
                    "source_id": source_meta.get("source_id", "certificate_docx" if not phrase_hits else ""),
                    "review_status": source_meta.get("review_status", "certificate_derived" if not phrase_hits else ""),
                })
        ranked.sort(key=lambda item: (-float(item["score"]), str(item["ata"])))
        return ranked[:limit]

    @staticmethod
    def _phrase_matches(phrase: str, text: str, request_tokens: list[str]) -> bool:
        """Match approved component names across simple Russian/English word forms."""
        if phrase in text:
            return True
        phrase_tokens = re.findall(r"[A-Za-zА-Яа-яЁё]{4,}", phrase)
        def stem(token: str) -> str:
            # Russian inflection commonly changes the fifth character
            # (крыло → крыла); four characters remain conservative for the
            # component vocabulary while English terms retain five.
            return token[:4] if re.search(r"[А-Яа-яЁё]", token) else token[:5]
        return bool(phrase_tokens) and all(
            any(token.startswith(stem(part)) or part.startswith(stem(token)) for token in request_tokens)
            for part in phrase_tokens
        )

    def related(self, text: str, known_ata: list[str]) -> list[dict[str, object]]:
        """Suggest related ATA from versioned rules; suggestions remain unconfirmed."""
        lowered = (text or "").lower()
        known = set(known_ata)
        candidates: list[dict[str, object]] = []
        for rule in self.impact_rules:
            terms = [str(term).lower() for term in (rule.get("when_any") or [])]
            if not any(term and term in lowered for term in terms):
                continue
            for ata in rule.get("add") or []:
                normalized = CertificateCatalog._normalize_ata(str(ata))
                if normalized and normalized not in known:
                    candidates.append({"ata": normalized, "reason": str(rule.get("reason") or rule.get("id") or "правило связи ATA"), "rule_id": str(rule.get("id") or ""), "source": "impact_rule_catalog"})
        return candidates

    def evidence_candidates(self, documents: list[dict[str, object]], known_ata: list[str]) -> list[dict[str, object]]:
        """Extract only an explicit ATA scope statement from evidence.

        AMM/NTM/SRM references identify a procedure, not an affected system.
        They must never create a new ATA-impact hypothesis.
        """
        known = set(known_ata)
        known_chapters = {self._chapter(ata) for ata in known}
        candidates: dict[str, dict[str, object]] = {}
        for document in documents:
            text = " ".join(str(document.get(key) or "") for key in ("title", "snippet"))
            for match in EXPLICIT_ATA_RE.finditer(text):
                ata = CertificateCatalog._normalize_ata(match.group(0))
                # ATA 34-11 is a subdivision of already direct ATA 34, not a
                # second aircraft system requiring an ATA-impact assessment.
                if ata and ata not in known and self._chapter(ata) not in known_chapters:
                    kind = "ATA явно упомянута в найденном документе"
                    start = max(0, match.start() - 240)
                    end = min(len(text), match.end() + 360)
                    card = {
                        "document_id": str(document.get("document_id") or ""), "chunk_id": str(document.get("chunk_id") or ""),
                        "title": str(document.get("title") or document.get("document_title") or ""), "path": str(document.get("path") or ""),
                        "source_type": str(document.get("source_type") or ""), "trust_level": str(document.get("trust_level") or "internal"),
                        "reference": match.group(0), "excerpt": text[start:end].strip(),
                    }
                    item = candidates.setdefault(ata, {"ata": ata, "reason": kind, "source": "evidence_iteration", "evidence_cards": []})
                    cards = item["evidence_cards"]
                    if isinstance(cards, list) and card not in cards:
                        cards.append(card)
        return list(candidates.values())

    @staticmethod
    def _chapter(ata: str) -> str:
        match = re.search(r"(\d{2})", ata)
        return match.group(1) if match else ""


class AtaImpactAgent:
    """Controlled ATA intake with an optional, bounded secondary-ATA evidence pass.

    A secondary ATA is never confirmed by a case, a filename, or an internet
    result.  Only an applicable controlled OEM/approved document can promote a
    hypothesis to ``confirmed_affected``.
    """

    def __init__(self, certificate: CertificateCatalog | None = None, catalog: AtaDiscoveryCatalog | None = None, retriever: EvidenceRetriever | None = None, llm: OpenAICompatibleLLM | None = None, internet_retriever: Any | None = None) -> None:
        self.certificate = certificate or CertificateCatalog()
        self.catalog = catalog or AtaDiscoveryCatalog(self.certificate)
        self.retriever = retriever
        self.internet_retriever = internet_retriever or DuckDuckGoEvidenceRetriever()
        self.ontology_path = PROJECT_ROOT / "config" / "mro_ontology_v1.json"
        self.ontology = self._load_ontology()
        self.evidence_enabled = True
        settings = RuntimeSettings()
        ata_llm_flag = os.getenv("MRO_KB_ATA_AGENT_LLM_ENABLED")
        enabled = (
            settings.llm_enabled
            if ata_llm_flag is None
            else ata_llm_flag.strip().lower() in {"1", "true", "yes", "on"}
        )
        ata_settings = replace(
            settings,
            llm_max_tokens=int(os.getenv("MRO_KB_ATA_LLM_MAX_TOKENS", "4000")),
            llm_timeout_seconds=float(os.getenv("MRO_KB_ATA_LLM_TIMEOUT_SECONDS", "90")),
        )
        self._llm = llm or (OpenAICompatibleLLM(ata_settings) if enabled and settings.llm_enabled and settings.llm_provider == "openai" else None)

    def analyze(self, request: str, fields: dict[str, object] | None = None, progress: Callable[[dict[str, object]], None] | None = None, mode: str = "auto") -> dict[str, object]:
        # v2 is the default. The old modes remain as explicit, deprecated
        # compatibility fallbacks while consumers migrate.
        if mode in {"auto", "standard", "extended"}:
            from core.ata_impact.evidence import LegacyEvidenceRetrieverAdapter, NullAtaEvidenceRetriever
            from core.ata_impact.service import AtaImpactService

            evidence = LegacyEvidenceRetrieverAdapter(self.retriever) if self.retriever is not None else NullAtaEvidenceRetriever()
            return AtaImpactService(self.certificate, self._llm, evidence).analyze(
                request,
                fields,
                runtime_mode=mode,
                progress=progress,
            )

        def report(stage: str, message: str, **extra: object) -> None:
            if progress is not None:
                progress({"stage": stage, "message": message, **extra})

        fields = fields if isinstance(fields, dict) else {}
        mode = mode if mode in {"rules_only", "ontology_llm", "full_pipeline"} else "ontology_llm"
        text = " ".join(str(x) for x in (request, *[fields.get(k) or "" for k in ("component", "components", "asset_name", "zone", "zones", "part_number")])).strip()
        facts = self._extract_intake_facts(text, fields)
        matches = self._ontology_matches(text, facts)
        direct_system = self._role_items(matches, "direct_system")
        direct_structural = self._role_items(matches, "direct_structural")
        secondary = self._role_items(matches, "secondary_hypothesis")
        declared = self._explicit_ata(" ".join(str(fields.get(k) or "") for k in ("ata", "ata_code", "ata_codes")) + " " + request)
        for ata in declared:
            if ata not in {item["ata"] for item in direct_system + direct_structural}:
                direct_system.append(self._manual_ata(ata))
        trace = [{"step": "fact_extraction", "status": "completed", "facts": facts}, {"step": "ontology_linking", "status": "completed", "ontology_version": self.ontology.get("version"), "candidates": [m["ata"] for m in matches]}]
        if mode != "rules_only" and self._llm is not None:
            allowed = sorted({i["ata"] for i in direct_system + direct_structural})
            report("llm_critic", "LLM-критик проверяет только разрешённые кандидаты онтологии.", candidates=allowed)
            verdict = self._react_plan(request, fields, direct_system + direct_structural, secondary, allowed)
            if verdict:
                selected = {str(v) for v in verdict.get("direct_ata", []) if str(v) in allowed}
                direct_system = [i for i in direct_system if i["ata"] in selected]
                direct_structural = [i for i in direct_structural if i["ata"] in selected]
                trace.append({"step": "llm_critic", "status": "completed", "allowed_candidates": allowed, "selected": sorted(selected)})
        direct = sorted({i["ata"] for i in direct_system + direct_structural})
        secondary = self._secondary_from_relationships(text, direct, secondary)
        controlled_documents: list[dict[str, object]] = []
        internet_documents: list[dict[str, object]] = []
        confirmed_secondary: list[str] = []
        if mode == "full_pipeline":
            report("secondary_hypotheses", "Сформированы вторичные ATA-гипотезы из инженерно ревьюируемой онтологии.", candidates=[item.get("ata") for item in secondary])
            evidence_result = self._retrieve_evidence(request, direct, fields, secondary)
            raw_documents = [item for item in evidence_result.get("documents", []) if isinstance(item, dict)]
            controlled_documents, rejected = self._controlled_applicable_documents(raw_documents, fields, secondary)
            trace.append({"step": "controlled_retrieval", "status": str(evidence_result.get("status") or "completed"), "searched_for": [item.get("ata") for item in secondary], "accepted_document_ids": [item.get("document_id") for item in controlled_documents], "filtered_documents": rejected, "warnings": list(evidence_result.get("warnings") or [])})
            report("controlled_retrieval", "Поиск выполнен во внутренней controlled-базе; внешние и historical материалы отфильтрованы.", documents=len(controlled_documents), filtered=len(rejected))
            supported = self._secondary_supported_by_documents(secondary, controlled_documents)
            allowed = sorted({str(item.get("ata")) for item in supported if item.get("ata")})
            # The critic can only reject controlled-evidence candidates; it can
            # never turn an unsupported hypothesis into a confirmation.
            verdict = self._react_verify(request, direct, supported, allowed)
            confirmed_secondary = allowed
            if verdict is not None:
                confirmed_secondary = sorted({str(ata) for ata in verdict.get("confirmed_ata", []) if str(ata) in set(allowed)})
            self._apply_controlled_cards(secondary, supported)
            trace.append({"step": "secondary_verification", "status": "completed", "allowed_by_controlled_evidence": allowed, "confirmed": confirmed_secondary, "llm_used": self._llm is not None})
            # A single bounded external pass is only for context when the
            # controlled base has not established all secondary hypotheses.
            if set(item.get("ata") for item in secondary) - set(confirmed_secondary):
                internet_result = self.internet_retriever.retrieve(request, {"ata_codes": [item.get("ata") for item in secondary], "aircraft_type": fields.get("aircraft_type", "")}, limit=4)
                internet_documents = [self._mark_internet_document(item) for item in internet_result.get("documents", []) if isinstance(item, dict)]
                trace.append({"step": "internet_context", "status": str(internet_result.get("status") or "completed"), "documents": len(internet_documents), "reason": "controlled evidence missing or insufficient; sources do not confirm ATA"})
                report("internet_context", "Внешний поиск выполнен только как контекст; он не подтверждает ATA.", documents=len(internet_documents))
            trace.append({"step": "stop", "status": "completed", "reason": "two_pass_limit_reached"})
        certificate_match = self.certificate.match(direct)
        required = self._required_intake_inputs(facts, bool(direct))
        decision = "proceed_to_go_no_go" if direct and not required else "request_information"
        report("document_stage", "Документная проверка выполняется на следующем этапе.")
        result = {
            "agent": "ata_impact", "contract_version": "v1", "mode": mode, "ontology_version": self.ontology.get("version", "unavailable"),
            "input": {"aircraft_type": fields.get("aircraft_type"), "request": request}, "extracted_facts": facts,
            "direct_system_ata": direct_system, "direct_structural_ata": direct_structural, "secondary_ata_hypotheses": secondary,
            "procedure_references": self._procedure_references(text), "required_input_data": required,
            "decision": decision, "certificate_chapter_match": certificate_match,
            "provenance": {"ontology": str(self.ontology_path), "document_verification": "completed" if mode == "full_pipeline" else "not_run"}, "agent_trace": trace,
            # Deprecated compatibility fields; no capability is inferred from them.
            "direct_ata": direct, "secondary_ata": secondary, "certificate_scope": certificate_match,
            "capability_screening": "not_assessed", "confirmed_affected_ata": confirmed_secondary, "potentially_affected_ata": [i["ata"] for i in secondary],
            "controlled_evidence": self._evidence_refs(controlled_documents), "internet_context": self._evidence_refs(internet_documents),
            "needs_human_approval": True, "warnings": ([] if mode == "full_pipeline" else ["document_verification_not_run"]) + (["engineering_review_or_controlled_document_required"] if mode == "full_pipeline" and secondary and not confirmed_secondary else []),
        }
        result["answer"] = self._build_answer(result)
        report("completed", "Предварительная оценка ATA сформирована.")
        return result

    def health(self) -> dict[str, object]:
        # Model selection belongs to the OpenAI-compatible endpoint.  Do not leak
        # or pin a concrete deployment name in the ATA service contract.
        return {
            "pipeline_version": "v2",
            "default_mode": "auto",
            "llm_critic": {"enabled": self._llm is not None, "provider": "openai_compatible" if self._llm is not None else None},
            "evidence_enabled": self.evidence_enabled,
            "ontology_loaded": bool(self.ontology.get("links")),
            "legacy_ontology": {"enabled_by_default": False, "deprecated": True},
        }

    def _load_ontology(self) -> dict[str, object]:
        try:
            payload = json.loads(self.ontology_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _extract_intake_facts(text: str, fields: dict[str, object]) -> dict[str, object]:
        lowered = text.lower()
        damage_terms = ("поврежд", "трещин", "корроз", "вмятин", "царап", "скол", "разруш", "crack", "corrosion", "dent", "scratch", "damage")
        aircraft = str(fields.get("aircraft_type") or "") or (AIRCRAFT_RE.search(text).group(0) if AIRCRAFT_RE.search(text) else "")
        return {"aircraft_type": aircraft, "defect_confirmed": any(t in lowered for t in damage_terms), "damage_dimensions_present": bool(re.search(r"\b\d+(?:[.,]\d+)?\s*(?:мм|mm)\b", lowered)), "photo_present": any(t in lowered for t in ("фото", "photo", "изображен", "снимок")), "zone_present": any(t in lowered for t in ("fr", "stgr", "stringer", "шпангоут", "стрингер", "зона"))}

    def _ontology_matches(self, text: str, facts: dict[str, object]) -> list[dict[str, object]]:
        lowered = text.lower()
        certificate_matches = self.certificate.description_matches(text)
        found: list[dict[str, object]] = []
        for link in self.ontology.get("links", []):
            if not isinstance(link, dict):
                continue
            terms = [str(t).lower() for t in link.get("terms_ru", []) + link.get("terms_en", [])]
            hits = [t for t in terms if self._term_matches(t, lowered)]
            excluded = [t for t in link.get("exclusion_terms", []) if self._term_matches(str(t).lower(), lowered)]
            if not hits or excluded or (link.get("conditions", {}).get("requires_damage") and not facts["defect_confirmed"]):
                continue
            certificate_terms = certificate_matches.get(str(link["ata"]), [])
            source = dict(link.get("source", {}))
            reason = f"Совпадение объекта: {hits[0]}"
            if certificate_terms:
                reason = "Совпадение с описанием главы сертификата: " + ", ".join(certificate_terms[:3])
                source = {"source_kind": "controlled_certificate_description", "document": str(self.certificate.path), "section": str(link["ata"])}
            found.append({"ata": link["ata"], "role": link["ata_role"], "entity": link["entity"], "entity_type": link["entity_type"], "priority": int(link.get("priority") or 0), "confidence": 0.95 if len(hits) > 1 else 0.85, "reason": reason, "conditions": link.get("applicability", ""), "provenance": {"ontology_link_id": link["id"], **source, "engineering_owner": self.ontology.get("review_workflow", {}).get("engineering_owner", ""), "review_status": self.ontology.get("review_workflow", {}).get("status", "unknown"), "reviewed_at": self.ontology.get("review_workflow", {}).get("reviewed_at", "")}})
        return sorted(found, key=lambda item: (-int(item["priority"]), str(item["ata"])))

    @staticmethod
    def _term_matches(term: str, text: str) -> bool:
        """Match a complete MRO term with conservative Russian inflection support."""
        if not term:
            return False
        parts = re.findall(r"[a-zа-яё0-9]+|[^a-zа-яё0-9\s]+", term.lower(), re.IGNORECASE)
        pattern: list[str] = []
        for part in parts:
            if re.fullmatch(r"[а-яё]{4,}", part, re.IGNORECASE):
                # Four letters are enough for short stems (рама → рамы), but
                # make long technical words dangerously broad: "перечень"
                # must not match "переоборудование".  Six letters still
                # admits normal inflection of component names.
                stem_length = 6 if len(part) >= 7 else 4
                pattern.append(re.escape(part[:stem_length]) + r"[а-яё]*")
            elif re.fullmatch(r"[a-z0-9]+", part, re.IGNORECASE):
                pattern.append(re.escape(part))
            else:
                pattern.append(re.escape(part))
        joined = r"\s+".join(pattern)
        return bool(re.search(r"(?<![a-zа-яё0-9])" + joined + r"(?![a-zа-яё0-9])", text, re.IGNORECASE))

    @staticmethod
    def _role_items(matches: list[dict[str, object]], role: str) -> list[dict[str, object]]:
        dedup: dict[str, dict[str, object]] = {}
        for item in matches:
            if item.get("role") == role:
                dedup.setdefault(str(item["ata"]), item)
        return [dedup[key] for key in sorted(dedup)]

    @staticmethod
    def _manual_ata(ata: str) -> dict[str, object]:
        return {"ata": ata, "role": "direct_system", "entity": "declared ATA", "entity_type": "declared_reference", "confidence": 0.7, "reason": "ATA явно указана в заявке", "conditions": "Подлежит инженерной проверке.", "provenance": {"source": "intake_text"}}

    @staticmethod
    def _procedure_references(text: str) -> list[dict[str, str]]:
        pattern = re.compile(r"\b(AMM|SRM|NTM|CMM|IPC|ALS)\s*([A-Z0-9][A-Z0-9./_-]*)", re.IGNORECASE)
        return [{"type": m.group(1).upper(), "reference": m.group(0), "value": m.group(2)} for m in pattern.finditer(text)]

    @staticmethod
    def _required_intake_inputs(facts: dict[str, object], has_direct: bool) -> list[str]:
        missing = []
        if not facts["aircraft_type"]: missing.append("тип ВС и effectivity/MSN")
        if not has_direct: missing.append("точный объект или зона работ")
        if facts["defect_confirmed"]:
            if not facts["damage_dimensions_present"]: missing.append("размеры и координаты повреждения")
            if not facts["photo_present"]: missing.append("фотографии повреждения")
        return missing

    def _react_plan(self, request: str, fields: dict[str, object], candidates: list[dict[str, object]], related: list[dict[str, object]], direct: list[str]) -> dict[str, object] | None:
        if self._llm is None:
            return None
        allowed = sorted(set(direct))
        system = (
            "Ты — планировщик ReAct для первичного MRO ATA scope. Верни только JSON: "
            '{"direct_ata":["ATA NN"],"rationale":"..."}. '
            "Разрешено выбрать только ATA из allowed_direct_ata. Не добавляй ATA, не принимай Go/No-Go и не утверждай применимость процедуры. "
            "Если данных недостаточно, верни пустой список."
        )
        user = json.dumps({"request": request, "fields": fields, "allowed_direct_ata": allowed, "catalog_candidates": candidates, "secondary_hypotheses": related}, ensure_ascii=False)
        return self._llm_json(system, user)

    def _react_critique(self, request: str, direct: list[str], secondary: list[dict[str, object]], documents: list[dict[str, object]]) -> dict[str, object] | None:
        if self._llm is None or not secondary:
            return None
        allowed = sorted(str(item.get("ata")) for item in secondary if item.get("ata"))
        evidence = [{"title": str(item.get("title") or ""), "snippet": str(item.get("snippet") or "")[:600], "path": str(item.get("path") or "")} for item in documents[:3]]
        system = (
            "Ты — критик ReAct для MRO ATA scope. Верни только JSON: "
            '{"accepted_secondary_ata":["ATA NN"],"rationale":"..."}. '
            "Разрешено выбрать только ATA из allowed_secondary_ata. Принимай только гипотезы, поддержанные текстом заявки или документом; "
            "при сомнении отклоняй. Это не final approval."
        )
        user = json.dumps({"request": request, "direct_ata": direct, "allowed_secondary_ata": allowed, "secondary_candidates": secondary, "evidence": evidence}, ensure_ascii=False)
        return self._llm_json(system, user)

    def _react_verify(self, request: str, direct: list[str], secondary: list[dict[str, object]], allowed: list[str]) -> dict[str, object] | None:
        if self._llm is None or not allowed:
            return None
        cards = [item for item in secondary if str(item.get("ata")) in set(allowed)]
        system = (
            "Ты — финальный верификатор ReAct для MRO ATA scope. Верни только JSON: "
            '{"confirmed_ata":["ATA NN"],"rationale":"..."}. '
            "Разрешено выбрать только ATA из allowed_confirmation_ata. Подтверждай ATA только если OEM/approved фрагмент прямо связывает её "
            "с объектом/работой из заявки. При малейшем сомнении верни пустой список. Это не approval ремонта."
        )
        user = json.dumps({"request": request, "direct_ata": direct, "allowed_confirmation_ata": allowed, "evidence_cards": cards}, ensure_ascii=False)
        return self._llm_json(system, user)

    def _llm_json(self, system: str, user: str) -> dict[str, object] | None:
        if self._llm is None:
            return None
        try:
            raw = self._llm.chat(system, user, allow_reasoning_fallback=True)
            start, end = raw.find("{"), raw.rfind("}")
            payload = json.loads(raw[start : end + 1]) if start >= 0 and end > start else {}
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def _retrieve_evidence(self, query: str, direct_ata: list[str], fields: dict[str, object], secondary: list[dict[str, object]]) -> dict[str, object]:
        if not self.evidence_enabled:
            return {"status": "disabled", "documents": [], "warnings": ["ata_agent_evidence_disabled"]}
        if not self.retriever:
            return {"status": "unavailable", "documents": [], "warnings": ["ata_agent_retriever_not_configured"]}
        if not direct_ata or not secondary:
            return {"status": "skipped_no_direct_ata", "documents": [], "warnings": []}
        try:
            result = self.retriever.retrieve(query, {"ata_codes": [item.get("ata") for item in secondary], "direct_ata_codes": direct_ata, "aircraft_type": fields.get("aircraft_type", ""), "msn": fields.get("msn", ""), "purpose": "secondary_ata_verification", "controlled_only": True}, limit=8)
            return {"status": "completed", **result}
        except Exception:
            return {"status": "error", "documents": [], "warnings": ["ata_agent_evidence_retrieval_failed"]}

    def _secondary_from_relationships(self, text: str, direct: list[str], existing: list[dict[str, object]]) -> list[dict[str, object]]:
        result = {str(item.get("ata") or ""): dict(item) for item in existing if item.get("ata")}
        lowered = text.lower()
        for link in self.ontology.get("secondary_links", []):
            if not isinstance(link, dict) or not set(link.get("direct_ata") or []).intersection(direct):
                continue
            terms = [str(term).lower() for term in link.get("when_any", [])]
            excluded = [str(term).lower() for term in link.get("exclusion_terms", [])]
            if terms and not any(self._term_matches(term, lowered) for term in terms):
                continue
            if any(self._term_matches(term, lowered) for term in excluded):
                continue
            ata = CertificateCatalog._normalize_ata(str(link.get("secondary_ata") or ""))
            if ata and ata not in direct:
                result[ata] = {"ata": ata, "status": "hypothesis", "reason": str(link.get("reason") or "онтологическая связь ATA"), "source": "secondary_ontology", "link_id": str(link.get("id") or ""), "required_document_types": list(link.get("required_document_types") or []), "review_status": str(link.get("review_status") or "unknown")}
        return [result[key] for key in sorted(result)]

    @staticmethod
    def _applicable_aircraft(document: dict[str, object], fields: dict[str, object]) -> bool:
        requested = re.sub(r"[^a-z0-9]", "", str(fields.get("aircraft_type") or "").lower())
        stated = re.sub(r"[^a-z0-9]", "", str(document.get("aircraft_type") or document.get("document_aircraft_type") or "").lower())
        if requested and stated and requested not in stated and stated not in requested:
            return False
        effectivity = str(document.get("effectivity") or "")
        msn = str(fields.get("msn") or "")
        return not (msn and effectivity and re.search(r"\bMSN\b|\d", effectivity, re.I) and msn not in effectivity)

    def _controlled_applicable_documents(self, documents: list[dict[str, object]], fields: dict[str, object], secondary: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
        accepted: list[dict[str, object]] = []
        rejected: list[dict[str, str]] = []
        required_types = {str(t).lower() for item in secondary for t in item.get("required_document_types", [])}
        for document in documents:
            trust = str(document.get("trust_level") or "internal_reference").lower()
            doc_type = str(document.get("document_type") or document.get("document_family") or "").lower()
            reason = ""
            if trust not in {"controlled_oem", "approved_data"}:
                reason = "trust_level_not_controlled"
            elif not self._applicable_aircraft(document, fields):
                reason = "aircraft_type_or_effectivity_mismatch"
            elif document.get("current_revision") is False:
                reason = "non_current_revision"
            elif required_types and doc_type not in required_types:
                reason = "document_type_not_required"
            if reason:
                rejected.append({"document_id": str(document.get("document_id") or ""), "reason": reason})
            else:
                accepted.append(document)
        return accepted, rejected

    @staticmethod
    def _secondary_supported_by_documents(secondary: list[dict[str, object]], documents: list[dict[str, object]]) -> list[dict[str, object]]:
        supported: list[dict[str, object]] = []
        for item in secondary:
            ata = str(item.get("ata") or "")
            chapter = re.search(r"(\d{2})", ata)
            chapter = chapter.group(1) if chapter else ""
            cards = [doc for doc in documents if chapter and (chapter in str(doc.get("ata") or doc.get("document_ata") or "") or bool(re.search(rf"\bATA\s*{chapter}\b", " ".join(str(doc.get(k) or "") for k in ("title", "snippet", "text")), re.I)))]
            if cards:
                supported.append({**item, "evidence_cards": cards})
        return supported

    @staticmethod
    def _apply_controlled_cards(secondary: list[dict[str, object]], supported: list[dict[str, object]]) -> None:
        by_ata = {str(item.get("ata") or ""): item for item in supported}
        for item in secondary:
            evidence = by_ata.get(str(item.get("ata") or ""))
            if evidence:
                item["evidence_cards"] = list(evidence.get("evidence_cards") or [])
                item["status"] = "controlled_evidence_found"

    @staticmethod
    def _mark_internet_document(document: dict[str, object]) -> dict[str, object]:
        copied = dict(document)
        url = str(copied.get("source_url") or copied.get("url") or copied.get("path") or "")
        copied["source_origin"] = "internet"
        copied["source_url"] = url
        copied["trust_level"] = "regulatory_external" if re.search(r"(?:easa\.europa\.eu|faa\.gov)", url, re.I) else "internet_unverified"
        return copied

    def _direct_details(self, direct: list[str], candidates: list[dict[str, object]], declared: list[str]) -> list[dict[str, object]]:
        by_ata = {str(item.get("ata")): item for item in candidates}
        details: list[dict[str, object]] = []
        for ata in direct:
            candidate = by_ata.get(ata, {})
            reason = "ATA явно указана в заявке" if ata in declared else "; ".join(str(item) for item in candidate.get("reasons", []))
            details.append({"ata": ata, "status": "direct", "reason": reason or "подтверждено каталогом", "source": candidate.get("source", "request"), "source_id": candidate.get("source_id", "request")})
        return details

    @staticmethod
    def _secondary_details(related: list[dict[str, object]], evidence_candidates: list[dict[str, object]], documents: list[dict[str, object]]) -> list[dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for item in related:
            ata = str(item.get("ata") or "")
            if ata:
                result[ata] = {"ata": ata, "status": "hypothesis", "reason": str(item.get("reason") or "связь ATA требует подтверждения"), "source": str(item.get("source") or "impact_rule_catalog"), "rule_id": str(item.get("rule_id") or "")}
        evidence_by_ata = {str(item.get("ata") or ""): item for item in evidence_candidates}
        for ata, item in evidence_by_ata.items():
            if not ata:
                continue
            result[ata] = {
                "ata": ata, "status": "evidence_found", "reason": str(item.get("reason") or "ATA явно упомянута в найденном документе"),
                "source": str(item.get("source") or "evidence_iteration"), "evidence_cards": list(item.get("evidence_cards") or []),
            }
        return [result[key] for key in sorted(result)]

    @staticmethod
    def _attach_validation_evidence(secondary: list[dict[str, object]], validation_candidates: list[dict[str, object]]) -> list[dict[str, object]]:
        by_ata = {str(item.get("ata") or ""): item for item in validation_candidates}
        for item in secondary:
            candidate = by_ata.get(str(item.get("ata") or ""))
            if not candidate:
                continue
            cards = list(item.get("evidence_cards") or [])
            for card in candidate.get("evidence_cards") or []:
                if card not in cards:
                    cards.append(card)
            item["evidence_cards"] = cards
            if cards:
                item["status"] = "evidence_found"
                item["reason"] = str(candidate.get("reason") or item.get("reason") or "найдены доказательства")
        return secondary

    @staticmethod
    def _trusted_evidence(card: dict[str, object]) -> bool:
        trust = str(card.get("trust_level") or "").strip().lower()
        source = str(card.get("source_type") or "").strip().lower()
        return trust in {"oem", "approved", "approved_data"} or source in {"oem", "approved", "approved_data"}

    def _oem_evidence_ata(self, secondary: list[dict[str, object]]) -> list[str]:
        return sorted({str(item.get("ata")) for item in secondary if any(self._trusted_evidence(card) for card in item.get("evidence_cards") or [])})

    @staticmethod
    def _assessments(direct: list[dict[str, object]], secondary: list[dict[str, object]], confirmed: list[str]) -> list[dict[str, object]]:
        confirmed_set = set(confirmed)
        result: list[dict[str, object]] = []
        for item in direct:
            result.append({**item, "status": "direct_candidate", "evidence_cards": list(item.get("evidence_cards") or [])})
        for item in secondary:
            copied = dict(item)
            ata = str(copied.get("ata") or "")
            cards = list(copied.get("evidence_cards") or [])
            if ata in confirmed_set:
                copied["status"] = "confirmed_affected"
            elif cards:
                copied["status"] = "expert_review_required"
            else:
                copied["status"] = "hypothesis"
            copied["evidence_cards"] = cards
            result.append(copied)
        return sorted(result, key=lambda item: str(item.get("ata") or ""))

    @staticmethod
    def _evidence_refs(documents: list[dict[str, object]]) -> list[dict[str, object]]:
        """Expose compact evidence references; full source text stays in the document store."""
        refs: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for document in documents:
            key = (str(document.get("document_id") or ""), str(document.get("chunk_id") or ""))
            if key in seen:
                continue
            seen.add(key)
            refs.append({
                "document_id": key[0], "chunk_id": key[1], "title": str(document.get("title") or ""),
                "path": str(document.get("path") or ""), "source_type": str(document.get("source_type") or ""),
                "trust_level": str(document.get("trust_level") or "unknown"), "source_origin": str(document.get("source_origin") or "internal"),
                "document_type": str(document.get("document_type") or document.get("document_family") or ""),
                "issuer": str(document.get("issuer") or ""), "aircraft_type": str(document.get("aircraft_type") or document.get("document_aircraft_type") or ""),
                "effectivity": str(document.get("effectivity") or ""), "ata": str(document.get("ata") or document.get("document_ata") or ""),
                "revision": str(document.get("revision") or ""), "issue_date": str(document.get("issue_date") or ""),
                "section_reference": str(document.get("section_reference") or ""), "source_url": str(document.get("source_url") or document.get("url") or ""),
                "snippet": str(document.get("snippet") or "")[:500],
            })
            if len(refs) >= 6:
                break
        return refs

    @staticmethod
    def _build_answer(result: dict[str, object]) -> str:
        decision = str(result.get("decision") or "request_information")
        label = "Можно передать на следующий этап" if decision == "proceed_to_go_no_go" else "Запросить информацию"
        lines = [f"## {label}", "", "Первый слой определяет ATA scope и не подтверждает capability или применимость процедуры."]
        for key, label in (("direct_system_ata", "Прямые ATA систем"), ("direct_structural_ata", "Прямые ATA конструкции"), ("secondary_ata_hypotheses", "Вторичные гипотезы")):
            items = result.get(key) or []
            if items:
                lines.extend(["", f"### {label}"])
                lines.extend(f"- {item.get('ata')} — {item.get('reason')}" for item in items if isinstance(item, dict))
        procedures = result.get("procedure_references") or []
        if procedures:
            lines.extend(["", "### Указанные процедуры"])
            lines.extend(f"- {item.get('reference')} (не является ATA scope)" for item in procedures if isinstance(item, dict))
        missing = result.get("required_input_data") or []
        if missing:
            lines.extend(["", "### Нужно запросить", *[f"- {item}" for item in missing]])
        certificate = result.get("certificate_chapter_match") or {}
        controlled = result.get("controlled_evidence") or []
        internet = result.get("internet_context") or []
        if controlled:
            lines.extend(["", "### Подтверждено controlled-документами", *[f"- {item.get('title') or item.get('document_id')}" for item in controlled if isinstance(item, dict)]])
        if internet:
            lines.extend(["", "### Найдено в интернете (контекст, не доказательство)", *[f"- {item.get('title') or item.get('source_url') or item.get('document_id')}" for item in internet if isinstance(item, dict)]])
        lines.extend(["", f"Проверка главы сертификата: {certificate.get('status', 'unknown')}", "Финальное решение по ATA и Go/No-Go остаётся за инженерным экспертом."])
        return "\n".join(lines)

    @staticmethod
    def _explicit_ata(text: str) -> list[str]:
        values: set[str] = set()
        for match in EXPLICIT_ATA_RE.finditer(text or ""):
            # A chapter embedded in an AMM/SRM/etc. citation identifies a
            # procedure reference, not affected ATA scope.
            prefix = (text or "")[max(0, match.start() - 12) : match.start()]
            if re.search(r"\b(?:AMM|SRM|NTM|CMM|IPC|ALS)\s*$", prefix, re.IGNORECASE):
                continue
            normalized = CertificateCatalog._normalize_ata(match.group(0))
            if normalized:
                values.add(normalized)
        return sorted(values)


class TSearchRetriever:
    """Optional adapter for the T-Search harness; returns an explicit fallback state."""

    def __init__(self, url: str | None = None, timeout: int = 60) -> None:
        self.url = (url or os.getenv("MRO_KB_TSEARCH_URL", "")).strip().rstrip("/")
        self.timeout = timeout
        self.explicitly_enabled = os.getenv("MRO_KB_TSEARCH_ENABLED")

    @property
    def enabled(self) -> bool:
        if not self.url:
            return False
        if self.explicitly_enabled is None:
            return True
        return self.explicitly_enabled.strip().lower() in {"1", "true", "yes", "on"}

    def retrieve(self, query: str, filters: dict[str, object], limit: int = 8) -> dict[str, object]:
        if not self.enabled:
            return {"status": "disabled", "documents": [], "warnings": ["tsearch_disabled"]}
        payload = json.dumps({"query": query, "filters": filters, "limit": limit}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}/retrieve",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            documents = body.get("documents") or body.get("evidence") or []
            return {"status": "ok", "documents": documents[:limit], "warnings": list(body.get("warnings") or [])}
        except Exception as exc:
            return {"status": "error", "documents": [], "warnings": ["tsearch_unavailable"], "error": repr(exc)}


class DuckDuckGoEvidenceRetriever:
    """Opt-in external context search; its output is intentionally non-controlled."""

    def __init__(self, timeout: int = 8) -> None:
        self.enabled = os.getenv("MRO_KB_ATA_AGENT_INTERNET_SEARCH", "").strip().lower() in {"1", "true", "yes", "on"}
        self.timeout = timeout

    def retrieve(self, query: str, filters: dict[str, object], limit: int = 4) -> dict[str, object]:
        if not self.enabled:
            return {"status": "disabled", "documents": [], "warnings": ["internet_search_disabled"]}
        terms = " ".join([query, *[str(item) for item in filters.get("ata_codes", [])]])
        try:
            from urllib.parse import quote_plus
            request = urllib.request.Request("https://html.duckduckgo.com/html/?q=" + quote_plus(terms), headers={"User-Agent": "mro-kb-platform/1.0"})
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                page = response.read().decode("utf-8", errors="ignore")
            links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, flags=re.I | re.S)
            documents = [{"document_id": f"ddg-{index}", "title": re.sub(r"<.*?>", "", title), "source_url": url, "snippet": "DuckDuckGo result; verify against source document.", "source_type": "internet_search"} for index, (url, title) in enumerate(links[:limit], 1)]
            return {"status": "completed", "documents": documents, "warnings": []}
        except Exception:
            return {"status": "error", "documents": [], "warnings": ["internet_search_unavailable"]}


class InternalEvidenceRetriever:
    _local_documents_cache: list[dict[str, str]] | None = None

    def __init__(self, store: SQLiteStore, commercial_offers: Any | None = None, tsearch: TSearchRetriever | None = None) -> None:
        self.store = store
        self.commercial_offers = commercial_offers
        self.tsearch = tsearch or TSearchRetriever()
        self.search_timeout_seconds = max(1, int(os.getenv("MRO_KB_GO_NO_GO_SEARCH_TIMEOUT_SECONDS", "6")))
        if self.__class__._local_documents_cache is None:
            self.__class__._local_documents_cache = self._load_local_documents()
        self.local_documents = self.__class__._local_documents_cache

    @staticmethod
    def _load_local_documents() -> list[dict[str, str]]:
        root = PROJECT_ROOT / "data" / "output"
        if not root.exists():
            return []
        documents: list[dict[str, str]] = []
        seen_hashes: set[str] = set()
        for path in sorted(root.rglob("*.md")):
            normalized_path = str(path).lower()
            if any(part in normalized_path for part in ("/books/", "/trainings/", "pmbok", "toyota", "/pm/")):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), path.stem)
            documents.append({"document_id": digest[:16], "title": title, "path": str(path), "text": text})
        return documents

    @staticmethod
    def _requested_ata_chapters(filters: dict[str, object]) -> set[str]:
        return {
            match.group(1)
            for value in filters.get("ata_codes", [])
            if (match := re.search(r"(\d{2})", str(value)))
        }

    @staticmethod
    def _document_matches_scope(document: dict[str, object], ata_chapters: set[str], aircraft_type: object) -> bool:
        """Keep evidence narrowly tied to the direct ATA and, when stated, aircraft type."""
        text = " ".join(str(document.get(key) or "") for key in ("title", "snippet", "text"))
        document_chapters = {
            match.group(1)
            for match in list(EXPLICIT_ATA_RE.finditer(text)) + list(DOCUMENT_ATA_RE.finditer(text))
        }
        metadata_ata = str(document.get("ata") or document.get("document_ata") or "")
        document_chapters.update(match.group(1) for match in ATA_RE.finditer(metadata_ata))
        if ata_chapters and not (document_chapters & ata_chapters):
            return False
        requested_models = {re.sub(r"[^a-z0-9]", "", item.lower()) for item in AIRCRAFT_RE.findall(str(aircraft_type or ""))}
        document_models = {re.sub(r"[^a-z0-9]", "", item.lower()) for item in AIRCRAFT_RE.findall(text)}
        return not (requested_models and document_models and requested_models.isdisjoint(document_models))

    def _search_local_documents(self, query: str, limit: int, filters: dict[str, object] | None = None) -> list[dict[str, object]]:
        filters = filters or {}
        tokens = {
            token.lower()
            for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9-]{3,}", query or "")
            if token.lower() not in LOCAL_STOP_TOKENS
        }
        if not tokens:
            return []
        ranked: list[tuple[int, dict[str, object]]] = []
        ata_chapters = self._requested_ata_chapters(filters)
        for document in self.local_documents:
            if not self._document_matches_scope(document, ata_chapters, filters.get("aircraft_type")):
                continue
            lowered = document["text"].lower()
            matched = [token for token in tokens if token in lowered]
            score = len(matched)
            technical_signal = bool(re.search(r"\b(?:ata|amm|cmm|srm|ipc|wdm|als|ad|sb|amoc|fr|rib|stress|прочност|ремонт|техническ)\b", lowered, re.IGNORECASE))
            if not score or (score < 2 and not technical_signal):
                continue
            position = min((lowered.find(token) for token in matched if lowered.find(token) >= 0), default=0)
            snippet = document["text"][max(0, position - 250) : position + 1700]
            ranked.append((score, {
                "source_type": "additional_internal_document",
                "document_id": document["document_id"],
                "title": document["title"],
                "snippet": snippet,
                "path": document["path"],
                "score": float(score),
                "trust_level": "internal",
            }))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in ranked[:limit]]

    def _search_sqlite_with_timeout(self, query: str, limit: int) -> tuple[list[dict[str, object]], str | None]:
        result: list[dict[str, object]] = []
        error: list[str] = []

        def worker() -> None:
            try:
                documents = self.store.search_documents(query, limit=max(3, limit // 2))
                for document in documents:
                    chunks = self.store.fetch_document_chunks(str(document.get("document_id", "")), limit=2)
                    for chunk in chunks:
                        merged = dict(chunk)
                        merged.update({"document_title": document.get("title", ""), "lexical_score": document.get("lexical_score", 0.0)})
                        result.append(merged)
            except Exception:
                error.append("internal_search_error")

        thread = threading.Thread(target=worker, name="go-no-go-search", daemon=True)
        thread.start()
        thread.join(timeout=self.search_timeout_seconds)
        if thread.is_alive():
            return [], "internal_search_timeout"
        return result, error[0] if error else None

    def retrieve(self, query: str, filters: dict[str, object], limit: int = 8) -> dict[str, object]:
        ata = " ".join(str(item) for item in filters.get("ata_codes", []) if item)
        search_query = " ".join(part for part in [query, ata] if part).strip()
        hits: list[dict[str, object]] = []
        warnings: list[str] = []
        if search_query:
            hits, warning = self._search_sqlite_with_timeout(search_query, limit)
            if warning:
                warnings.append(warning)
        documents: list[dict[str, object]] = []
        ata_chapters = self._requested_ata_chapters(filters)
        # Local markdown and historical MRO cases are references, never
        # controlled evidence.  Keep them out of a controlled-only ATA pass.
        if not filters.get("controlled_only"):
            documents.extend(self._search_local_documents(search_query, limit, filters))
        for hit in hits:
            item = {
                "source_type": "internal_document_chunk",
                "document_id": hit.get("document_id", ""),
                "chunk_id": hit.get("chunk_id", ""),
                "title": hit.get("document_title", "") or hit.get("section_title", ""),
                "snippet": str(hit.get("text", ""))[:1800],
                "path": hit.get("source_file", ""),
                "score": float(hit.get("lexical_score", 0.0)),
                "trust_level": "internal",
                "document_type": hit.get("document_type", hit.get("document_family", "")),
                "issuer": hit.get("issuer", ""),
                "aircraft_type": hit.get("document_aircraft_type", ""),
                "effectivity": hit.get("effectivity", ""),
                "ata": hit.get("document_ata", ""),
                "revision": hit.get("revision", ""),
                "issue_date": hit.get("issue_date", ""),
                "section_reference": hit.get("section_reference", ""),
                "source_url": hit.get("source_url", ""),
                "source_origin": hit.get("source_origin", "internal"),
            }
            item["trust_level"] = str(hit.get("trust_level") or item["trust_level"])
            if self._document_matches_scope(item, ata_chapters, filters.get("aircraft_type")):
                documents.append(item)
        if not filters.get("controlled_only"):
            tsearch_result = self.tsearch.retrieve(search_query, filters, limit=limit)
            for item in tsearch_result.get("documents", []):
                if isinstance(item, dict):
                    scoped = {"source_type": "tsearch_evidence", **item}
                    if self._document_matches_scope(scoped, ata_chapters, filters.get("aircraft_type")):
                        documents.append(scoped)
            warnings.extend(list(tsearch_result.get("warnings") or []))
        unique: list[dict[str, object]] = []
        seen: set[tuple[str, str, str]] = set()
        for document in documents:
            key = (str(document.get("document_id") or ""), str(document.get("chunk_id") or ""), str(document.get("path") or ""))
            if key not in seen:
                seen.add(key)
                unique.append(document)
            if len(unique) >= limit:
                break
        return {"documents": unique, "retrieval_mode": "scoped_tsearch_sqlite_local", "warnings": warnings}


class GoNoGoService:
    """First-line, evidence-backed recommendation for commercial MRO intake."""

    def __init__(self, store: SQLiteStore, commercial_offers: Any | None = None) -> None:
        self.store = store
        self.certificate = CertificateCatalog()
        self.ata_catalog = AtaDiscoveryCatalog(self.certificate)
        self.retriever = InternalEvidenceRetriever(store, commercial_offers)
        settings = RuntimeSettings()
        self._llm = OpenAICompatibleLLM(settings) if settings.llm_enabled and settings.llm_provider == "openai" else None
        self.ata_impact = AtaImpactAgent(self.certificate, self.ata_catalog, self.retriever)

    def health(self) -> dict[str, object]:
        return {
            "certificate": {"path": str(self.certificate.path), "loaded": bool(self.certificate.entries), "entries": len(self.certificate.entries)},
            "ata_catalog": {"path": str(self.ata_catalog.path), "version": self.ata_catalog.version, "loaded": bool(self.ata_catalog.payload)},
            "tsearch": {"enabled": self.retriever.tsearch.enabled, "url": self.retriever.tsearch.url},
            "llm_expert": self._llm.health() if self._llm is not None else {"ok": False, "disabled": True},
        }

    def triage(self, request: str, fields: dict[str, object] | None = None) -> dict[str, object]:
        fields = fields if isinstance(fields, dict) else {}
        facts = self._extract_facts(request, fields)
        if self._llm is not None and os.getenv("MRO_KB_GO_NO_GO_LLM_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
            facts = self._llm_enrich_facts(request, facts)
        staged = self.ata_impact.analyze(request, fields, mode="auto")
        impact = {
            "direct_ata": list(staged.get("affected_ata") or []),
            "potentially_affected_ata": list(staged.get("potentially_affected_ata") or []),
            "confirmed_affected_ata": list(staged.get("affected_ata") or []),
            "unresolved_ata": (
                ["ATA scope"] if staged.get("decision") == "engineering_review_required" else []
            ),
        }
        certificate = staged.get("certificate_scope") or self.certificate.match(impact["direct_ata"])
        missing = self._missing_inputs(facts)
        requested_ata = impact["confirmed_affected_ata"] or impact["direct_ata"]
        evidence_result = (
            self.retriever.retrieve(request, {"ata_codes": requested_ata, "aircraft_type": facts.get("aircraft_type", "")})
            if requested_ata
            else {"documents": [], "retrieval_mode": "skipped_no_validated_ata", "warnings": []}
        )
        evidence = evidence_result.get("documents") or []
        evidence_ata = self.ata_catalog.evidence_candidates([item for item in evidence if isinstance(item, dict)], impact["confirmed_affected_ata"])
        # Evidence search hits remain diagnostic candidates. They must not
        # bypass v2 relation/category/critic validation or mutate ATA impact.
        unresolved = impact["unresolved_ata"]
        risks: list[str] = []
        if unresolved:
            risks.append("не подтверждена полная область воздействия изменения")
        if not certificate.get("catalog_loaded"):
            risks.append("каталог сертификата не загружен")
        if not evidence:
            risks.append("внутренние доказательные документы не найдены")
        if certificate.get("status") == "out_of_scope":
            recommendation = "hold_expert_review"
            reason = "техническая ATA не найдена в текущей области сертификата; требуется отдельная capability-проверка"
        elif missing:
            recommendation = "need_more_info"
            reason = "для проверки применимости и обоснования не хватает исходных данных"
        elif unresolved or certificate.get("status") in {"unknown", "ambiguous"} or not certificate.get("catalog_loaded"):
            recommendation = "hold_expert_review"
            reason = "область работ или покрытие сертификатом не подтверждены однозначно"
        else:
            recommendation = "go_to_assessment"
            reason = "область покрыта предварительно, критические данные доступны для инженерной проработки"
        result = {
            "recommendation": recommendation,
            "confidence": self._confidence(facts, impact, certificate, missing, evidence),
            "needs_human_approval": True,
            "case_facts": facts,
            "ata_discovery": {
                "catalog_version": "deprecated-disabled",
                "catalog_source": "LLM semantic classification; certificate scope checked separately",
                "initial_candidates": staged.get("ata_mapping", {}),
                "related_candidates": [],
                "evidence_candidates": evidence_ata,
            },
            "direct_ata": impact["direct_ata"],
            "potentially_affected_ata": impact["potentially_affected_ata"],
            "confirmed_affected_ata": impact["confirmed_affected_ata"],
            "unresolved_ata": unresolved,
            "certificate_scope": certificate,
            "input_completeness": {"status": "insufficient" if missing else "sufficient", "missing_count": len(missing)},
            "missing_inputs": missing,
            "required_documents": self._required_documents(facts, impact),
            "evidence": evidence,
            "risks": risks,
            "questions_for_expert": self._expert_questions(facts, impact, evidence),
            "explanation": reason,
            "search_trace": {"retrieval_mode": evidence_result.get("retrieval_mode", ""), "warnings": evidence_result.get("warnings", [])},
            "ata_impact": staged,
            "capability_screening": "not_assessed",
        }
        result["answer"] = self._build_answer(result)
        return result

    @staticmethod
    def _build_answer(result: dict[str, object]) -> str:
        recommendation_labels = {
            "go_to_assessment": "Можно передать на инженерную проработку",
            "no_go": "Не брать: подтверждённый выход за область capability",
            "need_more_info": "Запросить дополнительную информацию",
            "hold_expert_review": "Нужна экспертная проверка",
        }
        lines = [
            f"## {recommendation_labels.get(str(result.get('recommendation')), 'Требуется проверка')}",
            "",
            str(result.get("explanation") or ""),
            "",
            f"- Уверенность: {float(result.get('confidence') or 0.0):.0%}",
            "- Требуется подтверждение экспертом: да",
        ]
        direct_ata = result.get("direct_ata") or []
        confirmed_ata = result.get("confirmed_affected_ata") or []
        if direct_ata:
            lines.append("- Прямые ATA: " + ", ".join(str(item) for item in direct_ata))
        if confirmed_ata and confirmed_ata != direct_ata:
            lines.append("- Подтверждённо затронутые ATA: " + ", ".join(str(item) for item in confirmed_ata))
        certificate = result.get("certificate_scope")
        if isinstance(certificate, dict):
            lines.append("- Сертификат: " + str(certificate.get("status") or "unknown"))
        missing = result.get("missing_inputs") or []
        if missing:
            lines.extend(["", "### Нужно запросить", *[f"- {item}" for item in missing]])
        required_documents = result.get("required_documents") or []
        if required_documents:
            lines.extend(["", "### Нужные документы", *[f"- {item}" for item in required_documents]])
        evidence = result.get("evidence") or []
        if evidence:
            lines.extend(["", "### Найденные материалы"])
            for item in evidence[:5]:
                if isinstance(item, dict):
                    title = str(item.get("title") or item.get("document_id") or "документ")
                    path = str(item.get("path") or "")
                    lines.append(f"- {title}" + (f" — `{path}`" if path else ""))
        return "\n".join(line for line in lines if line is not None).strip()

    def _extract_facts(self, request: str, fields: dict[str, object]) -> dict[str, object]:
        text = request or ""
        component_text = " ".join(str(fields.get(key) or "") for key in ("component", "components", "asset_name", "zone", "zones"))
        declared_ata = self._ata_codes(" ".join(str(fields.get(key) or "") for key in ("ata", "ata_code", "ata_codes")))
        direct_ata = sorted(set(self._ata_codes(" ".join(part for part in (text, component_text) if part)) + declared_ata))
        # Formal identifiers only. Semantic ATA classification belongs to the
        # staged AtaImpactAgent v2 invoked by triage(), never to legacy aliases.
        candidates: list[dict[str, object]] = []
        identifiers = sorted({match.group(0).strip() for match in IDENTIFIER_RE.finditer(text)})
        aircraft = str(fields.get("aircraft_type") or "") or (AIRCRAFT_RE.search(text).group(0) if AIRCRAFT_RE.search(text) else "")
        merged = {key: value for key, value in fields.items() if value not in (None, "", [])}
        merged.update({"request": text, "aircraft_type": aircraft, "direct_ata": direct_ata, "ata_candidates": candidates, "identifiers": identifiers})
        return merged

    def _llm_enrich_facts(self, request: str, facts: dict[str, object]) -> dict[str, object]:
        if self._llm is None:
            return facts
        system = "Верни только JSON. Извлеки из MRO-заявки факты, не придумывай значения и не принимай решение GO/NO-GO."
        user = f"Поля: direct_ata, affected_systems, components, zones, defect, work_type, required_inputs, required_documents, confidence.\nЗаявка:\n{request}"
        try:
            raw = self._llm.chat(system, user, allow_reasoning_fallback=True)
            payload = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if value not in (None, "", []):
                        facts[key] = value
        except Exception:
            facts.setdefault("warnings", []).append("llm_fact_extraction_failed")
        return facts

    def _impact_analysis(self, request: str, facts: dict[str, object]) -> dict[str, list[str]]:
        direct = sorted(set(str(item) for item in facts.get("direct_ata", []) if item))
        text = (request + " " + " ".join(str(facts.get(key, "")) for key in ("components", "zones", "work_type", "affected_systems"))).lower()
        potential = set(direct)
        unresolved: list[str] = []
        related = self.ata_catalog.related(text, direct)
        facts["related_ata_candidates"] = related
        potential.update(str(item.get("ata")) for item in related if item.get("ata"))
        if not direct and not facts.get("components") and not facts.get("zones"):
            unresolved.append("ATA scope")
        confirmed = sorted(potential - set(unresolved))
        return {"direct_ata": direct, "potentially_affected_ata": sorted(potential - set(direct)), "confirmed_affected_ata": confirmed, "unresolved_ata": sorted(unresolved)}

    @staticmethod
    def _ata_codes(text: str) -> list[str]:
        values = set()
        for match in ATA_RE.finditer(text or ""):
            system, subsystem = match.groups()
            # Avoid interpreting arbitrary years/numbers as ATA without an explicit ATA marker.
            prefix = text[max(0, match.start() - 4) : match.start()].lower()
            explicit = re.match(r"\s*ata", match.group(0), re.IGNORECASE) is not None
            if not explicit and not prefix.strip().endswith("ata") and not subsystem:
                continue
            values.add(f"ATA {system}" + (f"-{subsystem}" if subsystem else ""))
        return sorted(values)

    @staticmethod
    def _missing_inputs(facts: dict[str, object]) -> list[str]:
        missing: list[str] = []
        text = " ".join(str(facts.get(key) or "") for key in ("request", "component", "components", "zone", "zones", "damage_type", "defect_type")).lower()
        available_documents = " ".join(str(item) for item in (facts.get("documents_available") or [])).lower()
        if not facts.get("aircraft_type"):
            missing.append("тип ВС")
        if not facts.get("components") and not facts.get("zones") and not facts.get("direct_ata"):
            missing.append("точный объект/зона или ATA")
        if any(term in text for term in ("поврежден", "damage", "трещин", "корроз", "вмятин", "царап", "скол", "crack", "corrosion", "scratch", "chip")):
            if not any(term in text for term in ("размер", "мм", "mm", "координат")):
                missing.append("размеры/координаты повреждения")
            if not any(term in text or term in available_documents for term in ("фото", "photo", "изображен", "снимок")):
                missing.append("фотографии повреждения")
        if any(term in text for term in ("изменен", "модификац", "modification", "установ")) and not facts.get("aircraft_type"):
            missing.append("конфигурация ВС и применимость изменения")
        return missing

    @staticmethod
    def _required_documents(facts: dict[str, object], impact: dict[str, list[str]]) -> list[str]:
        docs = ["применимая эксплуатационная документация (AMM/SRM/IPC/CMM — по объекту)"]
        text = str(facts.get("request") or "").lower()
        if any(term in text for term in ("модификац", "изменен", "repair", "ремонт", "прочност", "stress")):
            docs.append("исходные чертежи/данные конструкции и применимые ремонтные ограничения")
        if any(code in impact["confirmed_affected_ata"] for code in ("ATA 04", "ATA 05")):
            docs.append("ALS/ограничения лётной годности и требования к осмотрам")
        return docs

    @staticmethod
    def _expert_questions(facts: dict[str, object], impact: dict[str, list[str]], evidence: list[object]) -> list[str]:
        questions = []
        if impact["unresolved_ata"]:
            questions.append("подтвердить полный перечень затронутых систем и ATA")
        if not evidence:
            questions.append("уточнить, есть ли доступ к применимой ревизии эксплуатационной документации")
        if not facts.get("work_type"):
            questions.append("уточнить требуемый результат: анализ, ремонт, изменение, approval или документ")
        return questions

    @staticmethod
    def _confidence(facts: dict[str, object], impact: dict[str, list[str]], certificate: dict[str, object], missing: list[str], evidence: list[object]) -> float:
        score = 0.25
        score += 0.2 if impact["direct_ata"] else 0.0
        score += 0.15 if certificate.get("catalog_loaded") else 0.0
        score += 0.15 if not missing else 0.0
        score += 0.15 if evidence else 0.0
        score -= 0.15 if impact["unresolved_ata"] else 0.0
        return round(max(0.0, min(1.0, score)), 3)
