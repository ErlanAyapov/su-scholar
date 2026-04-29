from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from .formula_utils import (
    build_paragraph_with_mixed_content,
    has_latex_formula_markers,
    replace_paragraph_text_with_mixed_content,
)


AUTHOR = "AI Assistant"
XML_SPACE_ATTR = "{http://www.w3.org/XML/1998/namespace}space"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond =0).isoformat().replace("+00:00", "Z")


class DocumentEditor:
    def __init__(self, docx_path: str, *, author: str = AUTHOR, change_date: str | None = None):
        self.doc = Document(docx_path)
        self.docx_path = docx_path
        self.author = author or AUTHOR
        self.change_date = change_date or _utc_now_iso()
        self._next_id = self._compute_next_change_id()

    def _compute_next_change_id(self) -> int:
        max_id = 0
        for element in self.doc.element.body.iter():
            raw_value = element.get(qn("w:id"))
            if raw_value is None:
                continue
            try:
                parsed = int(str(raw_value))
            except (TypeError, ValueError):
                continue
            if parsed > max_id:
                max_id = parsed
        return max_id + 1

    def _next_change_id(self) -> int:
        current = self._next_id
        self._next_id += 1
        return current

    @staticmethod
    def _coerce_index(raw_value: Any) -> int:
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            return -1

    @staticmethod
    def _operation_name(operation: dict) -> str:
        return str(operation.get("op") or "").strip().lower()

    def _operation_index(self, operation: dict) -> int:
        if operation.get("paragraph_index") is not None:
            return self._coerce_index(operation.get("paragraph_index"))
        if operation.get("paragraph") is not None:
            return self._coerce_index(operation.get("paragraph"))
        return self._coerce_index(operation.get("after_paragraph"))

    @staticmethod
    def _normalize_text(text: str) -> str:
        return " ".join(str(text or "").split()).strip()

    @classmethod
    def _resolve_replacement(cls, full_text: str, old_text: str, new_text: str) -> tuple[str, str] | None:
        if old_text in full_text:
            return old_text, new_text

        normalized_full = cls._normalize_text(full_text)
        normalized_old = cls._normalize_text(old_text)
        if normalized_old and normalized_old in normalized_full:
            return full_text, normalized_full.replace(normalized_old, new_text, 1)

        old_words = [word for word in normalized_old.split(" ") if len(word) > 3]
        if not old_words:
            return None

        normalized_full_lower = normalized_full.lower()
        match_count = sum(1 for word in old_words if word.lower() in normalized_full_lower)
        if match_count / len(old_words) >= 0.8:
            return full_text, new_text
        return None

    @staticmethod
    def _make_run(text: str, *, deleted: bool = False) -> OxmlElement:
        run = OxmlElement("w:r")
        text_node = OxmlElement("w:delText" if deleted else "w:t")
        text_node.text = text or ""
        text_node.set(XML_SPACE_ATTR, "preserve")
        run.append(text_node)
        return run

    # def apply_operations(self, operations: list[dict]) -> int:
    #     """
    #     Apply operations in reverse paragraph order so paragraph indices stay stable.
    #     Returns count of applied operations.
    #     """
    #     if not isinstance(operations, list):
    #         return 0

    #     def op_index(operation: dict) -> int:
    #         if not isinstance(operation, dict):
    #             return -1
    #         if "paragraph" in operation:
    #             return self._coerce_index(operation.get("paragraph"))
    #         return self._coerce_index(operation.get("after_paragraph"))

    #     sorted_operations = sorted(operations, key=op_index, reverse=True)
    #     applied = 0

    #     for operation in sorted_operations:
    #         if not isinstance(operation, dict):
    #             continue
    #         try:
    #             op_name = str(operation.get("op") or "").strip().lower()
    #             changed = False
    #             if op_name == "insert":
    #                 changed = self._apply_insert(operation)
    #             elif op_name == "replace":
    #                 changed = self._apply_replace(operation)
    #             elif op_name == "delete_paragraph":
    #                 changed = self._apply_delete_paragraph(operation)
    #             if changed:
    #                 applied += 1
    #         except Exception as exc:  # noqa: BLE001
    #             print(f"[DocumentEditor] skipped operation={operation}: {exc}")
    #     return applied
    def _legacy_apply_operations(self, operations: list[dict]) -> int:
        doc = self.doc
        paragraphs = doc.paragraphs
        applied = 0

        # Сортируем в обратном порядке — чтобы удаления/вставки не сбивали индексы
        sorted_ops = sorted(
            operations,
            key=lambda op: int(op.get("paragraph_index", 0)),
            reverse=True,
        )

        for op in sorted_ops:
            op_type = str(op.get("op") or "").strip().lower()
            idx = int(op.get("paragraph_index", -1))

            if op_type == "replace":
                if idx < 0 or idx >= len(paragraphs):
                    continue
                para = paragraphs[idx]
                old_text = str(op.get("old_text") or "").strip()
                new_text = str(op.get("new_text") or "").strip()
                # Верификация: old_text должен совпадать
                if old_text and old_text not in para.text:
                    continue  # не применяем если текст уже изменился
                # Сохраняем форматирование первого run
                if para.runs:
                    para.runs[0].text = new_text
                    for run in para.runs[1:]:
                        run.text = ""
                else:
                    para.text = new_text  # fallback
                applied += 1

            elif op_type == "insert_after":
                if idx < 0 or idx >= len(paragraphs):
                    continue
                new_text = str(op.get("new_text") or "").strip()
                # Вставляем новый параграф после указанного
                ref_para = paragraphs[idx]._element
                new_para = OxmlElement("w:p")
                r = OxmlElement("w:r")
                t = OxmlElement("w:t")
                t.text = new_text
                r.append(t)
                new_para.append(r)
                ref_para.addnext(new_para)
                applied += 1

            elif op_type == "delete":
                if idx < 0 or idx >= len(paragraphs):
                    continue
                para_el = paragraphs[idx]._element
                para_el.getparent().remove(para_el)
                applied += 1

        return applied

    def _apply_insert(self, operation: dict) -> bool:
        index = self._operation_index(operation)
        text = str(operation.get("text") or operation.get("new_text") or "")
        if not text.strip():
            return False

        has_formulas = has_latex_formula_markers(text)
        paragraphs = self.doc.paragraphs
        if not paragraphs:
            paragraph = self.doc.add_paragraph("" if has_formulas else text)
            if has_formulas:
                replace_paragraph_text_with_mixed_content(paragraph, text)
            return True

        if index < 0 or index >= len(paragraphs):
            paragraph = self.doc.add_paragraph("" if has_formulas else text)
            if has_formulas:
                replace_paragraph_text_with_mixed_content(paragraph, text)
            return True

        target_paragraph = paragraphs[index]._element

        if has_formulas:
            new_paragraph = build_paragraph_with_mixed_content(text)
        else:
            new_paragraph = OxmlElement("w:p")
            new_paragraph.append(self._make_run(text))
        target_paragraph.addnext(new_paragraph)
        return True
    
    # ИСПРАВЛЕНИЕ — брать full_text из XML напрямую, очищать только то что восстановим
    def _apply_replace(self, operation: dict) -> bool:
        index = self._operation_index(operation)
        old_text = str(operation.get("old_text") or "")
        new_text = str(operation.get("new_text") or "")

        if index < 0 or not old_text.strip():
            return False

        paragraphs = self.doc.paragraphs
        if index >= len(paragraphs):
            return False

        paragraph = paragraphs[index]
        full_text = paragraph.text or ""
        replacement = self._resolve_replacement(full_text, old_text, new_text)
        if replacement is None:
            return False

        replace_target, new_fragment = replacement
        replacement_text = (
            new_fragment
            if replace_target == full_text
            else full_text.replace(replace_target, new_fragment, 1)
        )
        if has_latex_formula_markers(replacement_text):
            if replace_paragraph_text_with_mixed_content(paragraph, replacement_text):
                return True
            paragraph.text = replacement_text
            return True

        paragraph.text = replacement_text
        return True

    # def _apply_replace(self, operation: dict) -> bool:
    #     index = self._coerce_index(operation.get("paragraph"))
    #     old_text = str(operation.get("old_text") or "")
    #     new_text = str(operation.get("new_text") or "")

    #     if index < 0 or not old_text.strip():
    #         return False

    #     paragraphs = self.doc.paragraphs
    #     if index >= len(paragraphs):
    #         return False

    #     paragraph = paragraphs[index]
    #     full_text = paragraph.text or ""
    #     if old_text not in full_text:
    #         return False

    #     before, _, after = full_text.partition(old_text)
    #     paragraph_element = paragraph._element

    #     removable_tags = {
    #         qn("w:r"),
    #         qn("w:ins"),
    #         qn("w:del"),
    #         qn("w:hyperlink"),
    #         qn("w:sdt"),
    #     }
    #     for child in list(paragraph_element):
    #         if child.tag in removable_tags:
    #             paragraph_element.remove(child)

    #     if before:
    #         paragraph_element.append(self._make_run(before))

    #     deleted_element = OxmlElement("w:del")
    #     deleted_element.set(qn("w:id"), str(self._next_change_id()))
    #     deleted_element.set(qn("w:author"), self.author)
    #     deleted_element.set(qn("w:date"), self.change_date)
    #     deleted_element.append(self._make_run(old_text, deleted=True))
    #     paragraph_element.append(deleted_element)

    #     if new_text:
    #         inserted_element = OxmlElement("w:ins")
    #         inserted_element.set(qn("w:id"), str(self._next_change_id()))
    #         inserted_element.set(qn("w:author"), self.author)
    #         inserted_element.set(qn("w:date"), self.change_date)
    #         inserted_element.append(self._make_run(new_text))
    #         paragraph_element.append(inserted_element)

    #     if after:
    #         paragraph_element.append(self._make_run(after))
    #     return True

    def _apply_delete_paragraph(self, operation: dict) -> bool:
        index = self._operation_index(operation)
        if index < 0:
            return False

        paragraphs = self.doc.paragraphs
        if index >= len(paragraphs):
            return False

        paragraph_element = paragraphs[index]._element
        parent = paragraph_element.getparent()
        if parent is None:
            return False
        parent.remove(paragraph_element)
        return True

    def apply_operations(self, operations: list[dict]) -> int:
        if not isinstance(operations, list):
            return 0

        valid_operations = [operation for operation in operations if isinstance(operation, dict)]
        delete_ops = sorted(
            [
                operation
                for operation in valid_operations
                if self._operation_name(operation) in {"delete", "delete_paragraph"}
            ],
            key=self._operation_index,
            reverse=True,
        )
        replace_ops = sorted(
            [
                operation
                for operation in valid_operations
                if self._operation_name(operation) == "replace"
            ],
            key=self._operation_index,
            reverse=True,
        )
        insert_ops = sorted(
            [
                operation
                for operation in valid_operations
                if self._operation_name(operation) in {"insert", "insert_after"}
            ],
            key=self._operation_index,
        )

        applied = 0
        for operation in delete_ops + replace_ops + insert_ops:
            try:
                op_name = self._operation_name(operation)
                changed = False
                if op_name in {"insert", "insert_after"}:
                    changed = self._apply_insert(operation)
                elif op_name == "replace":
                    changed = self._apply_replace(operation)
                elif op_name in {"delete", "delete_paragraph"}:
                    changed = self._apply_delete_paragraph(operation)
                if changed:
                    applied += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[DocumentEditor] skipped operation={operation}: {exc}")
        return applied

    def save(self, output_path: str | None = None) -> str:
        path = output_path or self.docx_path
        self.doc.save(path)
        return path
