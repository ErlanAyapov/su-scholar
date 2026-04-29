from __future__ import annotations

from functools import lru_cache
from typing import Any

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree


MATHML_NS = "http://www.w3.org/1998/Math/MathML"
OMML_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
XML_SPACE_ATTR = "{http://www.w3.org/XML/1998/namespace}space"


MATHML_TO_OMML_XSLT = f"""
<xsl:stylesheet version="1.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:mml="{MATHML_NS}"
    xmlns:m="{OMML_NS}">
  <xsl:output method="xml" omit-xml-declaration="yes"/>

  <xsl:template match="/">
    <xsl:apply-templates/>
  </xsl:template>

  <xsl:template match="mml:math">
    <m:oMath>
      <xsl:apply-templates/>
    </m:oMath>
  </xsl:template>

  <xsl:template match="mml:mrow | mml:semantics | mml:mstyle">
    <xsl:apply-templates/>
  </xsl:template>

  <xsl:template match="mml:mi | mml:mn | mml:mo | mml:mtext">
    <m:r>
      <m:t>
        <xsl:value-of select="."/>
      </m:t>
    </m:r>
  </xsl:template>

  <xsl:template match="mml:mfrac">
    <m:f>
      <m:num><xsl:apply-templates select="*[1]"/></m:num>
      <m:den><xsl:apply-templates select="*[2]"/></m:den>
    </m:f>
  </xsl:template>

  <xsl:template match="mml:msup">
    <m:sSup>
      <m:e><xsl:apply-templates select="*[1]"/></m:e>
      <m:sup><xsl:apply-templates select="*[2]"/></m:sup>
    </m:sSup>
  </xsl:template>

  <xsl:template match="mml:msub">
    <m:sSub>
      <m:e><xsl:apply-templates select="*[1]"/></m:e>
      <m:sub><xsl:apply-templates select="*[2]"/></m:sub>
    </m:sSub>
  </xsl:template>

  <xsl:template match="mml:msubsup">
    <m:sSubSup>
      <m:e><xsl:apply-templates select="*[1]"/></m:e>
      <m:sub><xsl:apply-templates select="*[2]"/></m:sub>
      <m:sup><xsl:apply-templates select="*[3]"/></m:sup>
    </m:sSubSup>
  </xsl:template>

  <xsl:template match="mml:msqrt">
    <m:rad>
      <m:deg/>
      <m:e><xsl:apply-templates/></m:e>
    </m:rad>
  </xsl:template>

  <xsl:template match="mml:mroot">
    <m:rad>
      <m:deg><xsl:apply-templates select="*[2]"/></m:deg>
      <m:e><xsl:apply-templates select="*[1]"/></m:e>
    </m:rad>
  </xsl:template>

  <xsl:template match="mml:mspace">
    <m:r><m:t> </m:t></m:r>
  </xsl:template>

  <xsl:template match="text()"/>

  <xsl:template match="*">
    <xsl:apply-templates/>
  </xsl:template>
</xsl:stylesheet>
""".strip()


def _find_unescaped(text: str, needle: str, start: int) -> int:
    index = start
    while True:
        index = text.find(needle, index)
        if index < 0:
            return -1
        slash_count = 0
        pos = index - 1
        while pos >= 0 and text[pos] == "\\":
            slash_count += 1
            pos -= 1
        if slash_count % 2 == 0:
            return index
        index += len(needle)


def has_latex_formula_markers(text: str) -> bool:
    return any(segment["type"] != "text" for segment in split_text_and_formulas(text))


def split_text_and_formulas(text: str) -> list[dict]:
    value = str(text or "")
    if "$" not in value:
        return [{"type": "text", "text": value}] if value else []

    segments: list[dict[str, str]] = []
    cursor = 0
    text_start = 0

    def push_text(end: int) -> None:
        if end > text_start:
            segments.append({"type": "text", "text": value[text_start:end]})

    while cursor < len(value):
        if value[cursor] == "\\":
            cursor += 2
            continue

        if value.startswith("$$", cursor):
            end = _find_unescaped(value, "$$", cursor + 2)
            if end < 0:
                cursor += 2
                continue
            push_text(cursor)
            segments.append({"type": "block_formula", "latex": value[cursor + 2:end]})
            cursor = end + 2
            text_start = cursor
            continue

        if value[cursor] == "$":
            end = _find_unescaped(value, "$", cursor + 1)
            if end < 0:
                cursor += 1
                continue
            if value.startswith("$$", end):
                cursor += 1
                continue
            push_text(cursor)
            segments.append({"type": "inline_formula", "latex": value[cursor + 1:end]})
            cursor = end + 1
            text_start = cursor
            continue

        cursor += 1

    if text_start < len(value):
        segments.append({"type": "text", "text": value[text_start:]})
    return segments


def latex_to_mathml_string(latex: str) -> str:
    from latex2mathml.converter import convert

    return convert(str(latex or ""))


@lru_cache(maxsize=1)
def _mathml_to_omml_transform() -> etree.XSLT:
    stylesheet = etree.XML(MATHML_TO_OMML_XSLT.encode("utf-8"))
    return etree.XSLT(stylesheet)


def mathml_to_omml(mathml: str) -> etree._Element:
    mathml_root = etree.fromstring(str(mathml or "").encode("utf-8"))
    result = _mathml_to_omml_transform()(mathml_root)
    root = result.getroot()
    if root is None:
        raise ValueError("MathML to OMML conversion returned empty result.")
    return root


def _make_text_run(text: str) -> OxmlElement:
    run = OxmlElement("w:r")
    text_node = OxmlElement("w:t")
    text_node.text = str(text or "")
    text_node.set(XML_SPACE_ATTR, "preserve")
    run.append(text_node)
    return run


def _formula_marker(segment: dict[str, Any]) -> str:
    latex = str(segment.get("latex") or "")
    if segment.get("type") == "block_formula":
        return f"$${latex}$$"
    return f"${latex}$"


def _append_mixed_content_to_element(paragraph_element, text: str) -> bool:
    for segment in split_text_and_formulas(text):
        segment_type = segment.get("type")
        if segment_type == "text":
            if segment.get("text"):
                paragraph_element.append(_make_text_run(str(segment.get("text") or "")))
            continue

        try:
            mathml = latex_to_mathml_string(str(segment.get("latex") or ""))
            paragraph_element.append(mathml_to_omml(mathml))
        except Exception:
            paragraph_element.append(_make_text_run(_formula_marker(segment)))
    return True


def append_mixed_content_to_paragraph(paragraph, text: str) -> bool:
    try:
        return _append_mixed_content_to_element(paragraph._element, text)
    except Exception:
        try:
            paragraph._element.append(_make_text_run(str(text or "")))
        except Exception:
            return False
    return True


def build_paragraph_with_mixed_content(text: str) -> OxmlElement:
    paragraph_element = OxmlElement("w:p")
    _append_mixed_content_to_element(paragraph_element, text)
    return paragraph_element


def replace_paragraph_text_with_mixed_content(paragraph, full_text: str) -> bool:
    paragraph_element = paragraph._element
    try:
        for child in list(paragraph_element):
            if child.tag != qn("w:pPr"):
                paragraph_element.remove(child)
        return _append_mixed_content_to_element(paragraph_element, full_text)
    except Exception:
        try:
            for child in list(paragraph_element):
                if child.tag != qn("w:pPr"):
                    paragraph_element.remove(child)
            paragraph_element.append(_make_text_run(str(full_text or "")))
        except Exception:
            return False
    return True
