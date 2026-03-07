from __future__ import annotations

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


def build_synonym_context() -> dict[str, str]:
    """
    Return synonym mappings as {code: value}.

    Empty codes are skipped. If duplicate codes exist, the latest one wins.
    """
    synonym_context: dict[str, str] = {}
    synonyms = (
        Synonym.objects.exclude(code__isnull=True)
        .exclude(code__exact="")
        .order_by("id")
        .values_list("code", "value")
    )
    for code, value in synonyms:
        clean_code = (code or "").strip()
        if not clean_code:
            continue
        synonym_context[clean_code] = value or ""
    return synonym_context


def _compile_synonym_pattern(code: str) -> re.Pattern[str]:
    return re.compile(SYNONYM_PATTERN_TEMPLATE % re.escape(code))


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

    for code, value in context.items():
        if not code:
            continue
        pattern = _compile_synonym_pattern(code)
        rendered = pattern.sub(lambda _: value or "", rendered)

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
    base_template = template.content or ""
    synonym_rendered = recursive_render(base_template)
    django_ready_template = _normalize_docxtpl_row_tags(synonym_rendered)
    return render_template(django_ready_template, context)


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
                content = replaced_xml.encode("utf-8")
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

    temp_docx_path = _prepare_docx_template_with_synonyms(source_path)
    try:
        doc = DocxTemplate(temp_docx_path)
        doc.render(context or {})
        doc.save(str(rendered_path))
    finally:
        try:
            os.remove(temp_docx_path)
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

