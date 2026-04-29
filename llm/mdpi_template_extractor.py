from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from django.conf import settings
from docx import Document as DocxDocument


logger = logging.getLogger(__name__)


CANONICAL_SECTIONS = [
    "title",
    "authors",
    "abstract",
    "keywords",
    "introduction",
    "materials_and_methods",
    "results",
    "discussion",
    "conclusions",
    "references",
]

SECTION_DISPLAY = {
    "title": "Title",
    "authors": "Authors / affiliations",
    "abstract": "Abstract",
    "keywords": "Keywords",
    "introduction": "Introduction",
    "materials_and_methods": "Materials and Methods / Methods",
    "results": "Results",
    "discussion": "Discussion",
    "conclusions": "Conclusions",
    "references": "References",
}

SECTION_ALIASES = {
    "title": {"title", "article title", "manuscript title", "название", "заголовок"},
    "authors": {"authors", "author", "affiliations", "affiliation", "авторы", "аффилиации"},
    "abstract": {"abstract", "аннотация", "резюме"},
    "keywords": {"keywords", "keyword", "key words", "ключевые слова"},
    "introduction": {"introduction", "введение"},
    "materials_and_methods": {
        "materials and methods",
        "methods",
        "methodology",
        "experimental section",
        "материалы и методы",
        "методы",
        "методология",
    },
    "results": {"results", "результаты"},
    "discussion": {"discussion", "обсуждение"},
    "conclusions": {"conclusion", "conclusions", "заключение", "вывод", "выводы"},
    "references": {"references", "reference", "bibliography", "литература", "список литературы"},
}

MDPI_REQUEST_PATTERNS = [
    r"\bmdpi\b",
    r"\bmicromachines\b",
    r"формат[ае]?\s+mdpi",
    r"стать[ьяюи]\s+mdpi",
    r"шаблон[ауе]?\s+mdpi",
    r"шаблон[ауе]?\s+micromachines",
    r"стать[ьяюи]\s+micromachines",
    r"формат[ае]?\s+журнал[а]?",
    r"как\s+стать[ьяюю]\s+журнал[а]?",
    r"article\s+format\s+mdpi",
    r"mdpi\s+article\s+format",
    r"article\s+template",
    r"manuscript\s+format",
    r"journal\s+format",
    r"template\s+micromachines",
]

FALLBACK_MDPI_PROMPT_BLOCK = """
MDPI / Micromachines manuscript guidance:
- Follow formal academic journal style only when the user explicitly requests MDPI/Micromachines format.
- Prefer standard article sections where relevant: Abstract, Keywords, Introduction, Materials and Methods, Results, Discussion, Conclusions, References.
- Keep abstract concise and keywords as a separate block after the abstract when requested.
- Use section-driven manuscript style, logical transitions, neutral scientific tone, and no conversational wording.
- Do not invent missing scientific results, citations, experiments, datasets, methods, numeric values, or references.
- If editing an existing manuscript, preserve the current structure unless the user explicitly asks to restructure it.
- Keep edits minimal and targeted; if the request targets one section, prioritize that section only.
""".strip()


def _normalize_text(text: str) -> str:
    value = " ".join(str(text or "").strip().lower().split())
    value = re.sub(r"^[\d.\s]+", "", value)
    value = value.strip(" :;.-–—")
    return value


def is_mdpi_request(user_prompt: str) -> bool:
    prompt = _normalize_text(user_prompt)
    if not prompt:
        return False
    return any(re.search(pattern, prompt, re.IGNORECASE) for pattern in MDPI_REQUEST_PATTERNS)


def normalize_section_name(text: str) -> str:
    return _normalize_text(text)


def match_section_alias(text: str) -> str | None:
    normalized = normalize_section_name(text)
    if not normalized:
        return None
    for section, aliases in SECTION_ALIASES.items():
        if normalized in aliases:
            return section
        if any(normalized.startswith(alias + " ") for alias in aliases):
            return section
    return None


def is_heading_like(text: str, style_name: str) -> bool:
    value = str(text or "").strip()
    if not value or len(value) > 120:
        return False
    if match_section_alias(value):
        return True
    style = str(style_name or "").strip().lower()
    if any(marker in style for marker in ("heading", "title", "subtitle")):
        return True
    if len(value) <= 80 and not value.endswith(".") and value[:1].isupper():
        return True
    return False


def _default_template_spec() -> dict:
    return {
        "available": False,
        "ordered_sections": CANONICAL_SECTIONS.copy(),
        "section_titles": [SECTION_DISPLAY[section] for section in CANONICAL_SECTIONS],
        "heading_styles": [],
        "style_guidance": [
            "Academic journal manuscript style",
            "Concise abstract",
            "Keywords after abstract",
            "Formal section-based structure",
            "Neutral scientific tone",
            "No conversational phrases",
            "Conclusions near the end before references",
        ],
    }


class MdpiTemplateExtractor:
    def __init__(self, template_path: str):
        self.template_path = Path(template_path)
        self._spec: dict | None = None
        self._error: str = ""

    def is_available(self) -> bool:
        return self.template_path.exists() and self.template_path.is_file()

    def extract_template_spec(self) -> dict:
        if self._spec is not None:
            return self._spec

        spec = _default_template_spec()
        if not self.is_available():
            self._spec = spec
            return self._spec

        try:
            document = DocxDocument(str(self.template_path))
            heading_candidates: list[dict[str, str]] = []
            seen_sections: set[str] = set()
            ordered_sections: list[str] = []
            heading_styles: list[str] = []

            for paragraph in document.paragraphs:
                text = (paragraph.text or "").strip()
                style_name = paragraph.style.name if paragraph.style else "Normal"
                if not text:
                    continue
                if is_heading_like(text, style_name):
                    heading_candidates.append({"text": text, "style": style_name})
                    if style_name and style_name not in heading_styles:
                        heading_styles.append(style_name)
                matched = match_section_alias(text)
                if matched and matched not in seen_sections:
                    seen_sections.add(matched)
                    ordered_sections.append(matched)

            if len(ordered_sections) < 4:
                ordered_sections = CANONICAL_SECTIONS.copy()

            spec = {
                "available": True,
                "template_path": str(self.template_path),
                "ordered_sections": ordered_sections,
                "section_titles": [SECTION_DISPLAY.get(section, section) for section in ordered_sections],
                "heading_styles": heading_styles[:12],
                "heading_candidates": heading_candidates[:30],
                "style_guidance": _default_template_spec()["style_guidance"],
            }
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            logger.warning("Failed to parse MDPI template %s: %s", self.template_path, exc)

        self._spec = spec
        return self._spec

    def system_prompt_block(self) -> str:
        spec = self.extract_template_spec()
        section_titles = spec.get("section_titles") or _default_template_spec()["section_titles"]
        heading_styles = spec.get("heading_styles") or []
        style_line = "; ".join(spec.get("style_guidance") or _default_template_spec()["style_guidance"])
        styles_line = f"\nObserved heading styles: {', '.join(heading_styles[:8])}." if heading_styles else ""
        return (
            "MDPI / Micromachines template guidance:\n"
            f"- Preferred section order: {', '.join(section_titles)}.\n"
            f"- Style guidance: {style_line}.{styles_line}\n"
            "- Do not invent missing scientific results, citations, experiments, datasets, methods, numeric values, or references.\n"
            "- Preserve current manuscript structure unless the user explicitly asks to restructure it.\n"
            "- Keep edits minimal and targeted."
        )

    def detect_section_guidance(self, user_prompt: str) -> str:
        matched = match_section_alias(user_prompt)
        if matched:
            return f"User is likely targeting the {SECTION_DISPLAY.get(matched, matched)} section."

        prompt = _normalize_text(user_prompt)
        for section, aliases in SECTION_ALIASES.items():
            if any(alias in prompt for alias in aliases):
                return f"User is likely targeting the {SECTION_DISPLAY.get(section, section)} section."
        return ""

    def validate_section_order(self, paragraphs: list[dict]) -> list[str]:
        issues: list[str] = []
        observed: list[tuple[int, str, int]] = []
        order_index = {section: index for index, section in enumerate(CANONICAL_SECTIONS)}

        for fallback_index, paragraph in enumerate(paragraphs or []):
            if isinstance(paragraph, dict):
                text = str(paragraph.get("text") or "")
                style = str(paragraph.get("style") or "")
                paragraph_index = int(paragraph.get("index", fallback_index) or fallback_index)
            else:
                text = str(paragraph or "")
                style = ""
                paragraph_index = fallback_index
            if not is_heading_like(text, style):
                continue
            section = match_section_alias(text)
            if section:
                observed.append((paragraph_index, section, order_index.get(section, 999)))

        if not observed:
            return issues

        max_seen_order = -1
        max_seen_section = ""
        for paragraph_index, section, current_order in observed:
            if current_order < max_seen_order:
                issues.append(
                    f"{SECTION_DISPLAY.get(section, section)} appears after "
                    f"{SECTION_DISPLAY.get(max_seen_section, max_seen_section)} at paragraph {paragraph_index}; "
                    "check MDPI section order."
                )
            else:
                max_seen_order = current_order
                max_seen_section = section

        sections = [section for _, section, _ in observed]
        positions = {section: index for index, section, _ in observed}
        if "abstract" in sections and "keywords" not in sections:
            issues.append("Keywords block is missing after Abstract.")
        if "abstract" in positions and "keywords" in positions and positions["keywords"] < positions["abstract"]:
            issues.append("Keywords should appear after Abstract.")
        if len(sections) >= 4 and "introduction" not in sections:
            issues.append("Introduction section is missing from the detected manuscript structure.")
        if "references" in positions and positions["references"] != len(observed) - 1:
            issues.append("References should appear near the end after main article sections.")

        return issues


@lru_cache(maxsize=8)
def get_extractor(template_path: str | None = None) -> MdpiTemplateExtractor | None:
    try:
        resolved_path = Path(template_path) if template_path else Path(settings.BASE_DIR) / "micromachines-template.docx"
        return MdpiTemplateExtractor(str(resolved_path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to initialize MDPI template extractor: %s", exc)
        return None
