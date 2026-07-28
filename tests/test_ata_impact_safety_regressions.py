from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from core.ata_impact.certificate_validator import (
    assess_certificate,
    validate_certificate,
)
from core.ata_impact.evidence import (
    EvidenceSearchResult,
    LegacyEvidenceRetrieverAdapter,
)
from core.ata_impact.identifiers import extract_identifiers
from core.ata_impact.modes import validate_ata_runtime_mode
from core.ata_impact.models import empty_mapping
from core.ata_impact.prompts import (
    ATA_CRITIC_PROMPT,
    ATA_JSON_REPAIR_PROMPT,
)
from core.ata_impact.reference_catalog import AtaClassificationReferenceCatalog
from core.ata_impact.service import AtaImpactService
from core.ata_impact.schemas import (
    ATA_CRITIC_GENERATION_SCHEMA,
    ATA_CRITIC_SCHEMA,
    ATA_MAPPING_GENERATION_SCHEMA,
    ATA_MAPPING_SCHEMA,
    ENGINEERING_FACTS_SCHEMA,
)
from core.ata_impact.validator import validate_facts, validate_mapping
from core.go_no_go import AtaImpactAgent, CertificateCatalog, CertificateEntry
from core.runtime_clients import (
    OpenAICompatibleLLM,
    RuntimeSettings,
    StructuredLLMResponse,
)
from tests.test_ata_impact_v2 import (
    SequenceLLM,
    combined_mapping,
    confirm_actions,
    expected_candidate_id,
    facts,
)


class FixedRetriever:
    def __init__(
        self,
        documents: list[dict[str, object]],
        status: str = "completed",
        warnings: list[str] | None = None,
    ) -> None:
        self.documents = documents
        self.status = status
        self.warnings = warnings or []

    def search(self, **kwargs: object) -> EvidenceSearchResult:
        return EvidenceSearchResult(self.status, self.documents, self.warnings)


class _HttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "_HttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _valid_probe_response() -> _HttpResponse:
    return _HttpResponse(
        {
            "choices": [
                {
                    "message": {"content": '{"ok": true, "value": "probe"}'},
                    "finish_reason": "stop",
                }
            ]
        }
    )


class AtaImpactSafetyRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.certificate = CertificateCatalog()

    def test_gost_civil_reference_catalog_is_complete_and_retrievable(self) -> None:
        catalog = AtaClassificationReferenceCatalog()
        self.assertTrue(catalog.available)
        self.assertGreaterEqual(len(catalog.entries), 490)
        context = catalog.context(
            "Коррозия балки пола фюзеляжа между шпангоутами",
            {
                "structural_elements": [
                    {"id": "structure_1", "name": "балка пола фюзеляжа"}
                ]
            },
        )
        chapters = {
            item["ata"]: item["title"]
            for item in context["chapter_index"]  # type: ignore[union-attr]
        }
        self.assertIn("ATA 53", chapters)
        self.assertIn(
            "ATA 53",
            {
                item["parent_ata"]
                for item in context["relevant_definitions"]  # type: ignore[union-attr]
            },
        )
        self.assertTrue(catalog.reference_ids_for_ata("ATA 34-10"))
        self.assertTrue(catalog.reference_ids_for_ata("ATA 53"))

    def test_gost_role_policy_blocks_procedure_as_affected_object(
        self,
    ) -> None:
        catalog = AtaClassificationReferenceCatalog()
        mapping = empty_mapping()
        mapping["object_ata"] = [
            {"ata": "ATA 51", "candidate_id": "candidate:object"}
        ]
        mapping["structural_ata"] = [
            {"ata": "ATA 57", "candidate_id": "candidate:structure"}
        ]
        warnings = catalog.enforce_mapping_roles(mapping)
        self.assertEqual(mapping["object_ata"], [])
        self.assertEqual(
            [item["ata"] for item in mapping["structural_ata"]],
            ["ATA 57"],
        )
        self.assertTrue(
            any(
                warning.startswith("mapping_role_candidate_removed:")
                for warning in warnings
            )
        )
        wing = next(
            entry
            for entry in catalog.entries
            if entry.ata == "ATA 57"
            and entry.ata == entry.parent_ata
        )
        self.assertEqual(
            wing.as_dict()["allowed_mapping_categories"],
            [
                "object_ata",
                "structural_ata",
                "location_context_ata",
                "interface_ata_hypotheses",
            ],
        )

    def test_exact_cross_role_duplicate_preserves_structural_damage(self) -> None:
        payload = facts()
        payload["physical_objects"][0]["name"] = "upper panel central fuel tank"  # type: ignore[index]
        payload["structural_elements"][0]["name"] = "central fuel tank upper panel"  # type: ignore[index]
        validated, warnings = validate_facts(
            payload,
            {
                "aircraft_type_raw": None,
                "msn": None,
            },
        )
        structure = validated["structural_elements"][0]  # type: ignore[index]
        self.assertTrue(structure["damage_confirmed"])
        self.assertEqual(structure["involvement"], "damaged")
        self.assertIn(
            "structure_1",
            {
                item["affected_entity_id"]
                for item in validated["damage"]  # type: ignore[union-attr]
            },
        )
        self.assertTrue(
            any(
                warning.startswith(
                    "facts_cross_role_duplicate_reconciled:"
                )
                for warning in warnings
            )
        )

    def test_generic_central_tank_corrosion_does_not_create_ata_51_procedure(self) -> None:
        engineering_facts = facts(structure_damage=True)
        mapping = combined_mapping("ATA 25", structure_affected=True, interface_ata=None)["ata_mapping"]
        mapping["structural_ata"] = [
            {
                "ata": "ATA 57",
                "confidence": 0.93,
                "reason": "damaged central wing tank panel is actual wing structure",
                "entity_id": "structure_1",
            }
        ]
        mapping["procedure_ata_hypotheses"] = [
            {
                "ata": "ATA 51",
                "confidence": 0.7,
                "reason": "generic corrosion procedure",
                "entity_id": "structure_1",
                "source_fragment": "corrosion on upper panel central fuel tank",
            }
        ]
        validated, warnings = validate_mapping(
            mapping,
            engineering_facts,
            [],
            (
                "На ВС в ходе инспекции была обнаружена коррозия на верхней панели "
                "центрального топливного бака между балкой Y1292 и нервюрой Rib1"
            ),
        )
        self.assertEqual(validated["structural_ata"][0]["ata"], "ATA 57")
        self.assertEqual(validated["procedure_ata_hypotheses"], [])
        self.assertTrue(
            any(
                "procedure_without_factual_anchor:ATA 51" in warning
                for warning in warnings
            )
        )

    def test_missing_structural_mapping_is_filled_from_gost_shortlist(self) -> None:
        engineering_facts = facts(structure_damage=True)
        engineering_facts["physical_objects"][0]["name"] = "upper panel central fuel tank"  # type: ignore[index]
        engineering_facts["structural_elements"][0]["name"] = "upper panel central fuel tank"  # type: ignore[index]
        engineering_facts["damage"] = [  # type: ignore[index]
            {"type": "corrosion", "affected_entity_id": "structure_1"}
        ]
        mapping = combined_mapping("ATA 25", structure_affected=False, interface_ata=None)["ata_mapping"]
        mapping["object_ata"] = []
        mapping["location_context_ata"] = []

        class StructuredSequence:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def structured_chat(self, **kwargs: object) -> StructuredLLMResponse:
                payload = dict(kwargs)
                self.calls.append(payload)
                stage = str(payload["stage"])
                if stage == "engineering_fact_extraction":
                    return StructuredLLMResponse(
                        parsed=engineering_facts,
                        content="",
                        finish_reason="stop",
                        structured_output_mode="json_schema",
                        schema_enforced=True,
                    )
                if stage == "ata_mapping":
                    return StructuredLLMResponse(
                        parsed=mapping,
                        content="",
                        finish_reason="stop",
                        structured_output_mode="json_schema",
                        schema_enforced=True,
                    )
                critic_mapping = payload["input_payload"]["ata_mapping"]  # type: ignore[index]
                actions = []
                for category, items in critic_mapping.items():  # type: ignore[union-attr]
                    if category == "user_declared_ata":
                        continue
                    for item in items:
                        actions.append(
                            {
                                "candidate_id": item["candidate_id"],
                                "action": "confirm",
                                "reason": "confirmed",
                            }
                        )
                return StructuredLLMResponse(
                    parsed={"actions": actions},
                    content="",
                    finish_reason="stop",
                    structured_output_mode="json_schema",
                    schema_enforced=True,
                )

        llm = StructuredSequence()
        result = AtaImpactService(
            self.certificate,
            llm,  # type: ignore[arg-type]
            FixedRetriever([]),
        ).analyze(
            (
                "На ВС в ходе инспекции была обнаружена коррозия на верхней панели "
                "центрального топливного бака между балкой Y1292 и нервюрой Rib1"
            ),
            runtime_mode="standard",
        )
        self.assertEqual(result["affected_ata"], ["ATA 57"])
        self.assertIn(
            "reference_shortlist_candidate_added:structural_ata:structure_1:ATA 57",
            result["warnings"],
        )

    def test_damaged_object_requires_explicit_role_for_structural_mapping(
        self,
    ) -> None:
        engineering_facts = facts()
        candidate = {
            "ata": "ATA 57",
            "entity_id": "object_1",
            "technical_role": "actual_structure",
            "confidence": 0.9,
            "reason": "Reference excludes this damaged construction from its functional ATA.",
        }
        raw = empty_mapping()
        raw["structural_ata"] = [candidate]
        accepted, warnings = validate_mapping(
            raw,
            engineering_facts,
            [],
        )
        self.assertEqual(
            accepted["structural_ata"][0]["entity_id"],
            "object_1",
        )
        self.assertFalse(
            any(
                warning.startswith(
                    "structural_ata_without_involvement:"
                )
                for warning in warnings
            )
        )

        without_role = empty_mapping()
        without_role["structural_ata"] = [
            {key: value for key, value in candidate.items() if key != "technical_role"}
        ]
        rejected, warnings = validate_mapping(
            without_role,
            engineering_facts,
            [],
        )
        self.assertEqual(rejected["structural_ata"], [])
        self.assertTrue(
            any(
                warning.startswith(
                    "structural_ata_without_involvement:"
                )
                for warning in warnings
            )
        )

    def test_missing_classification_reference_prevents_affected_confirmation(
        self,
    ) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        missing_catalog = AtaClassificationReferenceCatalog(
            Path("/tmp/mro-kb-missing-gost-reference.json")
        )
        result = AtaImpactService(
            self.certificate,
            SequenceLLM(facts(), mapping, confirm_actions(mapping)),
            FixedRetriever([]),
            reference_catalog=missing_catalog,
        ).analyze("A320 damaged cargo equipment")
        self.assertEqual(result["affected_ata"], [])
        self.assertEqual(result["decision"], "engineering_review_required")
        self.assertEqual(
            result["classification_reference"]["status"],  # type: ignore[index]
            "unavailable",
        )
        self.assertTrue(
            any(
                str(item).startswith("classification_reference_")
                for item in result["warnings"]
            )
        )

    def run_pipeline(
        self,
        mapping: dict[str, list[dict[str, object]]],
        critic: object,
        *,
        engineering_facts: dict[str, object] | None = None,
        retriever: object | None = None,
        mode: str = "standard",
    ) -> tuple[dict[str, object], SequenceLLM]:
        llm = SequenceLLM(engineering_facts or facts(), mapping, critic)
        result = AtaImpactService(self.certificate, llm, retriever).analyze(
            "A320 equipment corrosion near structure",
            runtime_mode=mode,
        )
        return result, llm

    def test_stage_schemas_are_sent_with_separate_system_and_user_roles(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]

        class StructuredSequence:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.responses = [
                    facts(structure_damage=True),
                    mapping,
                    confirm_actions(mapping),
                ]

            def structured_chat(self, **kwargs: object) -> StructuredLLMResponse:
                self.calls.append(dict(kwargs))
                return StructuredLLMResponse(
                    parsed=self.responses.pop(0),
                    content="",
                    finish_reason="stop",
                    structured_output_mode="json_schema",
                    schema_enforced=True,
                )

        llm = StructuredSequence()
        result = AtaImpactService(
            self.certificate,
            llm,  # type: ignore[arg-type]
            FixedRetriever([]),
        ).analyze("A320 damaged cargo equipment", runtime_mode="standard")
        self.assertEqual(
            [call["stage"] for call in llm.calls],
            ["engineering_fact_extraction", "ata_mapping", "independent_critic"],
        )
        self.assertEqual(
            [call["response_schema"] for call in llm.calls],
            [
                ENGINEERING_FACTS_SCHEMA,
                ATA_MAPPING_GENERATION_SCHEMA,
                ATA_CRITIC_GENERATION_SCHEMA,
            ],
        )
        self.assertNotIn("certificate_catalog", llm.calls[1]["input_payload"])
        self.assertNotIn("candidate_id", mapping["object_ata"][0])
        critic_mapping = llm.calls[2]["input_payload"]["ata_mapping"]  # type: ignore[index]
        self.assertNotIn(
            "certificate_catalog_reference",
            llm.calls[2]["input_payload"],  # type: ignore[operator]
        )
        self.assertTrue(llm.calls[1]["input_payload"]["ata_reference"])  # type: ignore[index]
        self.assertTrue(llm.calls[2]["input_payload"]["ata_reference"])  # type: ignore[index]
        self.assertIn("candidate_id", critic_mapping["object_ata"][0])  # type: ignore[index]
        self.assertTrue(
            critic_mapping["object_ata"][0]["classification_reference_ids"]  # type: ignore[index]
        )
        required_ids = llm.calls[2]["input_payload"]["required_candidate_ids"]  # type: ignore[index]
        self.assertEqual(
            len(required_ids),  # type: ignore[arg-type]
            llm.calls[2]["input_payload"]["required_candidate_count"],  # type: ignore[index]
        )
        self.assertEqual(
            set(required_ids),  # type: ignore[arg-type]
            {
                item["candidate_id"]
                for category, items in critic_mapping.items()  # type: ignore[union-attr]
                if category != "user_declared_ata"
                for item in items
            },
        )
        for step in result["agent_trace"][1:]:
            if step["step"] in {
                "engineering_fact_extraction",
                "ata_mapping",
                "independent_critic",
            }:
                self.assertEqual(step["structured_output_mode"], "json_schema")
                self.assertTrue(step["schema_enforced_by_server"])

    def test_structured_repair_uses_json_repair_stage_and_business_schema(self) -> None:
        invalid = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        invalid["object_ata"][0]["unexpected"] = "invalid"
        repaired = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]

        class StructuredRepairSequence:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.responses = [
                    facts(),
                    invalid,
                    repaired,
                    confirm_actions(repaired),
                ]

            def structured_chat(self, **kwargs: object) -> StructuredLLMResponse:
                self.calls.append(dict(kwargs))
                return StructuredLLMResponse(
                    parsed=self.responses.pop(0),
                    content="",
                    finish_reason="stop",
                    structured_output_mode="json_schema",
                    schema_enforced=True,
                )

        llm = StructuredRepairSequence()
        result = AtaImpactService(
            self.certificate,
            llm,  # type: ignore[arg-type]
            FixedRetriever([]),
        ).analyze("A320 damaged cargo equipment")
        self.assertEqual(
            [call["stage"] for call in llm.calls],
            [
                "engineering_fact_extraction",
                "ata_mapping",
                "json_repair",
                "independent_critic",
            ],
        )
        self.assertEqual(
            llm.calls[2]["response_schema"],
            ATA_MAPPING_GENERATION_SCHEMA,
        )
        mapping_trace = next(
            item
            for item in result["agent_trace"]
            if item["step"] == "ata_mapping"
        )
        self.assertEqual(mapping_trace["repair"], "completed")

    def test_openai_transport_profiles_and_generic_chat_isolation(self) -> None:
        captured: list[dict[str, object]] = []

        def fake_urlopen(request: object, **kwargs: object) -> _HttpResponse:
            captured.append(json.loads(request.data.decode("utf-8")))  # type: ignore[attr-defined]
            return _HttpResponse(
                {
                    "choices": [
                        {
                            "message": {"content": json.dumps(empty_mapping())},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )

        for mode, strict_expected in (
            ("json_schema", True),
            ("json_schema_no_strict", False),
        ):
            with self.subTest(mode=mode), patch(
                "urllib.request.urlopen",
                side_effect=fake_urlopen,
            ):
                client = OpenAICompatibleLLM(
                    RuntimeSettings(
                        llm_model="test-model",
                        llm_base_url="http://test/v1",
                        ata_structured_output_mode=mode,
                    )
                )
                response = client.structured_chat(
                    stage="ata_mapping",
                    system_prompt="system rules",
                    input_payload={"request": "damage"},
                    response_schema=ATA_MAPPING_SCHEMA,
                )
                self.assertIsNone(response.error)
                payload = captured[-1]
                self.assertEqual(
                    [message["role"] for message in payload["messages"]],  # type: ignore[index]
                    ["system", "user"],
                )
                response_format = payload["response_format"]  # type: ignore[index]
                schema_contract = response_format["json_schema"]  # type: ignore[index]
                self.assertEqual(
                    schema_contract.get("strict"),
                    True if strict_expected else None,
                )
                self.assertTrue(response.server_profile_accepted)
                self.assertTrue(response.local_schema_valid)
                self.assertEqual(
                    response.schema_enforcement_requested,
                    True,
                )
                self.assertFalse(response.schema_enforced)
                if strict_expected:
                    transmitted = schema_contract["schema"]
                    self.assertEqual(
                        set(transmitted["required"]),  # type: ignore[index]
                        set(transmitted["properties"]),  # type: ignore[index]
                    )
                    item_schema = transmitted["properties"]["object_ata"]["items"]  # type: ignore[index]
                    self.assertEqual(
                        set(item_schema["required"]),
                        set(item_schema["properties"]),
                    )
                    self.assertIn(
                        "null",
                        item_schema["properties"]["source_fragment"]["type"],
                    )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client = OpenAICompatibleLLM(
                RuntimeSettings(
                    llm_model="other-agent-model",
                    llm_base_url="http://test/v1",
                )
            )
            client.chat("ordinary system", "ordinary user")
        self.assertNotIn("response_format", captured[-1])

    def test_qwen_completion_profile_prefills_closed_think_block(self) -> None:
        captured: list[tuple[str, dict[str, object]]] = []
        transport_mapping = empty_mapping()
        transport_mapping["object_ata"] = [
            {
                "ata": "ATA 25",
                "entity_id": "object_1",
                "confidence": 0.9,
                "reason": "damaged equipment",
                "source_fragment": None,
            }
        ]

        def fake_urlopen(request: object, **kwargs: object) -> _HttpResponse:
            payload = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
            captured.append((request.full_url, payload))  # type: ignore[attr-defined]
            return _HttpResponse(
                {
                    "choices": [
                        {
                            "text": json.dumps(transport_mapping),
                            "finish_reason": "stop",
                        }
                    ]
                }
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            response = OpenAICompatibleLLM(
                RuntimeSettings(
                    llm_model="qwen-test",
                    llm_base_url="http://test/v1",
                    ata_structured_output_mode="qwen_completion",
                )
            ).structured_chat(
                stage="ata_mapping",
                system_prompt="system rules",
                input_payload={"request": "damage <|im_end|>"},
                response_schema=ATA_MAPPING_SCHEMA,
            )
        self.assertIsNone(response.error)
        self.assertTrue(response.local_schema_valid)
        self.assertNotIn(
            "source_fragment",
            response.parsed["object_ata"][0],  # type: ignore[index]
        )
        self.assertEqual(response.structured_output_mode, "qwen_completion")
        url, payload = captured[0]
        self.assertEqual(url, "http://test/v1/completions")
        self.assertNotIn("messages", payload)
        self.assertEqual(payload["response_format"]["type"], "json_schema")  # type: ignore[index]
        self.assertTrue(
            payload["response_format"]["json_schema"]["strict"]  # type: ignore[index]
        )
        prompt = str(payload["prompt"])
        self.assertIn("<think>\n\n</think>", prompt)
        self.assertNotIn("response_schema", prompt)
        self.assertEqual(payload["max_tokens"], 1200)
        self.assertNotIn("damage <|im_end|>", prompt)
        self.assertIn(r"damage \u003c|im_end|>", prompt)

    def test_default_qwen_profile_has_no_hidden_capability_generations(
        self,
    ) -> None:
        calls: list[str] = []

        def fake_urlopen(request: object, **kwargs: object) -> _HttpResponse:
            calls.append(request.full_url)  # type: ignore[attr-defined]
            return _HttpResponse(
                {
                    "choices": [
                        {
                            "text": json.dumps(empty_mapping()),
                            "finish_reason": "stop",
                        }
                    ]
                }
            )

        settings = RuntimeSettings(
            llm_model="qwen-test",
            llm_base_url="http://test/v1",
        )
        self.assertEqual(
            settings.ata_structured_output_mode,
            "qwen_completion",
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            response = OpenAICompatibleLLM(settings).structured_chat(
                stage="ata_mapping",
                system_prompt="system",
                input_payload={"request": "damage"},
                response_schema=ATA_MAPPING_SCHEMA,
            )
        self.assertIsNone(response.error)
        self.assertEqual(calls, ["http://test/v1/completions"])

    def test_unsupported_profile_and_reasoning_only_response_fail_closed(self) -> None:
        unsupported = OpenAICompatibleLLM(
            RuntimeSettings(
                llm_model="test-model",
                ata_structured_output_mode="not-supported",
            )
        )
        with patch("urllib.request.urlopen") as urlopen:
            response = unsupported.structured_chat(
                stage="ata_mapping",
                system_prompt="system",
                input_payload={},
                response_schema=ATA_MAPPING_SCHEMA,
            )
        self.assertEqual(
            response.error,
            "unsupported_structured_output_mode:not-supported",
        )
        urlopen.assert_not_called()

        reasoning_only = _HttpResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": '{"object_ata":[]}',
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        with patch("urllib.request.urlopen", return_value=reasoning_only):
            response = OpenAICompatibleLLM(
                RuntimeSettings(
                    llm_model="test-model",
                    llm_base_url="http://test/v1",
                    ata_structured_output_mode="json_schema",
                )
            ).structured_chat(
                stage="ata_mapping",
                system_prompt="system",
                input_payload={},
                response_schema=ATA_MAPPING_SCHEMA,
            )
        self.assertEqual(response.error, "reasoning_content_without_content")
        self.assertEqual(response.content, "")

    def test_auto_probe_selects_cached_qwen_completion_after_reasoning_only_schema(self) -> None:
        captured: list[dict[str, object]] = []
        business_mapping = json.dumps(empty_mapping())

        def fake_urlopen(request: object, **kwargs: object) -> _HttpResponse:
            payload = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
            captured.append(payload)
            response_format = payload.get("response_format")
            if "prompt" in payload:
                return _HttpResponse(
                    {
                        "choices": [
                            {
                                "text": (
                                    business_mapping
                                    if "system rules" in str(payload["prompt"])
                                    else '{"ok": true, "value": "probe"}'
                                ),
                                "finish_reason": "stop",
                            }
                        ]
                    }
                )
            if response_format and response_format.get("type") == "json_schema":
                return _HttpResponse(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "",
                                    "reasoning_content": '{"ok": true, "value": "probe"}',
                                },
                                "finish_reason": "stop",
                            }
                        ]
                    }
                )
            if response_format and response_format.get("type") == "json_object":
                raise urllib.error.HTTPError(
                    "http://test/v1/chat/completions",
                    400,
                    "unsupported",
                    {},
                    None,
                )
            if payload["messages"][0]["content"] == "system rules":
                return _HttpResponse(
                    {
                        "choices": [
                            {
                                "message": {"content": business_mapping},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                )
            return _valid_probe_response()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client = OpenAICompatibleLLM(
                RuntimeSettings(
                    llm_model="test-model",
                    llm_base_url="http://test/v1",
                    ata_structured_output_mode="auto",
                )
            )
            first = client.structured_chat(
                stage="ata_mapping",
                system_prompt="system rules",
                input_payload={"request": "damage"},
                response_schema=ATA_MAPPING_SCHEMA,
            )
            second = client.structured_chat(
                stage="ata_mapping",
                system_prompt="system rules",
                input_payload={"request": "damage"},
                response_schema=ATA_MAPPING_SCHEMA,
            )
        self.assertIsNone(first.error)
        self.assertIsNone(second.error)
        self.assertEqual(first.structured_output_mode, "qwen_completion")
        self.assertEqual(second.structured_output_mode, "qwen_completion")
        self.assertEqual(first.requested_structured_output_mode, "auto")
        self.assertTrue(first.schema_enforcement_requested)
        self.assertTrue(first.server_profile_accepted)
        self.assertTrue(first.local_schema_valid)
        probe_calls = [
            payload
            for payload in captured
            if (
                "system rules" not in str(payload.get("prompt") or "")
                and (
                    "messages" not in payload
                    or payload["messages"][0]["content"] != "system rules"  # type: ignore[index]
                )
            )
        ]
        self.assertEqual(len(probe_calls), 4)

    def test_parsed_json_success_and_trace_error_fields_are_explicit(self) -> None:
        parsed_response = _HttpResponse(
            {
                "choices": [
                    {
                        "message": {"parsed": empty_mapping(), "content": ""},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        with patch("urllib.request.urlopen", return_value=parsed_response):
            response = OpenAICompatibleLLM(
                RuntimeSettings(
                    llm_model="test-model",
                    llm_base_url="http://test/v1",
                    ata_structured_output_mode="json_schema",
                )
            ).structured_chat(
                stage="ata_mapping",
                system_prompt="system",
                input_payload={},
                response_schema=ATA_MAPPING_SCHEMA,
            )
        self.assertIsNone(response.error)
        self.assertTrue(response.local_schema_valid)
        self.assertEqual(response.parsed, empty_mapping())

        class BrokenThenBrokenRepair:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def structured_chat(self, **kwargs: object) -> StructuredLLMResponse:
                self.calls.append(str(kwargs["stage"]))
                if kwargs["stage"] == "engineering_fact_extraction":
                    return StructuredLLMResponse(
                        parsed=None,
                        content="",
                        finish_reason="stop",
                        structured_output_mode="prompt_only",
                        schema_enforced=False,
                        server_profile_accepted=True,
                        error="reasoning_content_without_content",
                        primary_error="reasoning_content_without_content",
                    )
                return StructuredLLMResponse(
                    parsed=None,
                    content="",
                    finish_reason="stop",
                    structured_output_mode="prompt_only",
                    schema_enforced=False,
                    repair_attempted=False,
                    server_profile_accepted=True,
                    error="empty_content",
                    primary_error="empty_content",
                )

        result = AtaImpactService(
            self.certificate,
            BrokenThenBrokenRepair(),  # type: ignore[arg-type]
        ).analyze("A320 unclear request")
        step = result["agent_trace"][1]
        self.assertEqual(step["primary_error"], "reasoning_content_without_content")
        self.assertEqual(step["repair_error"], "empty_content")
        self.assertEqual(step["repair"], "repair_failed")

    def test_unknown_event_null_fields_are_schema_valid_without_repair(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        unknown_facts = facts()
        unknown_facts["event"] = {
            "type": None,
            "maintenance_action": None,
            "target_entity_ids": [],
        }

        class StructuredSequence:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.responses = [
                    unknown_facts,
                    mapping,
                    confirm_actions(mapping),
                ]

            def structured_chat(self, **kwargs: object) -> StructuredLLMResponse:
                self.calls.append(str(kwargs["stage"]))
                return StructuredLLMResponse(
                    parsed=self.responses.pop(0),
                    content="",
                    finish_reason="stop",
                    structured_output_mode="prompt_only",
                    schema_enforced=False,
                    server_profile_accepted=True,
                    local_schema_valid=True,
                )

        llm = StructuredSequence()
        result = AtaImpactService(
            self.certificate,
            llm,  # type: ignore[arg-type]
            FixedRetriever([]),
        ).analyze("A320 item mentioned near cargo bay", runtime_mode="standard")
        self.assertNotIn("json_repair", llm.calls)
        self.assertIsNone(result["engineering_facts"]["event"]["type"])
        self.assertIsNone(result["engineering_facts"]["event"]["maintenance_action"])

    def test_standard_and_auto_always_use_independent_critic(self) -> None:
        for mode in ("standard", "auto"):
            mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
            result, llm = self.run_pipeline(mapping, confirm_actions(mapping), mode=mode)
            self.assertGreaterEqual(len(llm.calls), 3)
            self.assertEqual(llm.calls[2][0], ATA_CRITIC_PROMPT)
            self.assertFalse(any("self-audit the mapping" in call[0] for call in llm.calls))
            stages = [item["step"] for item in result["agent_trace"]]
            self.assertLess(
                stages.index("engineering_fact_extraction"),
                stages.index("ata_classification_reference_retrieval"),
            )
            self.assertLess(
                stages.index("ata_classification_reference_retrieval"),
                stages.index("ata_mapping"),
            )
            self.assertLess(
                stages.index("ata_mapping"),
                stages.index("certificate_scope_validation"),
            )
            self.assertLess(
                stages.index("certificate_scope_validation"),
                stages.index("independent_critic"),
            )

    def test_missing_empty_duplicate_unknown_and_mismatched_actions_fail_closed(self) -> None:
        cases: list[dict[str, object]] = [
            {"actions": []},
            {"actions": [
                {
                    "candidate_id": "candidate:object_ata:object_1:ATA_25:1",
                    "action": "confirm",
                    "reason": "one",
                },
                {
                    "candidate_id": "candidate:object_ata:object_1:ATA_25:1",
                    "action": "confirm",
                    "reason": "duplicate",
                },
            ]},
            {"actions": [
                {
                    "candidate_id": "candidate:unknown:object_1:ATA_25:1",
                    "action": "confirm",
                    "reason": "unknown target",
                }
            ]},
            {"actions": [
                {
                    "candidate_id": "candidate:object_ata:object_1:ATA_25:1",
                    "action": "confirm",
                    "ata": "ATA 53",
                    "category": "structural_ata",
                    "reason": "mutated identity",
                }
            ]},
        ]
        for critic in cases:
            with self.subTest(critic=critic):
                mapping = combined_mapping("ATA 25", context_ata="ATA 25", interface_ata=None)["ata_mapping"]
                mapping["location_context_ata"] = []
                result, _ = self.run_pipeline(mapping, critic)
                self.assertEqual(result["affected_ata"], [])
                self.assertTrue(result["validated_ata"]["candidate_unverified"])
                self.assertEqual(result["decision"], "engineering_review_required")

    def test_critic_unavailable_or_invalid_never_creates_affected(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        llm = SequenceLLM(facts(), mapping, RuntimeError("offline"))
        result = AtaImpactService(self.certificate, llm).analyze("A320 equipment damage")
        self.assertEqual(result["affected_ata"], [])
        self.assertTrue(result["validated_ata"]["candidate_unverified"])

    def test_same_ata_candidates_keep_unique_identity_and_exact_coverage(self) -> None:
        engineering_facts = facts()
        engineering_facts["physical_objects"].append(  # type: ignore[union-attr]
            {
                "id": "object_2",
                "name": "second damaged item",
                "involvement": "damaged",
                "damage_confirmed": True,
                "confidence": 0.8,
            }
        )
        engineering_facts["damage"].append(  # type: ignore[union-attr]
            {"type": "corrosion", "affected_entity_id": "object_2"}
        )
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        mapping["object_ata"].extend(
            [
                {
                    "ata": "ATA 25",
                    "entity_id": "object_2",
                    "confidence": 0.8,
                    "reason": "second entity",
                },
                {
                    "ata": "ATA 25",
                    "entity_id": "object_1",
                    "confidence": 0.7,
                    "reason": "independent inspection basis",
                },
            ]
        )
        actions = confirm_actions(mapping)
        result, _ = self.run_pipeline(mapping, actions, engineering_facts=engineering_facts)
        candidates = result["ata_mapping"]["object_ata"]
        self.assertEqual(len(candidates), 3)
        self.assertEqual(len({item["candidate_id"] for item in candidates}), 3)
        self.assertEqual(len(result["validated_ata"]["inferred_from_request"]), 3)

    def test_critic_addition_cannot_reuse_existing_candidate_id(self) -> None:
        engineering_facts = facts(structure_damage=True)
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        object_candidate_id = expected_candidate_id(mapping, "object_ata")
        actions = confirm_actions(mapping)
        actions["actions"].append(
            {
                "candidate_id": object_candidate_id,
                "action": "add_missing_candidate",
                "ata": "ATA 53",
                "category": "object_ata",
                "entity_id": "object_1",
                "confidence": 0.9,
                "reason": "collision attempt",
            }
        )
        result, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=engineering_facts,
        )
        self.assertNotIn("ATA 53", result["affected_ata"])
        self.assertTrue(
            any(
                "critic_addition_candidate_id_collision" in warning
                for warning in result["warnings"]
            )
        )

    def test_extra_unknown_critic_action_invalidates_coverage_globally(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        actions = confirm_actions(mapping)
        actions["actions"].append(
            {
                "candidate_id": "candidate:unknown",
                "action": "confirm",
                "reason": "extra unknown action",
            }
        )
        result, _ = self.run_pipeline(mapping, actions)
        self.assertEqual(result["affected_ata"], [])
        self.assertTrue(result["validated_ata"]["candidate_unverified"])

    def test_malformed_and_valid_duplicate_actions_fail_coverage(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        candidate_id = expected_candidate_id(mapping, "object_ata")
        actions = {
            "actions": [
                {
                    "candidate_id": candidate_id,
                    "action": "reject",
                },
                {
                    "candidate_id": candidate_id,
                    "action": "confirm",
                    "reason": "valid-looking duplicate",
                },
            ]
        }
        result, _ = self.run_pipeline(mapping, actions)
        self.assertEqual(result["affected_ata"], [])
        self.assertTrue(result["validated_ata"]["candidate_unverified"])

    def test_declared_candidate_id_is_allocated_without_global_collision(self) -> None:
        engineering_facts = facts()
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        validated, _ = validate_mapping(
            mapping,
            engineering_facts,
            ["ATA 34"],
            "ATA 34",
        )
        ids = [
            str(item["candidate_id"])
            for items in validated.values()
            for item in items
        ]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            validated["user_declared_ata"][0]["initial_state"],
            "candidate_unverified",
        )

    def test_require_document_is_potential_until_exact_controlled_transition(self) -> None:
        for category in ("object_ata", "structural_ata"):
            with self.subTest(category=category):
                engineering_facts = facts(structure_damage=category == "structural_ata")
                mapping = combined_mapping(
                    "ATA 25",
                    structure_affected=category == "structural_ata",
                    interface_ata=None,
                )["ata_mapping"]
                mapping["location_context_ata"] = []
                actions = confirm_actions(mapping)
                target = mapping[category][0]
                target_id = expected_candidate_id(mapping, category)
                for action in actions["actions"]:
                    if action["candidate_id"] == target_id:
                        action["action"] = "require_document"
                result, _ = self.run_pipeline(
                    mapping,
                    actions,
                    engineering_facts=engineering_facts,
                    retriever=FixedRetriever([]),
                )
                self.assertNotIn(target["ata"], result["affected_ata"])
                self.assertIn(target["ata"], result["potentially_affected_ata"])
                self.assertEqual(
                    result["validated_ata"]["document_verification_required"][0]["candidate_id"],
                    target_id,
                )

    def test_document_transition_rejects_wrong_nonapplicable_and_obsolete_records(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        actions = confirm_actions(mapping)
        target = mapping["object_ata"][0]
        target_id = expected_candidate_id(mapping, "object_ata")
        actions["actions"][0]["action"] = "require_document"  # type: ignore[index]
        base = {
            "document_id": "amm-25",
            "document_type": "AMM",
            "revision": "42",
            "effectivity": "A320 all",
            "section_reference": "25-50-00",
            "trust_level": "controlled_oem",
            "applicable": True,
            "current_revision": True,
            "verification_status": "confirmed",
            "confirmed_candidates": [
                {
                    "candidate_id": target_id,
                    "ata": target["ata"],
                    "category": "object_ata",
                    "entity_id": "object_1",
                    "verification_status": "confirmed",
                    "confirmed_claim": "the candidate object ATA is applicable",
                }
            ],
        }
        bad_documents = [
            {**base, "applicable": False},
            {**base, "current_revision": False},
            {
                **base,
                "document_id": " ",
                "document_type": " ",
                "revision": " ",
                "effectivity": " ",
                "section_reference": " ",
            },
            {
                **base,
                "confirmed_candidates": [
                    {
                        **base["confirmed_candidates"][0],  # type: ignore[index]
                        "candidate_id": "candidate:wrong",
                    }
                ],
            },
        ]
        for document in bad_documents:
            result, _ = self.run_pipeline(
                mapping,
                actions,
                retriever=FixedRetriever([document]),
            )
            self.assertEqual(result["validated_ata"]["document_confirmed"], [])
            self.assertTrue(result["validated_ata"]["document_verification_required"])
        result, _ = self.run_pipeline(
            mapping,
            actions,
            retriever=FixedRetriever([base]),
        )
        self.assertEqual(result["validated_ata"]["document_verification_required"], [])
        self.assertEqual(
            result["validated_ata"]["document_confirmed"][0]["previous_status"],
            "document_verification_required",
        )
        self.assertEqual(result["controlled_evidence"][0]["document_id"], "amm-25")

    def test_possible_interface_document_confirmation_is_exact_transition(self) -> None:
        mapping = combined_mapping("ATA 25")["ata_mapping"]
        mapping["object_ata"] = []
        mapping["location_context_ata"] = []
        target = mapping["interface_ata_hypotheses"][0]
        target_id = expected_candidate_id(mapping, "interface_ata_hypotheses")
        actions = confirm_actions(mapping)
        actions["actions"][0]["action"] = "require_document"  # type: ignore[index]
        document = {
            "document_id": "amm-interface",
            "document_type": "AMM",
            "revision": "7",
            "effectivity": "A320",
            "section_reference": "53-00",
            "trust_level": "approved_data",
            "applicable": True,
            "current_revision": True,
            "verification_status": "confirmed",
            "confirmed_candidates": [
                {
                    "candidate_id": target_id,
                    "ata": target["ata"],
                    "category": "interface_ata_hypotheses",
                    "relation_id": "relation_1",
                    "verification_status": "confirmed",
                    "confirmed_claim": "the attachment interface is affected",
                }
            ],
        }
        result, _ = self.run_pipeline(
            mapping,
            actions,
            retriever=FixedRetriever([document]),
        )
        self.assertEqual(result["validated_ata"]["document_verification_required"], [])
        self.assertEqual(
            result["validated_ata"]["document_confirmed"][0]["candidate_id"],
            target_id,
        )
        self.assertIn("ATA 53", result["affected_ata"])

    def test_require_document_cannot_mutate_candidate_anchor(self) -> None:
        engineering_facts = facts()
        engineering_facts["relations"].append(  # type: ignore[union-attr]
            {
                "id": "relation_2",
                "source_entity_id": "object_1",
                "target_entity_id": "structure_1",
                "relation": "attached_to",
                "evidence_type": "explicit",
                "confidence": 0.9,
            }
        )
        mapping = combined_mapping("ATA 25")["ata_mapping"]
        target = mapping["interface_ata_hypotheses"][0]
        target_id = expected_candidate_id(mapping, "interface_ata_hypotheses")
        actions = confirm_actions(mapping)
        for action in actions["actions"]:
            if action["candidate_id"] == target_id:
                action.update(
                    {
                        "action": "require_document",
                        "relation_id": "relation_2",
                    }
                )
        result, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=engineering_facts,
        )
        self.assertNotIn("ATA 53", result["affected_ata"])
        self.assertTrue(
            any(
                warning.startswith("critic_candidate_mismatch:")
                for warning in result["warnings"]
            )
        )

    def test_critic_cannot_add_relation_anchor_to_confirmed_object(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        actions = confirm_actions(mapping)
        actions["actions"][0]["relation_id"] = "relation_1"  # type: ignore[index]
        result, _ = self.run_pipeline(mapping, actions)
        self.assertEqual(result["affected_ata"], [])
        self.assertTrue(result["validated_ata"]["candidate_unverified"])
        self.assertTrue(
            any(
                warning.startswith("critic_candidate_mismatch:")
                for warning in result["warnings"]
            )
        )

    def test_controlled_evidence_requires_record_identity_consistency(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        target = mapping["object_ata"][0]
        target_id = expected_candidate_id(mapping, "object_ata")
        actions = confirm_actions(mapping)
        record = {
            "candidate_id": target_id,
            "ata": "ATA 99",
            "category": "object_ata",
            "entity_id": "object_1",
            "verification_status": "confirmed",
            "confirmed_claim": "mismatched claim",
        }
        document = {
            "document_id": "bad",
            "document_type": "AMM",
            "revision": "1",
            "effectivity": "A320",
            "section_reference": "99-00",
            "trust_level": "controlled_oem",
            "applicable": True,
            "current_revision": True,
            "verification_status": "confirmed",
            "confirmed_candidates": [record],
        }
        result, _ = self.run_pipeline(
            mapping,
            actions,
            retriever=FixedRetriever([document]),
        )
        self.assertEqual(result["controlled_evidence"], [])
        self.assertEqual(result["retrieved_documents"], [document])

    def test_production_evidence_adapter_receives_exact_mapping_candidates(self) -> None:
        class CaptureRetriever:
            def __init__(self) -> None:
                self.filters: dict[str, object] = {}

            def retrieve(
                self,
                query: str,
                filters: dict[str, object],
                limit: int,
            ) -> dict[str, object]:
                self.filters = filters
                return {"status": "completed", "documents": [], "warnings": []}

        candidate = {
            "candidate_id": "candidate:object_ata:object_1:ATA_25:1",
            "ata": "ATA 25",
            "mapping_category": "object_ata",
            "entity_id": "object_1",
            "initial_state": "candidate_unverified",
        }
        retriever = CaptureRetriever()
        result = LegacyEvidenceRetrieverAdapter(retriever).search(
            request="cargo equipment damage",
            engineering_facts=facts(),
            ata_candidates=["ATA 25"],
            mapping_candidates=[candidate],
            aircraft={"family": "A320"},
            document_types=["AMM"],
            limit=12,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(retriever.filters["mapping_candidates"], [candidate])
        self.assertEqual(
            retriever.filters["mapping_candidates"][0]["candidate_id"],  # type: ignore[index]
            candidate["candidate_id"],
        )

    def test_source_fragment_only_procedure_candidate_is_rejected(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        procedure = {
            "ata": "ATA 51",
            "confidence": 0.7,
            "reason": "procedure may apply",
            "source_fragment": "equipment corrosion",
        }
        mapping["procedure_ata_hypotheses"] = [procedure]
        actions = confirm_actions(mapping)
        result, _ = self.run_pipeline(
            mapping,
            actions,
            retriever=FixedRetriever([]),
        )
        self.assertEqual(result["validated_ata"]["possible_procedure"], [])
        self.assertTrue(
            any(
                "procedure_without_factual_anchor:ATA 51" in warning
                for warning in result["warnings"]
            )
        )

    def test_installed_in_is_context_not_interface_basis(self) -> None:
        engineering_facts = facts()
        engineering_facts["relations"][0]["relation"] = "installed_in"  # type: ignore[index]
        mapping = combined_mapping("ATA 25")["ata_mapping"]
        validated, warnings = validate_mapping(mapping, engineering_facts, [], "installed in")
        self.assertEqual(validated["interface_ata_hypotheses"], [])
        self.assertTrue(any("non_interface_relation" in warning for warning in warnings))

    def test_installed_in_pipeline_keeps_location_context_only(self) -> None:
        engineering_facts = facts()
        engineering_facts["relations"][0]["relation"] = "installed_in"  # type: ignore[index]
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        result, _ = self.run_pipeline(
            mapping,
            confirm_actions(mapping),
            engineering_facts=engineering_facts,
            retriever=FixedRetriever([]),
        )
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        self.assertEqual(result["context_ata"], ["ATA 53"])
        self.assertEqual(result["potentially_affected_ata"], [])

    def test_structured_interface_relations_remain_valid(self) -> None:
        for relation_type in ("attached_to", "connected_to", "requires_access_through"):
            with self.subTest(relation_type=relation_type):
                engineering_facts = facts()
                engineering_facts["relations"][0]["relation"] = relation_type  # type: ignore[index]
                mapping = combined_mapping("ATA 25")["ata_mapping"]
                validated, _ = validate_mapping(mapping, engineering_facts, [], relation_type)
                self.assertTrue(validated["interface_ata_hypotheses"])

    def test_adjacent_relation_requires_structured_interface_basis(self) -> None:
        engineering_facts = facts()
        engineering_facts["relations"][0]["relation"] = "adjacent_to"  # type: ignore[index]
        mapping = combined_mapping("ATA 25")["ata_mapping"]
        validated, warnings = validate_mapping(mapping, engineering_facts, [], "protect adjacent")
        self.assertEqual(validated["interface_ata_hypotheses"], [])
        self.assertTrue(
            any(
                warning.startswith("adjacent_without_access_or_protection:")
                for warning in warnings
            )
        )

    def test_part_of_is_not_an_interface_relation(self) -> None:
        engineering_facts = facts()
        engineering_facts["relations"][0]["relation"] = "part_of"  # type: ignore[index]
        mapping = combined_mapping("ATA 25")["ata_mapping"]
        validated, warnings = validate_mapping(
            mapping,
            engineering_facts,
            [],
            "part of structure",
        )
        self.assertEqual(validated["interface_ata_hypotheses"], [])
        self.assertTrue(
            any(warning.startswith("non_interface_relation:") for warning in warnings)
        )

    def test_critic_transition_rejects_adjacent_without_access_or_protection(self) -> None:
        engineering_facts = facts()
        engineering_facts["relations"][0]["relation"] = "adjacent_to"  # type: ignore[index]
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["location_context_ata"] = []
        actions = confirm_actions(mapping)
        actions["actions"][0].update(  # type: ignore[index]
            {
                "action": "downgrade_to_possible",
                "relation_id": "relation_1",
                "reason": "adjacent item",
            }
        )
        result, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=engineering_facts,
        )
        self.assertEqual(result["affected_ata"], [])
        self.assertEqual(result["potentially_affected_ata"], [])
        self.assertTrue(result["validated_ata"]["candidate_unverified"])
        self.assertTrue(
            any(
                warning.startswith("incompatible_critic_action:")
                for warning in result["warnings"]
            )
        )

    def test_reasoning_embedded_json_is_not_accepted_without_successful_repair(self) -> None:
        reasoning = 'analysis {"aircraft":{}} final {"aircraft":{}}'
        result = AtaImpactService(self.certificate, SequenceLLM(reasoning, "still invalid")).analyze(
            "equipment damage"
        )
        self.assertEqual(result["affected_ata"], [])
        self.assertEqual(result["decision"], "engineering_review_required")

    def test_empty_and_array_responses_fail_after_one_repair(self) -> None:
        for raw in ("", "[]"):
            with self.subTest(raw=raw):
                llm = SequenceLLM(raw, "repair also invalid")
                result = AtaImpactService(self.certificate, llm).analyze("equipment damage")
                self.assertEqual(result["affected_ata"], [])
                self.assertEqual(len(llm.calls), 2)

    def test_schema_invalid_mapping_uses_one_repair_with_validation_errors(self) -> None:
        invalid_mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        invalid_mapping["object_ata"][0].pop("reason")
        repaired = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        llm = SequenceLLM(
            facts(),
            invalid_mapping,
            repaired,
            confirm_actions(repaired),
        )
        result = AtaImpactService(
            self.certificate,
            llm,
            FixedRetriever([]),
        ).analyze("A320 equipment damage")
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        self.assertEqual(llm.calls[2][0], ATA_JSON_REPAIR_PROMPT)
        repair_payload = json.loads(llm.calls[2][1])
        self.assertEqual(repair_payload["stage"], "ata_mapping")
        self.assertIn("stage_contract", repair_payload)
        self.assertEqual(
            repair_payload["response_schema"],
            ATA_MAPPING_GENERATION_SCHEMA,
        )
        self.assertTrue(
            any(
                error.startswith("mapping_item_missing_required:")
                for error in repair_payload["validation_errors"]
            )
        )
        mapping_trace = next(
            item
            for item in result["agent_trace"]
            if item["step"] == "ata_mapping"
        )
        self.assertEqual(mapping_trace["repair"], "completed")

    def test_additional_mapping_property_requires_repair_and_fail_safe(self) -> None:
        invalid_mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        invalid_mapping["location_context_ata"] = []
        invalid_mapping["object_ata"][0]["unexpected"] = "unsafe"
        repaired = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        repaired["location_context_ata"] = []
        llm = SequenceLLM(
            facts(structure_damage=True),
            invalid_mapping,
            repaired,
            confirm_actions(repaired),
        )
        result = AtaImpactService(
            self.certificate,
            llm,
            FixedRetriever([]),
        ).analyze("A320 equipment damage")
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        self.assertEqual(llm.calls[2][0], ATA_JSON_REPAIR_PROMPT)
        errors = json.loads(llm.calls[2][1])["validation_errors"]
        self.assertTrue(
            any(error.startswith("schema_additional_property:") for error in errors)
        )

        failed = SequenceLLM(
            facts(structure_damage=True),
            invalid_mapping,
            invalid_mapping,
        )
        result = AtaImpactService(
            self.certificate,
            failed,
            FixedRetriever([]),
        ).analyze("A320 equipment damage")
        self.assertEqual(result["affected_ata"], [])
        self.assertEqual(result["decision"], "engineering_review_required")

    def test_mapping_enum_and_range_errors_require_repair(self) -> None:
        for warning_prefix, mutate in (
            (
                "schema_maximum_error:",
                lambda mapping: mapping["object_ata"][0].update(
                    {"confidence": 1.5}
                ),
            ),
            (
                "schema_enum_error:",
                lambda mapping: mapping["user_declared_ata"].append(
                    {
                        "ata": "ATA 25",
                        "confidence": 0.9,
                        "reason": "declared",
                        "status": "model_decided",
                    }
                ),
            ),
        ):
            with self.subTest(warning_prefix=warning_prefix):
                invalid = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
                invalid["location_context_ata"] = []
                mutate(invalid)
                repaired = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
                repaired["location_context_ata"] = []
                llm = SequenceLLM(
                    facts(structure_damage=True),
                    invalid,
                    repaired,
                    confirm_actions(repaired),
                )
                result = AtaImpactService(
                    self.certificate,
                    llm,
                    FixedRetriever([]),
                ).analyze("A320 equipment damage")
                self.assertEqual(result["affected_ata"], ["ATA 25"])
                errors = json.loads(llm.calls[2][1])["validation_errors"]
                self.assertTrue(
                    any(error.startswith(warning_prefix) for error in errors)
                )

    def test_mapping_item_missing_ata_uses_one_schema_repair(self) -> None:
        invalid_mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        invalid_mapping["object_ata"][0].pop("ata")
        repaired = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        llm = SequenceLLM(
            facts(),
            invalid_mapping,
            repaired,
            confirm_actions(repaired),
        )
        result = AtaImpactService(
            self.certificate,
            llm,
            FixedRetriever([]),
        ).analyze("A320 equipment damage")
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        self.assertEqual(llm.calls[2][0], ATA_JSON_REPAIR_PROMPT)
        repair_payload = json.loads(llm.calls[2][1])
        self.assertTrue(
            any(
                error.startswith("invalid_ata:")
                for error in repair_payload["validation_errors"]
            )
        )

    def test_non_object_mapping_item_uses_schema_repair(self) -> None:
        invalid_mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        invalid_mapping["object_ata"].append("junk")  # type: ignore[arg-type]
        repaired = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        llm = SequenceLLM(
            facts(),
            invalid_mapping,
            repaired,
            confirm_actions(repaired),
        )
        result = AtaImpactService(
            self.certificate,
            llm,
            FixedRetriever([]),
        ).analyze("A320 equipment damage")
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        repair_payload = json.loads(llm.calls[2][1])
        self.assertTrue(
            any(
                error.startswith("schema_type_error:ata_mapping:")
                for error in repair_payload["validation_errors"]
            )
        )

    def test_fenced_json_and_single_repair_are_supported(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        fenced_facts = "```json\n" + json.dumps(facts()) + "\n```"
        llm = SequenceLLM(
            fenced_facts,
            "invalid mapping response",
            mapping,
            confirm_actions(mapping),
        )
        result = AtaImpactService(self.certificate, llm).analyze("A320 equipment damage")
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        mapping_trace = next(
            item
            for item in result["agent_trace"]
            if item["step"] == "ata_mapping"
        )
        self.assertEqual(mapping_trace["repair"], "completed")

    def test_malformed_fenced_json_is_not_accepted(self) -> None:
        llm = SequenceLLM(
            "```json\n{}\n``` trailing text",
            "```json\n{}\n",
        )
        result = AtaImpactService(self.certificate, llm).analyze(
            "A320 equipment damage"
        )
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(result["affected_ata"], [])
        self.assertEqual(result["decision"], "engineering_review_required")

    def test_truncated_response_fails_without_partial_json_recovery(self) -> None:
        class TruncatedLLM:
            def chat_response(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {"content": '{"aircraft":', "finish_reason": "length"}

        result = AtaImpactService(self.certificate, TruncatedLLM()).analyze("damage")
        fact_trace = result["agent_trace"][1]
        self.assertEqual(fact_trace["status"], "repair_failed")
        self.assertEqual(fact_trace["warning"], "truncated_response")
        self.assertEqual(fact_trace["reason"], "truncated_response")
        self.assertEqual(fact_trace["repair"], "repair_failed")

    def test_completed_decision_requires_fully_closed_analysis(self) -> None:
        closed_facts = facts(structure_damage=True)
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["structural_ata"] = []
        mapping["location_context_ata"] = []
        actions = confirm_actions(mapping)
        completed, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=closed_facts,
            retriever=FixedRetriever([]),
        )
        self.assertEqual(completed["decision"], "completed")

        uncertain_facts = dict(closed_facts)
        uncertain_facts["uncertainties"] = ["effectivity is unclear"]
        uncertain, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=uncertain_facts,
            retriever=FixedRetriever([]),
        )
        self.assertEqual(uncertain["decision"], "additional_input_required")

        no_aircraft = dict(closed_facts)
        no_aircraft["aircraft"] = {"family": None, "model": None, "msn": None, "confidence": 0.5}
        required, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=no_aircraft,
            retriever=FixedRetriever([]),
        )
        self.assertEqual(required["decision"], "additional_input_required")

    def test_conflicting_user_declared_ata_blocks_completed(self) -> None:
        engineering_facts = facts(structure_damage=True)
        mapping = combined_mapping(
            "ATA 25",
            context_ata="ATA 53",
            interface_ata=None,
            user_ata="ATA 34",
        )["ata_mapping"]
        mapping["structural_ata"] = []
        mapping["location_context_ata"] = []
        llm = SequenceLLM(
            engineering_facts,
            mapping,
            confirm_actions(mapping),
        )
        result = AtaImpactService(
            self.certificate,
            llm,
            FixedRetriever([]),
        ).analyze(
            "A320 cargo equipment damage; request also declares ATA 34",
            runtime_mode="standard",
        )
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        self.assertEqual(
            [
                item["ata"]
                for item in result["validated_ata"][
                    "user_declared_conflicting"
                ]
            ],
            ["ATA 34"],
        )
        self.assertEqual(result["decision"], "engineering_review_required")
        self.assertIn("user_declared_ata_unresolved", result["decision_reasons"])

    def test_consistent_user_declared_ata_does_not_block_completed(self) -> None:
        engineering_facts = facts(structure_damage=True)
        mapping = combined_mapping(
            "ATA 25",
            interface_ata=None,
            user_ata="ATA 25",
            user_status="consistent",
        )["ata_mapping"]
        mapping["structural_ata"] = []
        mapping["location_context_ata"] = []
        llm = SequenceLLM(
            engineering_facts,
            mapping,
            confirm_actions(mapping),
        )
        result = AtaImpactService(
            self.certificate,
            llm,
            FixedRetriever([]),
        ).analyze(
            "A320 customer declared ATA 25; cargo equipment is damaged",
            runtime_mode="standard",
        )
        self.assertEqual(result["decision"], "completed")
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        self.assertEqual(
            [
                item["ata"]
                for item in result["validated_ata"]["user_declared_consistent"]
            ],
            ["ATA 25"],
        )
        self.assertEqual(result["validated_ata"]["user_declared_unverified"], [])

    def test_same_ata_declared_role_conflict_blocks_completion(self) -> None:
        engineering_facts = facts(structure_damage=True)
        mapping = combined_mapping(
            "ATA 25",
            interface_ata=None,
            user_ata="ATA 25",
            user_status="conflicting",
        )["ata_mapping"]
        mapping["structural_ata"] = []
        mapping["location_context_ata"] = []
        llm = SequenceLLM(
            engineering_facts,
            mapping,
            confirm_actions(mapping),
        )
        result = AtaImpactService(
            self.certificate,
            llm,
            FixedRetriever([]),
        ).analyze(
            "A320 customer declared ATA 25 with a conflicting technical role",
            runtime_mode="standard",
        )
        self.assertEqual(result["affected_ata"], ["ATA 25"])
        self.assertEqual(
            [
                item["ata"]
                for item in result["validated_ata"]["user_declared_conflicting"]
            ],
            ["ATA 25"],
        )
        self.assertEqual(result["decision"], "engineering_review_required")

    def test_multiple_user_declarations_are_reconciled_independently(self) -> None:
        engineering_facts = facts(structure_damage=True)
        mapping = combined_mapping(
            "ATA 25",
            interface_ata=None,
            user_ata="ATA 25",
            user_status="consistent",
        )["ata_mapping"]
        mapping["structural_ata"] = []
        mapping["location_context_ata"] = []
        mapping["user_declared_ata"].append(
            {
                "ata": "ATA 34",
                "confidence": 0.9,
                "reason": "second declared chapter",
                "status": "unverified",
            }
        )
        llm = SequenceLLM(
            engineering_facts,
            mapping,
            confirm_actions(mapping),
        )
        result = AtaImpactService(
            self.certificate,
            llm,
            FixedRetriever([]),
        ).analyze(
            "A320 cargo equipment damage; customer declared ATA 25 and ATA 34",
            runtime_mode="standard",
        )
        self.assertEqual(
            [item["ata"] for item in result["validated_ata"]["user_declared_consistent"]],
            ["ATA 25"],
        )
        self.assertEqual(
            [item["ata"] for item in result["validated_ata"]["user_declared_conflicting"]],
            ["ATA 34"],
        )
        self.assertEqual(result["decision"], "engineering_review_required")

    def test_declared_outside_or_ambiguous_certificate_requires_review(self) -> None:
        for declared, expected_bucket in (
            ("ATA 99", "user_declared_not_in_certificate"),
            ("ATA 25-99", "user_declared_conflicting"),
        ):
            with self.subTest(declared=declared):
                engineering_facts = facts(structure_damage=True)
                mapping = combined_mapping(
                    "ATA 25",
                    interface_ata=None,
                    user_ata=declared,
                )["ata_mapping"]
                mapping["structural_ata"] = []
                mapping["location_context_ata"] = []
                llm = SequenceLLM(
                    engineering_facts,
                    mapping,
                    confirm_actions(mapping),
                )
                result = AtaImpactService(
                    self.certificate,
                    llm,
                    FixedRetriever([]),
                ).analyze(
                    f"A320 cargo equipment damage; customer declared {declared}",
                    runtime_mode="standard",
                )
                self.assertEqual(
                    [item["ata"] for item in result["validated_ata"][expected_bucket]],
                    [declared],
                )
                self.assertEqual(result["decision"], "engineering_review_required")

    def test_mapper_cannot_invent_user_declaration(self) -> None:
        engineering_facts = facts(structure_damage=True)
        mapping = combined_mapping(
            "ATA 25",
            interface_ata=None,
            user_ata="ATA 34",
        )["ata_mapping"]
        mapping["structural_ata"] = []
        mapping["location_context_ata"] = []
        llm = SequenceLLM(
            engineering_facts,
            mapping,
            confirm_actions(mapping),
        )
        result = AtaImpactService(
            self.certificate,
            llm,
            FixedRetriever([]),
        ).analyze(
            "A320 cargo equipment damage with no customer ATA declaration",
            runtime_mode="standard",
        )
        self.assertEqual(result["ata_mapping"]["user_declared_ata"], [])
        self.assertTrue(
            any(
                warning == "mapper_user_declared_not_formally_extracted:ATA 34"
                for warning in result["warnings"]
            )
        )
        self.assertEqual(result["decision"], "completed")

    def test_completed_is_blocked_by_critical_warning_or_missing_required_document(self) -> None:
        engineering_facts = facts(structure_damage=True)
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["structural_ata"] = []
        mapping["location_context_ata"] = []
        mapping["interface_ata_hypotheses"] = [
            {
                "ata": "ATA 53",
                "relation_id": "missing",
                "confidence": 0.7,
                "reason": "invalid interface",
            }
        ]
        actions = confirm_actions(mapping)
        warned, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=engineering_facts,
            retriever=FixedRetriever([]),
        )
        self.assertEqual(warned["decision"], "engineering_review_required")
        self.assertIn("critical_validation_warning", warned["decision_reasons"])

        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        mapping["structural_ata"] = []
        mapping["location_context_ata"] = []
        actions = confirm_actions(mapping)
        actions["actions"][0]["action"] = "require_document"  # type: ignore[index]
        missing_document, _ = self.run_pipeline(
            mapping,
            actions,
            engineering_facts=engineering_facts,
            retriever=FixedRetriever([]),
        )
        self.assertEqual(missing_document["decision"], "document_verification_required")
        self.assertEqual(missing_document["validated_ata"]["document_confirmed"], [])
        self.assertEqual(
            missing_document["secondary_ata"][0]["status"],
            "document_verification_required",
        )

        evidence_warning, _ = self.run_pipeline(
            mapping,
            confirm_actions(mapping),
            engineering_facts=engineering_facts,
            retriever=FixedRetriever(
                [],
                warnings=["schema_evidence_integrity_failure"],
            ),
        )
        self.assertEqual(evidence_warning["decision"], "engineering_review_required")
        self.assertIn("critical_validation_warning", evidence_warning["decision_reasons"])

    def test_runtime_modes_are_strict_and_legacy_is_feature_flagged(self) -> None:
        for mode in ("auto", "standard", "extended", " standard "):
            expected = mode.strip()
            self.assertEqual(validate_ata_runtime_mode(mode), expected)
        for mode in ("", "standrd", "AUTO", "Standard"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_ata_runtime_mode(mode)
        for legacy in ("rules_only", "ontology_llm", "full_pipeline"):
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ValueError):
                    AtaImpactAgent().analyze("damage", mode=legacy)
            with patch.dict(
                "os.environ",
                {
                    "MRO_KB_ENABLE_LEGACY_ATA_MODES": "true",
                    "MRO_KB_ATA_AGENT_LLM_ENABLED": "0",
                },
            ):
                self.assertEqual(
                    AtaImpactAgent().analyze("damage", mode=legacy)["mode"],
                    legacy,
                )

    def test_mapping_without_candidate_id_gets_python_identity(self) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        validated, warnings = validate_mapping(
            mapping,
            facts(),
            [],
            "A320 equipment corrosion near structure",
        )
        candidate = validated["object_ata"][0]
        self.assertEqual(
            candidate["candidate_id"],
            "candidate:object_ata:object_1:ATA_25:1",
        )
        self.assertFalse(warnings)

    def test_auto_depth_uses_reconciled_declaration_field_name(self) -> None:
        mapping = empty_mapping()
        mapping["user_declared_ata"].append(
            {
                "ata": "ATA 25",
                "confidence": 1.0,
                "declared_assessment": "conflicting",
                "reason": "Customer declaration conflicts with the technical role.",
            }
        )
        self.assertTrue(
            AtaImpactService._needs_post_mapping_extended(mapping, facts())
        )

    def test_document_reference_formats_and_declared_ata_separation(self) -> None:
        text = (
            "Использовать AMM ATA 34-11, AMM 34-11, SRM 53-10-01, "
            "IPC ATA 25-50, CMM 32-10-15, WDM 24-00, NTM 51-00, ALS ATA 05-10."
        )
        identifiers = extract_identifiers(text)
        self.assertEqual(identifiers["explicit_ata"], [])
        references = identifiers["document_references"]
        self.assertEqual(
            [item["value"] for item in references],
            ["34-11", "34-11", "53-10-01", "25-50", "32-10-15", "24-00", "51-00", "05-10"],
        )
        self.assertEqual(references[0]["ata"], "ATA 34-11")

    def test_certificate_exact_subchapter_and_missing_subchapter(self) -> None:
        class Catalog:
            entries = [
                CertificateEntry("25", "20", "twenty", ""),
                CertificateEntry("25", "10", "ten", ""),
            ]
            by_system = {"25": entries}

        exact = validate_certificate(Catalog(), ["ATA 25-10"])[0]
        self.assertEqual(exact["certificate_scope_status"], "in_scope_candidate")
        self.assertEqual(exact["match_type"], "exact")
        self.assertEqual(exact["certificate_entry"]["name"], "ten")
        missing = validate_certificate(Catalog(), ["ATA 25-30"])[0]
        self.assertEqual(missing["certificate_scope_status"], "ambiguous_subchapter")
        self.assertIsNone(missing["certificate_entry"])
        chapter = validate_certificate(Catalog(), ["ATA 25"])[0]
        self.assertEqual(chapter["certificate_scope_status"], "in_scope_candidate")
        self.assertIsNone(chapter["certificate_entry"])

    def test_certificate_parent_chapter_covers_subchapter_and_assessment(self) -> None:
        class Catalog:
            path = Path("sertifikat_glavy_new.docx")
            entries = [CertificateEntry("53", "", "Фюзеляж", "")]
            by_system = {"53": entries}

        chapter_match = validate_certificate(Catalog(), ["ATA 53-10"])[0]
        self.assertEqual(
            chapter_match["certificate_scope_status"],
            "in_scope_candidate",
        )
        self.assertEqual(chapter_match["match_type"], "chapter")
        self.assertEqual(chapter_match["certificate_ata"], "ATA 53")

        covered = assess_certificate(Catalog(), ["ATA 53-10"], [])
        self.assertEqual(covered["status"], "covered")
        self.assertEqual(covered["matches"][0]["match_type"], "chapter")

        partial = assess_certificate(
            Catalog(),
            ["ATA 53-10"],
            ["ATA 34"],
        )
        self.assertEqual(partial["status"], "partially_covered")
        self.assertEqual(
            [item["ata"] for item in partial["missing"]],
            ["ATA 34"],
        )

        affected_missing = assess_certificate(
            Catalog(),
            ["ATA 34"],
            ["ATA 53-10"],
        )
        self.assertEqual(affected_missing["status"], "not_covered")

        undetermined = assess_certificate(Catalog(), [], ["ATA 53"])
        self.assertEqual(undetermined["status"], "undetermined")

    def test_new_certificate_assessment_preserves_legacy_scope_status(
        self,
    ) -> None:
        mapping = combined_mapping("ATA 25", interface_ata=None)["ata_mapping"]
        result, _ = self.run_pipeline(
            mapping,
            confirm_actions(mapping),
            retriever=FixedRetriever([]),
        )
        self.assertEqual(
            result["certificate_assessment"]["status"],  # type: ignore[index]
            "covered",
        )
        self.assertEqual(
            result["certificate_scope"]["status"],  # type: ignore[index]
            "in_scope_candidate",
        )
        self.assertIn("matches", result["certificate_assessment"])  # type: ignore[operator]
        self.assertIn("matched", result["certificate_scope"])  # type: ignore[operator]

    def test_gost_reference_is_bound_to_supplied_source_pdf(self) -> None:
        catalog = AtaClassificationReferenceCatalog()
        self.assertTrue(catalog.available)
        self.assertEqual(len(catalog.entries), 501)
        self.assertEqual(
            catalog.source_file_sha256,
            "816d7e245823acf02afb6e463d8e6818c0d721c84a3c88526a1790165a087676",
        )

    def test_gost_vector_retrieval_returns_only_small_ranked_subset(self) -> None:
        catalog = AtaClassificationReferenceCatalog(
            vector_cache_path=Path("/tmp/mro-kb-missing-ata-vectors.json")
        )
        target = next(
            entry
            for entry in catalog.entries
            if entry.ata == "ATA 50" and entry.ata == entry.parent_ata
        )
        other = next(
            entry
            for entry in catalog.entries
            if entry.ata == "ATA 53" and entry.ata == entry.parent_ata
        )
        catalog._vectors = {
            target.ata: [1.0, 0.0],
            other.ata: [0.0, 1.0],
        }
        catalog.vector_search_enabled = True
        with patch.object(catalog, "_embed_query", return_value=[1.0, 0.0]):
            context = catalog.context(
                "corroded cargo roller track",
                {"physical_objects": [{"name": "cargo roller track"}]},
            )
        self.assertEqual(
            context["retrieval_mode"],
            "hybrid_vector_lexical",
        )
        self.assertEqual(
            context["relevant_definitions"][0]["ata"],  # type: ignore[index]
            "ATA 50",
        )
        self.assertLessEqual(len(context["relevant_definitions"]), 8)

    def test_gost_vector_endpoint_is_not_called_when_feature_is_disabled(
        self,
    ) -> None:
        with patch.dict(
            "os.environ",
            {
                "MRO_KB_ATA_REFERENCE_VECTORS_ENABLED": "false",
                "MRO_KB_ATA_AGENT_LLM_ENABLED": "false",
            },
        ):
            catalog = AtaClassificationReferenceCatalog()
        catalog._vectors = {"ATA 50": [1.0, 0.0]}
        with patch.object(catalog, "_embed_query") as embed_query:
            context = catalog.context(
                "corroded cargo roller track",
                {"physical_objects": [{"name": "cargo roller track"}]},
            )
        embed_query.assert_not_called()
        self.assertEqual(context["retrieval_mode"], "lexical_fallback")
        self.assertLessEqual(len(context["relevant_definitions"]), 8)

    def test_malformed_vector_cache_and_embedding_fail_to_lexical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "vectors.json"
            cache.write_text("[]", encoding="utf-8")
            catalog = AtaClassificationReferenceCatalog(
                vector_cache_path=cache
            )
            self.assertEqual(catalog._vectors, {})

        catalog = AtaClassificationReferenceCatalog(
            vector_cache_path=Path("/tmp/mro-kb-missing-vector-cache.json")
        )
        catalog._vectors = {"ATA 50": [1.0, 0.0]}
        catalog.vector_search_enabled = True
        with patch(
            "urllib.request.urlopen",
            return_value=_HttpResponse(
                {"embeddings": [["not-a-number"]]}
            ),
        ):
            context = catalog.context(
                "corroded cargo roller track",
                {"physical_objects": [{"name": "cargo roller track"}]},
            )
        self.assertEqual(context["retrieval_mode"], "lexical_fallback")

    def test_mapping_failure_does_not_repeat_reference_retrieval(self) -> None:
        class CountingCatalog(AtaClassificationReferenceCatalog):
            def __init__(self) -> None:
                super().__init__()
                self.context_calls = 0

            def context(
                self,
                request: str,
                engineering_facts: dict[str, object],
                limit: int = 8,
            ) -> dict[str, object]:
                self.context_calls += 1
                return super().context(request, engineering_facts, limit)

        catalog = CountingCatalog()
        result = AtaImpactService(
            self.certificate,
            SequenceLLM(facts(), "invalid mapping", "invalid repair"),
            reference_catalog=catalog,
        ).analyze("A320 cargo equipment damage")
        self.assertEqual(result["runtime_mode"], "fallback")
        self.assertEqual(catalog.context_calls, 1)

    def test_no_production_scenario_hardcodes_or_combined_prompt_usage(self) -> None:
        root = Path(__file__).resolve().parents[1]
        production = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (root / "core" / "ata_impact").glob("*.py")
        )
        self.assertNotIn("ATA_MAPPING_AND_CRITIC_PROMPT", production)
        for phrase in ("roller track", "gear rib", "shock strut", "static pressure port"):
            self.assertNotIn(phrase, production.lower())


if __name__ == "__main__":
    unittest.main()
