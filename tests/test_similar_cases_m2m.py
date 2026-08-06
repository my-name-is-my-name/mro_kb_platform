from __future__ import annotations

import json
import unittest
import urllib.error
from unittest.mock import patch

from core.ata_impact.similar_cases_client import (
    SimilarCasesClient,
    SimilarCasesClientConfig,
    attach_similar_cases,
    build_similar_cases_context,
)
from core.commercial_offers import CommercialOffersService, similar_case_status_group


def candidate(
    case_id: str,
    status: str,
    reason_class: str = "same_component_defect_zone",
    reasons: list[str] | None = None,
    score: float = 1.0,
    semantic_score: float = 0.8,
    exact_score: float = 0.0,
    structured_score: float = 3.6,
) -> dict[str, object]:
    return {
        "case": {
            "case_id": case_id,
            "status_normalized": status,
            "request_description": f"{case_id} crack repair at FR27",
            "aircraft_type": "A320",
        },
        "score": score,
        "semantic_score": semantic_score,
        "profile_semantic_score": 0.0,
        "exact_score": exact_score,
        "structured_score": structured_score,
        "similarity_reason_class": reason_class,
        "reasons": reasons
        or [
            "структурный профиль: совпал компонент",
            "структурный профиль: совпал тип дефекта",
            "структурный профиль: совпала зона/позиция",
        ],
    }


class FakeCommercialOffersService(CommercialOffersService):
    def __init__(self, candidates: list[dict[str, object]]) -> None:
        self._candidates = candidates
        self._llm = None
        self.profile_search_enabled = False
        self.public_base_url = "http://127.0.0.1:8121"
        self._index_status = {"warnings": []}

    def _rewrite_query(self, query: str) -> tuple[str, list[str]]:
        return query, []

    def _search_queries(self, query: str, query_rewrite: str) -> list[dict[str, object]]:
        return [{"text": query, "display_text": query, "source": "original_query", "weight": 1.0}]

    def _candidate_cases(self, search_queries: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
        return self._candidates[:limit]

    def _rerank_cases(self, query: str, candidates: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
        return candidates[:limit]

    def _case_evidence(self, case_id: str, query: str, max_docs: int = 3) -> list[dict[str, object]]:
        return []

    def _go_no_go_assessment(self, case: dict[str, object], evidence: list[dict[str, object]]) -> dict[str, object]:
        return {"recommended_action": "engineering_review_required"}

    def _cost_readiness(self, case: dict[str, object], evidence: list[dict[str, object]]) -> dict[str, object]:
        return {"usable_for_estimate": False, "score": 0}


class FakeLlm:
    def chat(self, system_prompt: str, prompt: str) -> str:
        return "### Источники\n\nLLM answer that must not be used"


class SimilarCasesM2MTests(unittest.TestCase):
    def test_status_grouping_is_centralized(self) -> None:
        self.assertEqual(similar_case_status_group("accepted"), "accepted")
        self.assertEqual(similar_case_status_group("in_work"), "accepted")
        self.assertEqual(similar_case_status_group("rejected"), "not_accepted")
        self.assertEqual(similar_case_status_group("cancelled"), "not_accepted")
        self.assertEqual(similar_case_status_group("no_quote"), "not_accepted")
        self.assertEqual(similar_case_status_group("on_hold"), "intermediate")
        self.assertEqual(similar_case_status_group("quote_preparation"), "intermediate")
        self.assertEqual(similar_case_status_group("in_triage"), "intermediate")
        self.assertIsNone(similar_case_status_group(""))
        self.assertIsNone(similar_case_status_group("unknown"))

    def test_api_payload_separates_five_accepted_and_five_not_accepted(self) -> None:
        service = FakeCommercialOffersService(
            [candidate(f"A{i}", "accepted") for i in range(6)]
            + [candidate("IW1", "in_work")]
            + [candidate(f"R{i}", "rejected") for i in range(3)]
            + [candidate("C1", "cancelled")]
            + [candidate("NQ1", "no_quote")]
            + [candidate("NQ2", "no_quote")]
        )

        result = service.search_similar_cases(
            {"query": "A320 frame crack repair FR27", "context": {"components": ["frame"], "defect_type": "crack", "zones": ["FR27"]}}
        )

        self.assertEqual(result["similarity_status"], "qualified_matches_found")
        self.assertEqual(len(result["accepted"]), 5)
        self.assertEqual(len(result["not_accepted"]), 5)
        self.assertEqual(result["coverage"]["accepted_available"], 7)
        self.assertEqual(result["coverage"]["not_accepted_available"], 6)

    def test_less_than_five_returns_actual_count(self) -> None:
        service = FakeCommercialOffersService([candidate("A1", "accepted"), candidate("R1", "rejected")])

        result = service.search_similar_cases({"query": "A320 frame crack repair FR27"})

        self.assertEqual([item["case_id"] for item in result["accepted"]], ["A1"])
        self.assertEqual([item["case_id"] for item in result["not_accepted"]], ["R1"])

    def test_weak_candidates_do_not_fill_lists(self) -> None:
        service = FakeCommercialOffersService(
            [
                candidate("A1", "accepted"),
                candidate("W1", "accepted", reason_class="weak_analog", reasons=["общие слова"], score=0.2, semantic_score=0.2, structured_score=0.0),
            ]
        )

        result = service.search_similar_cases({"query": "A320 frame crack repair FR27"})

        self.assertEqual([item["case_id"] for item in result["accepted"]], ["A1"])

    def test_openwebui_similar_cases_keeps_old_format_but_filters_weak_candidates(self) -> None:
        service = FakeCommercialOffersService(
            [
                candidate("W1", "accepted", reason_class="weak_analog", reasons=["общие слова"], score=0.2, semantic_score=0.2, structured_score=0.0),
                candidate("A1", "accepted", reason_class="same_identifier", reasons=["совпал инженерный идентификатор"]),
                candidate("W2", "cancelled", reason_class="weak_analog", reasons=["общие слова"], score=0.2, semantic_score=0.2, structured_score=0.0),
            ]
        )

        result = service.similar_cases("A320 frame crack repair FR27", limit=5)

        self.assertEqual([item["case_id"] for item in result["similar_cases"]], ["A1"])
        self.assertIn("| Заявка | Score | Статус/решение | Описание | Почему похожа | Что проверить | Документы |", result["answer"])
        self.assertIn("weak_similar_cases_filtered", result["warnings"])
        self.assertNotIn("### Источники", result["answer"])

    def test_openwebui_similar_cases_keeps_retrieval_answer_when_llm_is_available(self) -> None:
        service = FakeCommercialOffersService([candidate("A1", "accepted")])
        service._llm = FakeLlm()

        result = service.similar_cases("A320 frame crack repair FR27", limit=5)

        self.assertIn("| Заявка | Score | Статус/решение | Описание | Почему похожа | Что проверить | Документы |", result["answer"])
        self.assertNotIn("LLM answer that must not be used", result["answer"])
        self.assertNotIn("### Источники", result["answer"])
        self.assertEqual(result["llm_status"], "retrieval_only")

    def test_openwebui_strong_legacy_lexical_match_is_not_labeled_weak(self) -> None:
        service = FakeCommercialOffersService(
            [
                candidate(
                    "A1",
                    "accepted",
                    reason_class="weak_analog",
                    reasons=["совпали ключевые признаки: модификация, наружной, ливреи"],
                    score=1.1,
                    semantic_score=0.0,
                    structured_score=0.0,
                )
            ]
        )

        result = service.similar_cases("Модификация наружной ливреи", limit=5)

        self.assertEqual(result["similar_cases"][0]["similarity_reason_class"], "strong_lexical_analog")
        self.assertIn("strong_lexical_analog", result["answer"])

    def test_openwebui_similar_cases_caps_result_count_to_five(self) -> None:
        service = FakeCommercialOffersService([candidate(f"A{i}", "accepted", reason_class="same_identifier") for i in range(7)])

        result = service.similar_cases("A320 frame crack repair FR27", limit=10)

        self.assertEqual(len(result["similar_cases"]), 5)

    def test_m2m_legacy_ranked_query_uses_clean_query_and_groups_statuses(self) -> None:
        service = FakeCommercialOffersService(
            [
                candidate("A1", "accepted", reason_class="weak_analog", reasons=["совпали ключевые признаки: чехлов"], score=1.1),
                candidate("C1", "cancelled", reason_class="weak_analog", reasons=["совпали ключевые признаки: чехлов"], score=1.0),
                candidate("H1", "on_hold", reason_class="weak_analog", reasons=["совпали ключевые признаки: чехлов"], score=0.95),
            ]
        )

        result = service.search_similar_cases(
            {
                "query": "ремонт чехлов",
                "context": {"ata": ["ATA 25"]},
                "limits": {"accepted": 5, "not_accepted": 5},
                "retrieval_mode": "legacy_ranked_query",
            }
        )

        self.assertEqual([item["case_id"] for item in result["accepted"]], ["A1"])
        self.assertEqual([item["case_id"] for item in result["not_accepted"]], ["C1"])
        self.assertEqual([item["case_id"] for item in result["intermediate"]], ["H1"])
        self.assertEqual(result["coverage"]["intermediate_available"], 1)
        self.assertEqual(result["coverage"]["unknown_status_excluded"], 0)
        self.assertEqual(result["threshold_version"], "legacy-ranked-query-v1")

    def test_only_ata_match_is_not_qualified(self) -> None:
        service = FakeCommercialOffersService(
            [
                candidate(
                    "A1",
                    "accepted",
                    reason_class="commercially_similar",
                    reasons=["структурный профиль: совпала ATA-глава"],
                    semantic_score=0.2,
                    structured_score=1.0,
                )
            ]
        )

        result = service.search_similar_cases({"query": "A320 repair request ATA 53 damaged structure", "context": {"ata": ["ATA 53"], "components": []}})

        self.assertEqual(result["similarity_status"], "no_qualified_matches")
        self.assertEqual(result["accepted"], [])

    def test_strong_identifier_match_passes_gate(self) -> None:
        service = FakeCommercialOffersService(
            [candidate("A1", "accepted", reason_class="same_identifier", reasons=["структурный профиль: совпал инженерный идентификатор STRG12LH"], exact_score=8.0)]
        )

        result = service.search_similar_cases({"query": "STRG12LH damage", "context": {"identifiers": ["STRG12LH"]}})

        self.assertEqual(result["accepted"][0]["case_id"], "A1")
        self.assertEqual(result["accepted"][0]["similarity_confidence"], "high")

    def test_ata_code_is_not_a_strong_identifier_by_itself(self) -> None:
        service = FakeCommercialOffersService(
            [
                candidate(
                    "A1",
                    "accepted",
                    reason_class="same_identifier",
                    reasons=[
                        "по исходному запросу: точный термин: ATA25",
                        "структурный профиль: совпал инженерный идентификатор",
                    ],
                    exact_score=8.0,
                    structured_score=1.0,
                )
            ]
        )

        result = service.search_similar_cases({"query": "замена чехлов ATA 25", "context": {"ata": ["ATA 25"]}})

        self.assertEqual(result["similarity_status"], "no_qualified_matches")
        self.assertEqual(result["accepted"], [])

    def test_component_defect_zone_match_passes_gate(self) -> None:
        service = FakeCommercialOffersService([candidate("A1", "accepted")])

        result = service.search_similar_cases(
            {"query": "A320 frame crack repair FR27", "context": {"components": ["frame"], "defect_type": "crack", "zones": ["FR27"]}}
        )

        self.assertEqual(result["accepted"][0]["similarity_reason_class"], "same_component_defect_zone")

    def test_ata_context_keeps_user_supplied_ata_but_not_computed_ata(self) -> None:
        context = build_similar_cases_context(
            "ремонт чехлов ATA 25",
            {
                "affected_ata": ["ATA 57"],
                "context_ata": ["ATA 44"],
                "confirmed_affected_ata": ["ATA 53"],
                "engineering_facts": {},
            },
            {},
        )

        self.assertEqual(context["ata"], ["ATA 25"])
        self.assertEqual(context["identifiers"], [])

    def test_unknown_status_is_excluded_from_both_groups(self) -> None:
        service = FakeCommercialOffersService([candidate("A1", "accepted"), candidate("U1", "unknown")])

        result = service.search_similar_cases({"query": "A320 frame crack repair FR27"})

        self.assertEqual([item["case_id"] for item in result["accepted"]], ["A1"])
        self.assertEqual(result["coverage"]["unknown_status_excluded"], 1)
        self.assertFalse(set(item["case_id"] for item in result["accepted"]) & set(item["case_id"] for item in result["not_accepted"]))

    def test_no_qualified_matches_and_insufficient_query_statuses(self) -> None:
        weak = FakeCommercialOffersService([candidate("W1", "accepted", reason_class="weak_analog", reasons=["общие слова"], structured_score=0)])
        none_result = weak.search_similar_cases({"query": "A320 frame crack repair FR27"})
        self.assertEqual(none_result["similarity_status"], "no_qualified_matches")

        service = FakeCommercialOffersService([])
        insufficient = service.search_similar_cases({"query": "test"})
        self.assertEqual(insufficient["similarity_status"], "insufficient_query")


class FakeResponse:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class SimilarCasesClientTests(unittest.TestCase):
    def test_env_timeout_allows_two_minute_similar_cases_call(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "MRO_ATA_SIMILAR_CASES_ENABLED": "1",
                "MRO_ATA_SIMILAR_CASES_TIMEOUT_SECONDS": "120",
                "MRO_ATA_SIMILAR_CASES_RETRIES": "0",
            },
        ):
            config = SimilarCasesClientConfig.from_env()

        self.assertEqual(config.timeout_seconds, 120.0)

    def test_disabled_integration_makes_no_http_call(self) -> None:
        client = SimilarCasesClient(SimilarCasesClientConfig(enabled=False, url="http://127.0.0.1:8121/api/similar-cases/search", timeout_seconds=1, retries=1))
        with patch("core.ata_impact.similar_cases_client.urllib.request.urlopen") as urlopen:
            result, trace = client.search("damage", {"affected_ata": ["ATA 53"]})

        urlopen.assert_not_called()
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(trace["result"], "disabled")

    def test_4xx_does_not_retry(self) -> None:
        client = SimilarCasesClient(SimilarCasesClientConfig(enabled=True, url="http://127.0.0.1:8121/api/similar-cases/search", timeout_seconds=1, retries=1))
        error = urllib.error.HTTPError(client.config.url, 400, "bad", {}, None)
        with patch("core.ata_impact.similar_cases_client.urllib.request.urlopen", side_effect=error) as urlopen:
            result, trace = client.search("damage", {"affected_ata": ["ATA 53"]})

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(trace["http_status"], 400)

    def test_timeout_or_5xx_does_not_break_ata_result(self) -> None:
        client = SimilarCasesClient(SimilarCasesClientConfig(enabled=True, url="http://127.0.0.1:8121/api/similar-cases/search", timeout_seconds=1, retries=1))
        error = urllib.error.HTTPError(client.config.url, 503, "down", {}, None)
        ata = {"answer": "ATA result", "affected_ata": ["ATA 53"], "certificate_assessment": {"status": "covered"}}
        with patch("core.ata_impact.similar_cases_client.urllib.request.urlopen", side_effect=error):
            combined = attach_similar_cases(ata, "A320 frame crack FR27", client=client)

        self.assertEqual(combined["ata_impact"]["affected_ata"], ["ATA 53"])
        self.assertEqual(combined["ata_impact"]["certificate_assessment"], {"status": "covered"})
        self.assertEqual(combined["similar_cases"]["status"], "unavailable")

    def test_successful_client_adds_trace_counts(self) -> None:
        payload = {
            "status": "ok",
            "similarity_status": "qualified_matches_found",
            "threshold_version": "similarity-gate-v1",
            "accepted": [{"case_id": "A1"}],
            "not_accepted": [{"case_id": "R1"}],
            "intermediate": [{"case_id": "H1"}],
            "warnings": [],
        }
        client = SimilarCasesClient(SimilarCasesClientConfig(enabled=True, url="http://127.0.0.1:8121/api/similar-cases/search", timeout_seconds=1, retries=0))
        with patch("core.ata_impact.similar_cases_client.urllib.request.urlopen", return_value=FakeResponse(payload)):
            result, trace = client.search("A320 frame crack FR27", {"affected_ata": ["ATA 53"]})

        self.assertEqual(result["similarity_status"], "qualified_matches_found")
        self.assertEqual(trace["accepted_count"], 1)
        self.assertEqual(trace["not_accepted_count"], 1)
        self.assertEqual(trace["intermediate_count"], 1)
        self.assertEqual(trace["qualified_count"], 3)
        self.assertEqual(trace["threshold_version"], "similarity-gate-v1")

    def test_successful_similar_cases_do_not_change_ata_candidates_or_certificate(self) -> None:
        payload = {
            "status": "ok",
            "similarity_status": "qualified_matches_found",
            "threshold_version": "similarity-gate-v1",
            "accepted": [{"case_id": "A1", "status_normalized": "accepted", "similarity_confidence": "high", "reasons": ["same identifier"]}],
            "not_accepted": [{"case_id": "R1", "status_normalized": "cancelled", "similarity_confidence": "medium", "reasons": ["same component"]}],
            "intermediate": [{"case_id": "H1", "status_normalized": "on_hold", "similarity_confidence": "medium", "reasons": ["same query"]}],
            "warnings": [],
        }
        client = SimilarCasesClient(SimilarCasesClientConfig(enabled=True, url="http://127.0.0.1:8121/api/similar-cases/search", timeout_seconds=1, retries=0))
        ata = {
            "answer": "ATA result",
            "affected_ata": ["ATA 53"],
            "potentially_affected_ata": ["ATA 57"],
            "confirmed_affected_ata": [],
            "certificate_assessment": {"status": "partially_covered"},
        }

        with patch("core.ata_impact.similar_cases_client.urllib.request.urlopen", return_value=FakeResponse(payload)):
            combined = attach_similar_cases(ata, "A320 frame crack FR27", client=client)

        self.assertEqual(combined["ata_impact"]["affected_ata"], ["ATA 53"])
        self.assertEqual(combined["ata_impact"]["potentially_affected_ata"], ["ATA 57"])
        self.assertEqual(combined["ata_impact"]["confirmed_affected_ata"], [])
        self.assertEqual(combined["ata_impact"]["certificate_assessment"], {"status": "partially_covered"})
        self.assertIn("Похожие заявки с промежуточным статусом", combined["answer"])
        self.assertIn("H1", combined["answer"])
        self.assertIn("Исторический статус похожей заявки", combined["answer"])

    def test_ata_server_does_not_import_commercial_offer_implementation(self) -> None:
        with open("apps/api/ata_server.py", encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("CommercialOffersService", text)


if __name__ == "__main__":
    unittest.main()
