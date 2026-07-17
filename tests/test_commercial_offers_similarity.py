from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from core.commercial_offers import CommercialOffersService
from core.config import WORKSPACE_ROOT
from tools.evaluate_com_offers_ground_truth import build_ground_truth


GROUND_TRUTH = [
    ("АМОС ДЛГ Шпангоута 35.3", {"MP-0861"}),
    ("Анализ ПКМ для стоек шасси А320", {"MP-0135"}),
    ("Вмятины верхних хвостовиков предкрылков", {"MP-0819"}),
    ("Возможность выполнения технического перегона", {"MP-0738"}),
    ("Временная установка макетных стоек шасси", {"MP-0128"}),
    ("Временное разрешение на эксплуатацию с трещиной", {"MP-0842", "MP-0842.01"}),
    (
        "Вывешивание ВС для проведения ремонтных работ",
        {"MP-0184", "MP-0197", "MP-0209", "MP-0215", "MP-0225", "MP-0239", "MP-0330", "MP-0523", "MP-0618"},
    ),
    ("Замена огнетушителей", {"MP-0172.2"}),
    ("Затяжка шпилек крепления киля к фюзеляжу", {"MP-0079", "MP-0079.1", "MP-0079.2", "MP-0339"}),
    ("Изменение ограничения выполнения директивы летной годности", {"MP-0764"}),
]


def first_relevant_rank(ids: list[str], relevant: set[str]) -> int | None:
    for idx, case_id in enumerate(ids, start=1):
        if case_id in relevant:
            return idx
    return None


def ndcg_at(ids: list[str], relevant: set[str], k: int) -> float:
    dcg = sum((1.0 if case_id in relevant else 0.0) / math.log2(idx + 2) for idx, case_id in enumerate(ids[:k]))
    ideal = sum(1.0 / math.log2(idx + 2) for idx in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


class CommercialOffersSimilarityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = CommercialOffersService()
        cls.service.rebuild_converted_markdown_manifest()
        cls.service._llm = None
        cls.service._vectors = {}

    def test_converted_markdown_enrichment_fallback_metrics(self) -> None:
        ranks: list[int | None] = []
        precision_at_5 = 0.0
        recall_at_5 = 0.0
        ndcg_5 = 0.0
        for query, relevant in GROUND_TRUTH:
            ids = [case["case_id"] for case in self.service.similar_cases(query, limit=10)["similar_cases"]]
            rank = first_relevant_rank(ids, relevant)
            ranks.append(rank)
            precision_at_5 += sum(1 for case_id in ids[:5] if case_id in relevant) / 5
            recall_at_5 += sum(1 for case_id in ids[:5] if case_id in relevant) / len(relevant)
            ndcg_5 += ndcg_at(ids, relevant, 5)

        count = len(GROUND_TRUTH)
        hit_at_5 = sum(1 for rank in ranks if rank is not None and rank <= 5) / count
        hit_at_10 = sum(1 for rank in ranks if rank is not None and rank <= 10) / count
        mrr = sum((1 / rank) if rank else 0 for rank in ranks) / count

        self.assertGreaterEqual(hit_at_5, 0.6)
        self.assertGreaterEqual(hit_at_10, 0.7)
        self.assertGreater(mrr, 0.5)
        self.assertGreater(precision_at_5 / count, 0.1)
        self.assertGreater(recall_at_5 / count, 0.35)
        self.assertGreater(ndcg_5 / count, 0.45)

    def test_unavailable_rewrite_keeps_baseline_with_warning(self) -> None:
        result = self.service.similar_cases("Замена огнетушителей", limit=3)

        self.assertNotIn("query_rewrite_unavailable", result["warnings"])
        self.assertEqual(result["similar_cases"][0]["case_id"], "MP-0172.2")

    def test_unreadable_matched_document_is_not_trusted_source(self) -> None:
        self.service._links = {
            "MP-0123": {
                "link_status": "matched",
                "document_count": "1",
                "documents": ["/tmp/MRO-123-missing.md"],
            }
        }
        self.service._documents = {}

        evidence = self.service._case_evidence("MP-0123", "test", max_docs=3)

        self.assertEqual(evidence[0]["source_type"], "unverified_document_link")
        self.assertEqual(evidence[0]["link_status"], "unreadable_document")

    def test_conflicting_nested_markdown_is_not_attached_to_parent_case(self) -> None:
        text = self.service._extra_search_texts.get("MP-0172.2", "")

        self.assertIn("огнетушителей", text.lower())
        self.assertNotIn("Oxigen_system_Stress", text)

    def test_manifest_contains_real_markdown_signals(self) -> None:
        self.assertIn("Dummy", self.service._extra_search_texts.get("MP-0128", ""))
        self.assertIn("AMOC", self.service._extra_search_texts.get("MP-0861", ""))
        self.assertIn("FR 35", self.service._extra_search_texts.get("MP-0842", ""))

    def test_similar_cases_exposes_structured_mro_decision_hints(self) -> None:
        result = self.service.similar_cases("Замена огнетушителей", limit=3)
        first = result["similar_cases"][0]

        self.assertEqual(first["case_id"], "MP-0172.2")
        self.assertIn("similarity_reason_class", first)
        self.assertGreaterEqual(first["structured_score"], 0.0)
        self.assertIn("recommended_action", first["go_no_go"])
        self.assertIn("usable_for_estimate", first["cost_readiness"])

    def test_answer_format_keeps_documents_inline_without_sources_table(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".md") as handle:
            answer = self.service._build_answer(
                "Коррозия Gear Rib5",
                [
                    {
                        "case_id": "MP-0429",
                        "similarity_reason_class": "same_identifier",
                        "score": 1.23456,
                        "rerank_score": 0.87654,
                        "customer": "АК Россия",
                        "aircraft_type": "A319",
                        "status_normalized": "accepted",
                        "request_description": "Ремонт коррозии RIB 5 LH и RIB 5 RH",
                        "reasons": [
                            "по исходному запросу: общий термин: коррозия",
                            "по исходному запросу: ключевое слово: коррозия",
                            "по исходному запросу: точный термин: RIB5",
                        ],
                        "check": ["сверить фактическую зону повреждения"],
                        "cost_readiness": {"usable_for_estimate": True, "score": 5},
                        "documents": [
                            {
                                "source_type": "commercial_offer_document",
                                "document_id": "МР-429-SDM-A319-LH-RH_Wing_RIB_5_-_corrosion",
                                "path": handle.name,
                                "link": "",
                                "link_status": "matched",
                                "quality_warning": "",
                            }
                        ],
                    }
                ],
                sources=[
                    {
                        "source_descriptor": {"case_id": "MP-0429", "document_id": "doc", "link": handle.name},
                        "snippet": "duplicate source table should not be rendered",
                    }
                ],
            )

        self.assertIn("| Заявка | Score | Статус/решение | Описание | Почему похожа | Что проверить | Оценка стоимости | Документы |", answer)
        self.assertIn("[MP-0429](http://127.0.0.1:8121/api/com-offers/registry/MP-0429)", answer)
        self.assertIn("1.235<br>R 0.877", answer)
        self.assertIn("| годится (", answer)
        self.assertIn("| есть 1 |", answer)
        self.assertIn("совпал точный идентификатор: RIB5", answer)
        self.assertIn("принята", answer)
        self.assertNotIn("### Источники", answer)
        self.assertNotIn("Предупреждения по качеству источников", answer)
        self.assertNotIn("Это поиск аналогов", answer)

    def test_registry_case_markdown_page_contains_decision_context(self) -> None:
        markdown = self.service.registry_case_markdown("MP-0481")

        self.assertIsNotNone(markdown)
        self.assertIn("# MP-0481", markdown or "")
        self.assertIn("не взяли / отменена", markdown or "")
        self.assertIn("Запрос более неактуален", markdown or "")

    def test_registry_case_html_page_renders_for_browser(self) -> None:
        html = self.service.registry_case_html("MP-0481")

        self.assertIsNotNone(html)
        self.assertIn("<!doctype html>", html or "")
        self.assertIn("<h1>MP-0481", html or "")
        self.assertIn("не взяли / отменена", html or "")

    def test_aircraft_prefixed_ground_truth_fixture_matches_workbook_order(self) -> None:
        rows = build_ground_truth(
            WORKSPACE_ROOT / "com_offers" / "tests" / "ground truth.xlsx",
            aircraft_query_fixture=WORKSPACE_ROOT / "com_offers" / "tests" / "aircraft_queries.tsv",
            prefix_aircraft=True,
        )

        self.assertEqual(len(rows), 87)
        self.assertFalse([row for row in rows if row.get("aircraft_fixture_warning")])
        gear_rib = next(row for row in rows if row["base_query"] == "Коррозия Gear Rib5")
        self.assertEqual(gear_rib["query"], "Airbus A319 Коррозия Gear Rib5")
        extinguishers = next(row for row in rows if row["base_query"] == "Замена огнетушителей")
        self.assertEqual(extinguishers["query"], "Замена огнетушителей")

    def test_aircraft_prefix_is_removed_from_retrieval_text(self) -> None:
        self.assertEqual(self.service._retrieval_query_text("Airbus A319 Коррозия Gear Rib5"), "Коррозия Gear Rib5")
        self.assertEqual(
            self.service._retrieval_query_text("Boeing-737/800 Коррозия интеркостала пола"),
            "Коррозия интеркостала пола",
        )
        self.assertEqual(
            self.service._retrieval_query_text("Airbus A320/A321 Приведение флота Airbus"),
            "Приведение флота Airbus",
        )

    def test_aircraft_check_does_not_flag_same_aircraft(self) -> None:
        checks = self.service._check_points(
            "Airbus A320 Устранение царапин",
            {"aircraft_type": "A320", "status_normalized": "accepted"},
            [],
        )

        self.assertNotIn("тип ВС отличается или требует проверки: A320", checks)

    def test_fallback_profile_extracts_mro_fields(self) -> None:
        profile = self.service._query_profile("AMOC AD 2024-1234 трещина FR 35 стойки шасси")

        self.assertEqual(profile["work_type"], "other")
        self.assertEqual(profile["defect_type"], "unknown")
        self.assertEqual(profile["components"], [])
        self.assertTrue(profile["zones"])
        self.assertLess(profile["confidence"], 0.4)

    def test_bad_llm_profile_fails_quality_gate(self) -> None:
        profile = self.service._normalize_case_profile(
            {
                "problem_summary": "...",
                "work_type": "...",
                "defect_type": "unknown",
                "search_terms_ru_en": ["converted", "pdf", "ocr", "page"],
                "confidence": 0.0,
            }
        )

        errors = self.service._profile_quality_errors(profile)

        self.assertIn("empty_or_placeholder_summary", errors)
        self.assertIn("confidence_below_0_4", errors)
        self.assertIn("not_enough_structured_signal", errors)

    def test_low_confidence_profile_without_technical_signal_fails_quality_gate(self) -> None:
        profile = self.service._normalize_case_profile(
            {
                "problem_summary": "Реконфигурация 24 to 293",
                "work_type": "modification",
                "defect_type": "other",
                "components": [],
                "zones": [],
                "identifiers": [],
                "constraints_or_risks": ["Проект отменён"],
                "search_terms_ru_en": ["модификация", "реконфигурация"],
                "confidence": 0.2,
            }
        )

        errors = self.service._profile_quality_errors(profile)

        self.assertIn("confidence_below_0_4", errors)

    def test_profile_source_text_strips_paths(self) -> None:
        cleaned = self.service._clean_profile_source_fragment("path: converted_md_pdf_ocr/000-100/МР-040/file.md\\nЗапрос: Замена печек")

        self.assertNotIn("converted_md_pdf_ocr", cleaned)
        self.assertNotIn(".md", cleaned)
        self.assertIn("Запрос", cleaned)

    def test_build_profiles_falls_back_when_llm_returns_empty_profile(self) -> None:
        class EmptyProfileLLM:
            def chat(self, *_args, **_kwargs) -> str:
                return "{}"

        with tempfile.TemporaryDirectory() as tmp:
            service = CommercialOffersService(
                case_profile_cache_path=Path(tmp) / "profiles.jsonl",
                case_profile_progress_path=Path(tmp) / "progress.json",
            )
            service._registry = [row for row in service._registry if row.get("case_id") == "MP-0002"]
            service._llm = EmptyProfileLLM()

            result = service.build_case_profiles(force=True)
            item = service._case_profiles["MP-0002"]
            profile = item["profile"]

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["failure_count"], 0)
            self.assertEqual(result["fallback_count"], 1)
            self.assertIn("деактивация", profile["problem_summary"].lower())
            self.assertIn("runtime_fallback_profile", profile["quality_warnings"])
            self.assertIn("llm_profile_failed", profile["quality_warnings"])


if __name__ == "__main__":
    unittest.main()
