from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

from llm.agent.llm_edit_planner import LlmEditPlanner
from llm.agent.orchestrator import ProjectAgentOrchestrator
from llm.mdpi_template_extractor import MdpiTemplateExtractor, get_extractor, is_mdpi_request


class MdpiDetectionTests(SimpleTestCase):
    def test_explicit_mdpi_requests_are_detected(self):
        self.assertTrue(is_mdpi_request("перепиши в формате mdpi"))
        self.assertTrue(is_mdpi_request("оформи как статью micromachines"))

    def test_generic_academic_requests_do_not_enable_mdpi(self):
        self.assertFalse(is_mdpi_request("сделай академичнее"))
        self.assertFalse(is_mdpi_request("добавь еще один абзац"))


class MdpiTemplateExtractorTests(SimpleTestCase):
    def test_existing_template_is_available_and_returns_prompt(self):
        extractor = get_extractor(str(Path(settings.BASE_DIR) / "micromachines-template.docx"))

        self.assertIsNotNone(extractor)
        self.assertTrue(extractor.is_available())
        self.assertIsInstance(extractor.extract_template_spec(), dict)
        self.assertTrue(extractor.system_prompt_block())

    def test_detect_section_guidance_for_conclusions(self):
        extractor = get_extractor(str(Path(settings.BASE_DIR) / "micromachines-template.docx"))

        guidance = extractor.detect_section_guidance("дополни заключение в формате mdpi")

        self.assertIn("Conclusions", guidance)

    def test_validate_section_order_reports_soft_issues(self):
        extractor = MdpiTemplateExtractor("missing-template.docx")
        paragraphs = [
            {"index": 0, "text": "Conclusions", "style": "Heading 1"},
            {"index": 1, "text": "Introduction", "style": "Heading 1"},
            {"index": 2, "text": "Abstract", "style": "Heading 1"},
            {"index": 3, "text": "References", "style": "Heading 1"},
        ]

        issues = extractor.validate_section_order(paragraphs)

        self.assertTrue(issues)

    def test_missing_template_does_not_raise(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            extractor = get_extractor(str(Path(temp_dir) / "missing.docx"))

            self.assertIsNotNone(extractor)
            self.assertFalse(extractor.is_available())
            self.assertIsInstance(extractor.extract_template_spec(), dict)
            self.assertTrue(extractor.system_prompt_block())


class _CaptureLlmClient:
    def __init__(self):
        self.messages = []

    def send_json_request(self, *, messages, schema_hint, temperature):
        self.messages = messages
        return {"summary": "No changes", "confidence": 0.0, "operations": []}


class LlmEditPlannerMdpiIntegrationTests(SimpleTestCase):
    def test_plain_prompt_does_not_add_mdpi_guidance(self):
        client = _CaptureLlmClient()
        planner = LlmEditPlanner(client)

        planner.plan("добавь еще один абзац", [{"index": 0, "text": "Text", "style": "Normal"}])

        combined = "\n".join(str(message.get("content", "")) for message in client.messages)
        self.assertNotIn("Requested format:\nMDPI", combined)

    def test_mdpi_prompt_adds_template_guidance_when_available(self):
        client = _CaptureLlmClient()
        planner = LlmEditPlanner(client)

        planner.plan("перепиши введение в формате mdpi", [{"index": 0, "text": "Introduction", "style": "Heading 1"}])

        combined = "\n".join(str(message.get("content", "")) for message in client.messages)
        self.assertIn("Requested format:\nMDPI / Micromachines manuscript style", combined)
        self.assertIn("MDPI / Micromachines", combined)

    def test_mdpi_prompt_uses_fallback_when_extractor_unavailable(self):
        client = _CaptureLlmClient()
        planner = LlmEditPlanner(client)

        with patch("llm.agent.llm_edit_planner.get_extractor", return_value=None):
            planner.plan("rewrite in manuscript format", [{"index": 0, "text": "Text", "style": "Normal"}])

        combined = "\n".join(str(message.get("content", "")) for message in client.messages)
        self.assertIn("formal academic journal style", combined)
        self.assertIn("Requested format:\nMDPI", combined)


class ProjectAgentOrchestratorMdpiIntegrationTests(SimpleTestCase):
    def test_plain_prompt_does_not_add_validation_block(self):
        context = ProjectAgentOrchestrator._mdpi_enriched_edit_context(
            "добавь еще один абзац",
            [{"index": 0, "text": "Conclusions", "style": "Heading 1"}],
            "",
        )

        self.assertNotIn("MDPI STRUCTURE ISSUES DETECTED", context)

    def test_mdpi_prompt_adds_validation_block(self):
        context = ProjectAgentOrchestrator._mdpi_enriched_edit_context(
            "перепиши в формате mdpi",
            [
                {"index": 0, "text": "Conclusions", "style": "Heading 1"},
                {"index": 1, "text": "Introduction", "style": "Heading 1"},
                {"index": 2, "text": "References", "style": "Heading 1"},
            ],
            "",
        )

        self.assertIn("MDPI STRUCTURE ISSUES DETECTED", context)
