from __future__ import annotations

import logging
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from django.template import Context, Template, TemplateSyntaxError

from document.models import DocumentGenerator, Synonym


SYNONYM_PATTERN_TEMPLATE = r"{{\s*%s\s*}}"
DOCXTPL_ROW_TAG_PATTERN = re.compile(r"{%\s*tr\s+(.+?)\s*%}")
JINJA_FOR_TAG_PATTERN = re.compile(r"{%\s*(tr\s+)?for\s+(.+?)\s+in\s+(.+?)\s*%}")
logger = logging.getLogger(__name__)


def build_synonym_context() -> dict[str, str]:
    """
    Return synonym mappings as {value_placeholder: code_expression}.

    Example:
    value = "жұмыс_атауы", code = "{{ object.title }}"
    mapping entry becomes: {"жұмыс_атауы": "{{ object.title }}"}

    Empty placeholders are skipped. If duplicate placeholders exist, the latest one wins.
    """
    synonym_context: dict[str, str] = {}
    synonyms = (
        Synonym.objects.exclude(value__isnull=True)
        .exclude(value__exact="")
        .order_by("id")
        .values_list("code", "value")
    )
    for code, value in synonyms:
        clean_value = (value or "").strip()
        if clean_value.startswith("{{") and clean_value.endswith("}}"):
            clean_value = clean_value[2:-2].strip()
        if not clean_value:
            continue
        synonym_context[clean_value] = (code or "").strip()
    return synonym_context


def _compile_synonym_pattern(placeholder: str) -> re.Pattern[str]:
    return re.compile(SYNONYM_PATTERN_TEMPLATE % re.escape(placeholder))


def replace_synonyms(template_string: str, synonym_context: dict[str, str] | None = None) -> str:
    """
    Replace all synonym placeholders in template string.

    Placeholder format supports spaces, for example:
    {{ code }}, {{code}}, {{   code   }}
    """
    if not template_string:
        return ""

    rendered = template_string
    context = synonym_context or build_synonym_context()

    for placeholder, code_expression in context.items():
        if not placeholder:
            continue
        pattern = _compile_synonym_pattern(placeholder)
        rendered = pattern.sub(lambda _: code_expression or "", rendered)

    return rendered


def recursive_render(
    template_string: str,
    max_depth: int = 5,
    synonym_context: dict[str, str] | None = None,
) -> str:
    """
    Re-run synonym replacement until no more changes or max_depth is reached.
    """
    if not template_string:
        return ""
    if max_depth < 1:
        return template_string

    rendered = template_string
    context = synonym_context or build_synonym_context()
    for _ in range(max_depth):
        updated = replace_synonyms(rendered, context)
        if updated == rendered:
            break
        rendered = updated
    return rendered


def _normalize_docxtpl_row_tags(template_string: str) -> str:
    """
    Convert docxtpl row tags into regular Django template tags for text preview/render.

    Example:
    {%tr for object in publications %} -> {% for object in publications %}
    """
    return DOCXTPL_ROW_TAG_PATTERN.sub(lambda match: "{% " + match.group(1) + " %}", template_string or "")


def _normalize_jinja_iterables(template_string: str) -> str:
    """
    Normalize iterable expressions for Jinja2/docxtpl loops.

    Django template allows `.all` in `{% for %}`, while Jinja2 needs `.all()`.
    """
    if not template_string:
        return ""

    def _replace(match: re.Match[str]) -> str:
        prefix = (match.group(1) or "").strip()
        loop_target = (match.group(2) or "").strip()
        iterable_expr = (match.group(3) or "").strip()
        normalized_iterable = re.sub(r"\.all(?!\s*\()", ".all()", iterable_expr)
        if prefix:
            return "{%tr for " + loop_target + " in " + normalized_iterable + " %}"
        return "{% for " + loop_target + " in " + normalized_iterable + " %}"

    return JINJA_FOR_TAG_PATTERN.sub(_replace, template_string)


def render_template(template_string: str, context: dict[str, Any]) -> str:
    """
    Render template string using Django Template engine.
    """
    try:
        template = Template(template_string or "")
    except TemplateSyntaxError as exc:
        raise ValueError(f"Template syntax error: {exc}") from exc
    return template.render(Context(context or {}, autoescape=False))


def generate_document(template: DocumentGenerator, context: dict[str, Any]) -> str:
    """
    Generate final text from DocumentGenerator.content in two stages:
    1) recursive synonym replacement
    2) Django template rendering
    """
    template_id = getattr(template, "id", None)
    base_template = template.content or ""
    logger.info("Template render start template_id=%s base_len=%s", template_id, len(base_template))
    synonym_rendered = recursive_render(base_template)
    logger.info("Template render after synonyms template_id=%s len=%s", template_id, len(synonym_rendered))
    django_ready_template = _normalize_docxtpl_row_tags(synonym_rendered)
    rendered = render_template(django_ready_template, context)
    if not (rendered or "").strip():
        logger.warning("Template render result is empty template_id=%s", template_id)
    else:
        logger.info("Template render finished template_id=%s rendered_len=%s", template_id, len(rendered))
    return rendered


def _prepare_docx_template_with_synonyms(
    source_docx_path: str | os.PathLike[str],
    max_depth: int = 5,
) -> str:
    """
    Create a temporary docx where synonym placeholders are already replaced.
    """
    synonym_context = build_synonym_context()
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as temp_docx:
        temp_path = temp_docx.name

    with zipfile.ZipFile(source_docx_path, "r") as source_archive, zipfile.ZipFile(
        temp_path, "w", zipfile.ZIP_DEFLATED
    ) as target_archive:
        for item in source_archive.infolist():
            content = source_archive.read(item.filename)
            if item.filename.startswith("word/") and item.filename.endswith(".xml"):
                try:
                    xml_content = content.decode("utf-8")
                except UnicodeDecodeError:
                    target_archive.writestr(item, content)
                    continue
                replaced_xml = recursive_render(xml_content, max_depth=max_depth, synonym_context=synonym_context)
                replaced_xml = _normalize_jinja_iterables(replaced_xml)
                content = replaced_xml.encode("utf-8")
            target_archive.writestr(item, content)

    return temp_path


def _normalize_docx_row_tags_file(source_docx_path: str | os.PathLike[str]) -> str:
    """
    Create a temporary DOCX where {%tr ... %} tags are converted to regular {% ... %} tags.

    This is used as a fallback when docxtpl cannot parse row tags in some templates.
    """
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as temp_docx:
        temp_path = temp_docx.name

    with zipfile.ZipFile(source_docx_path, "r") as source_archive, zipfile.ZipFile(
        temp_path, "w", zipfile.ZIP_DEFLATED
    ) as target_archive:
        for item in source_archive.infolist():
            content = source_archive.read(item.filename)
            if item.filename.startswith("word/") and item.filename.endswith(".xml"):
                try:
                    xml_content = content.decode("utf-8")
                except UnicodeDecodeError:
                    target_archive.writestr(item, content)
                    continue
                normalized_xml = _normalize_docxtpl_row_tags(xml_content)
                normalized_xml = _normalize_jinja_iterables(normalized_xml)
                content = normalized_xml.encode("utf-8")
            target_archive.writestr(item, content)

    return temp_path


def generate_docx(
    template: DocumentGenerator,
    context: dict[str, Any],
    output_path: str | os.PathLike[str],
) -> str:
    """
    Generate a DOCX file from DocumentGenerator.file template using docxtpl.

    Steps:
    1) apply synonym replacement inside docx XML
    2) render with docxtpl context
    3) save to output_path
    """
    try:
        from docxtpl import DocxTemplate
    except ImportError as exc:
        raise RuntimeError("docxtpl is required for DOCX generation. Install it with `pip install docxtpl`.") from exc

    if not template.file:
        raise ValueError("Template file is required for DOCX generation.")

    source_path = Path(template.file.path)
    if source_path.suffix.lower() != ".docx":
        raise ValueError("Template file must be a .docx file.")

    rendered_path = Path(output_path)
    rendered_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(
        "DOCX render start template_id=%s source=%s output=%s",
        getattr(template, "id", None),
        source_path,
        rendered_path,
    )
    temp_docx_path = _prepare_docx_template_with_synonyms(source_path)
    normalized_docx_path: str | None = None
    try:
        try:
            doc = DocxTemplate(temp_docx_path)
            doc.render(context or {})
            doc.save(str(rendered_path))
        except Exception as exc:  # noqa: BLE001
            # Some DOCX templates contain valid {%tr ... %} tags that fail parser heuristics
            # and trigger "unknown tag 'endfor'". Retry once with normalized {% ... %} tags.
            if "unknown tag 'endfor'" not in str(exc):
                raise
            logger.warning(
                "DOCX render retry with normalized row tags template_id=%s error=%s",
                getattr(template, "id", None),
                exc,
            )
            normalized_docx_path = _normalize_docx_row_tags_file(temp_docx_path)
            doc = DocxTemplate(normalized_docx_path)
            doc.render(context or {})
            doc.save(str(rendered_path))
        logger.info(
            "DOCX render finished template_id=%s output=%s",
            getattr(template, "id", None),
            rendered_path,
        )
    finally:
        try:
            os.remove(temp_docx_path)
        except OSError:
            pass
        if normalized_docx_path:
            try:
                os.remove(normalized_docx_path)
            except OSError:
                pass

    return str(rendered_path)


def preview_template(template_id: int, context: dict[str, Any]) -> str:
    """
    Return preview text by rendering a saved DocumentGenerator.content template.
    """
    template = DocumentGenerator.objects.filter(pk=template_id).first()
    if not template:
        raise DocumentGenerator.DoesNotExist(f"DocumentGenerator with id={template_id} does not exist.")
    return generate_document(template, context)
