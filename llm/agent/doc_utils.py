from __future__ import annotations

from pathlib import Path
from typing import Any

from docx import Document as DocxDocument

from .document_editor import DocumentEditor


def _safe_int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_default_output_path(docx_path: str | Path) -> str:
    source = Path(docx_path)
    return str(source.with_name(f"{source.stem}-corrected{source.suffix or '.docx'}"))


def _resolve_replace_paragraph_index(
    paragraphs: list[str],
    old_text: str,
    requested_index: int,
) -> int:
    if requested_index >= 0 and requested_index < len(paragraphs):
        return requested_index
    for idx, paragraph_text in enumerate(paragraphs):
        if old_text and old_text in (paragraph_text or ""):
            return idx
    return -1


def apply_docx_corrections(
    docx_path: str | Path,
    corrections: list[dict],
    *,
    output_path: str | Path | None = None,
    author: str = "AI Assistant",
) -> dict:
    """
    Apply correction operations to DOCX using tracked changes.

    Supported correction formats:
    - {"op": "replace", "old_text": "...", "new_text": "...", "paragraph": 5}
    - {"op": "insert_after", "paragraph": 3, "text": "..."}
    - {"op": "append", "text": "..."}
    - {"op": "delete_paragraph", "paragraph": 7}
    """
    source_path = Path(docx_path)
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"DOCX file not found: {source_path}")

    if source_path.suffix.lower() != ".docx":
        raise ValueError("Only .docx files are supported.")

    if not isinstance(corrections, list):
        raise ValueError("corrections must be a list of dict operations.")

    source_doc = DocxDocument(str(source_path))
    paragraph_texts = [paragraph.text or "" for paragraph in source_doc.paragraphs]

    editor_operations: list[dict] = []
    skipped: list[dict] = []

    for raw_item in corrections:
        if not isinstance(raw_item, dict):
            skipped.append({"operation": raw_item, "reason": "operation must be object"})
            continue

        op_name = str(raw_item.get("op") or "").strip().lower()

        if op_name == "replace":
            old_text = str(raw_item.get("old_text") or "")
            new_text = str(raw_item.get("new_text") or "")
            requested_index = _safe_int(raw_item.get("paragraph"), default=-1)
            resolved_index = _resolve_replace_paragraph_index(paragraph_texts, old_text, requested_index)
            if not old_text.strip() or resolved_index < 0:
                skipped.append({"operation": raw_item, "reason": "replace target not found"})
                continue
            editor_operations.append(
                {
                    "op": "replace",
                    "paragraph": resolved_index,
                    "old_text": old_text,
                    "new_text": new_text,
                }
            )
            continue

        if op_name == "insert_after":
            text = str(raw_item.get("text") or "")
            paragraph_index = _safe_int(raw_item.get("paragraph"), default=-1)
            if not text.strip() or paragraph_index < 0:
                skipped.append({"operation": raw_item, "reason": "insert_after requires paragraph>=0 and text"})
                continue
            editor_operations.append(
                {
                    "op": "insert",
                    "after_paragraph": paragraph_index,
                    "text": text,
                }
            )
            continue

        if op_name == "append":
            text = str(raw_item.get("text") or "")
            if not text.strip():
                skipped.append({"operation": raw_item, "reason": "append requires text"})
                continue
            after_paragraph = max(0, len(paragraph_texts) - 1)
            editor_operations.append(
                {
                    "op": "insert",
                    "after_paragraph": after_paragraph,
                    "text": text,
                }
            )
            paragraph_texts.append(text)
            continue

        if op_name == "delete_paragraph":
            paragraph_index = _safe_int(raw_item.get("paragraph"), default=-1)
            if paragraph_index < 0:
                skipped.append({"operation": raw_item, "reason": "delete_paragraph requires paragraph>=0"})
                continue
            editor_operations.append({"op": "delete_paragraph", "paragraph": paragraph_index})
            continue

        skipped.append({"operation": raw_item, "reason": f"unsupported operation: {op_name}"})

    target_path = Path(output_path) if output_path else Path(_build_default_output_path(source_path))
    target_path.parent.mkdir(parents=True, exist_ok=True)

    editor = DocumentEditor(str(source_path), author=author)
    applied_count = editor.apply_operations(editor_operations)
    editor.save(str(target_path))

    return {
        "source_path": str(source_path),
        "output_path": str(target_path),
        "requested_operations": len(corrections),
        "prepared_operations": len(editor_operations),
        "applied_operations": applied_count,
        "skipped": skipped,
    }


def example_apply_docx_corrections(docx_path: str | Path, output_path: str | Path | None = None) -> dict:
    """
    Example call for apply_docx_corrections().
    """
    sample_corrections = [
        {
            "op": "replace",
            "paragraph": 0,
            "old_text": "IoT",
            "new_text": "Internet of Things",
        },
        {
            "op": "insert_after",
            "paragraph": 1,
            "text": "Additional paragraph inserted by AI Assistant.",
        },
        {
            "op": "append",
            "text": "Final note appended to the end of the document.",
        },
    ]
    return apply_docx_corrections(
        docx_path=docx_path,
        corrections=sample_corrections,
        output_path=output_path,
        author="AI Assistant",
    )
