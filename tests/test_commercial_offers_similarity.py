from __future__ import annotations

import math
import unittest

from core.commercial_offers import CommercialOffersService


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


if __name__ == "__main__":
    unittest.main()
