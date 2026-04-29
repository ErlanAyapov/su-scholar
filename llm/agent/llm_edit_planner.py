from __future__ import annotations

import re
from typing import Any

from llm.mdpi_template_extractor import FALLBACK_MDPI_PROMPT_BLOCK, get_extractor, is_mdpi_request

from .llm_core import LlmJsonClient


SYSTEM_PROMPT = """
You are an academic document editor.
You receive document text and a user edit request.
Return JSON only with an edit plan.

Rules:
- op=insert: add a new paragraph after index `after_paragraph` (0-based).
- op=replace: replace `old_text` with `new_text` inside `paragraph` (0-based).
  `old_text` must exist literally in that paragraph, otherwise operation will be skipped.
- op=delete_paragraph: delete whole paragraph by `paragraph` (0-based).
- Do not invent facts not present in source unless user explicitly asks to insert new text.
- When writing formulas, use LaTeX markers only.
- Inline formulas must be wrapped as $...$.
- Display formulas must be wrapped as $$...$$.
- Do not output OMML, XML, MathML, or code fences for formulas.
- If no changes needed, return empty `operations`.
- Return only valid JSON.
""".strip()


REPLACE_PATTERN = re.compile(
    '(?:\u0437\u0430\u043c\u0435\u043d\u0438(?:\u0442\u044c)?|replace)\\s+["“](?P<old>.+?)["”]\\s+'
    '(?:\u043d\u0430|to)\\s+["“](?P<new>.+?)["”]',
    re.IGNORECASE | re.DOTALL,
)
DELETE_PARAGRAPH_PATTERN = re.compile(
    "(?:\u0443\u0434\u0430\u043b\u0438(?:\u0442\u044c)?|delete)\\s+"
    "(?:\u0430\u0431\u0437\u0430\u0446|paragraph)\\s*(?P<index>\\d+)",
    re.IGNORECASE,
)
INSERT_PATTERN = re.compile(
    '(?:\u0434\u043e\u0431\u0430\u0432(?:\u044c|\u0438\u0442\u044c)?|'
    '\u0432\u0441\u0442\u0430\u0432(?:\u044c|\u0438\u0442\u044c)?|append|insert)\\s+["“](?P<text>.+?)["”]',
    re.IGNORECASE | re.DOTALL,
)
AFTER_PARAGRAPH_PATTERN = re.compile(
    "(?:\u043f\u043e\u0441\u043b\u0435|after)\\s+"
    "(?:\u0430\u0431\u0437\u0430\u0446\u0430|\u0430\u0431\u0437\u0430\u0446|paragraph)?\\s*(?P<index>\\d+)",
    re.IGNORECASE,
)


def _coerce_int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if parsed < 0.0:
        return 0.0
    if parsed > 1.0:
        return 1.0
    return parsed


def _paragraph_text(paragraph: Any) -> str:
    if isinstance(paragraph, dict):
        return str(paragraph.get("text") or "")
    return str(paragraph or "")


def format_doc_for_llm(paragraphs: list[Any]) -> str:
    lines: list[str] = []
    for fallback_index, paragraph in enumerate(paragraphs or []):
        if isinstance(paragraph, dict):
            index = _coerce_int(paragraph.get("index"), default=fallback_index)
            style = str(paragraph.get("style") or "Normal").strip() or "Normal"
            text = _paragraph_text(paragraph).strip()
            if text:
                lines.append(f"[{index}] ({style}) {text[:800]}")
            else:
                lines.append(f"[{index}] ({style}) [empty paragraph]")
        else:
            text = _paragraph_text(paragraph).strip()
            if text:
                lines.append(f"[{fallback_index}] {text[:800]}")
            else:
                lines.append(f"[{fallback_index}] [empty paragraph]")
    return "\n".join(lines)


class LlmEditPlanner:
    def __init__(self, llm_client: LlmJsonClient):
        self.llm_client = llm_client

    @staticmethod
    def _normalize_operations(raw_operations: Any) -> list[dict]:
        if not isinstance(raw_operations, list):
            return []

        operations: list[dict] = []
        for raw_operation in raw_operations[:40]:
            if not isinstance(raw_operation, dict):
                continue

            op_name = str(raw_operation.get("op") or "").strip().lower()

            # replace — имя поля paragraph ИЛИ paragraph_index
            if op_name == "replace":
                paragraph_index = _coerce_int(
                    raw_operation.get("paragraph_index") 
                    if raw_operation.get("paragraph_index") is not None 
                    else raw_operation.get("paragraph")
                )
                old_text = str(raw_operation.get("old_text") or "")
                new_text = str(raw_operation.get("new_text") or "")
                if paragraph_index < 0 or not old_text.strip():
                    continue
                operations.append({
                    "op": "replace",
                    "paragraph_index": paragraph_index,  # единое имя
                    "old_text": old_text,
                    "new_text": new_text,
                })

            # insert / insert_after
            elif op_name in {"insert", "insert_after"}:
                after_paragraph = _coerce_int(
                    raw_operation.get("paragraph_index")
                    if raw_operation.get("paragraph_index") is not None
                    else raw_operation.get("after_paragraph")
                    if raw_operation.get("after_paragraph") is not None
                    else raw_operation.get("paragraph")
                )
                text = str(raw_operation.get("new_text") or raw_operation.get("text") or "")
                if after_paragraph < 0 or not text.strip():
                    continue
                operations.append({
                    "op": "insert_after",
                    "paragraph_index": after_paragraph,
                    "new_text": text,
                })

            # delete / delete_paragraph
            elif op_name in {"delete", "delete_paragraph"}:
                paragraph_index = _coerce_int(
                    raw_operation.get("paragraph_index")
                    if raw_operation.get("paragraph_index") is not None
                    else raw_operation.get("paragraph")
                )
                if paragraph_index < 0:
                    continue
                operations.append({
                    "op": "delete",
                    "paragraph_index": paragraph_index,
                })

        return operations
    @staticmethod
    def _heuristic_operations(user_prompt: str, paragraphs: list[Any]) -> list[dict]:
        prompt = str(user_prompt or "").strip()
        if not prompt:
            return []

        operations: list[dict] = []

        replace_match = REPLACE_PATTERN.search(prompt)
        if replace_match:
            old_text = (replace_match.group("old") or "").strip()
            new_text = (replace_match.group("new") or "").strip()
            if old_text:
                paragraph_index = -1
                for idx, paragraph in enumerate(paragraphs or []):
                    if old_text in _paragraph_text(paragraph):
                        paragraph_index = idx
                        break
                if paragraph_index >= 0:
                    operations.append(
                        {
                            "op": "replace",
                            "paragraph": paragraph_index,
                            "old_text": old_text,
                            "new_text": new_text,
                        }
                    )

        delete_match = DELETE_PARAGRAPH_PATTERN.search(prompt)
        if delete_match:
            raw_index = _coerce_int(delete_match.group("index"), default=-1)
            if raw_index > 0:
                operations.append(
                    {
                        "op": "delete_paragraph",
                        "paragraph": raw_index - 1,
                    }
                )

        insert_match = INSERT_PATTERN.search(prompt)
        if insert_match:
            insert_text = (insert_match.group("text") or "").strip()
            if insert_text:
                after_index = max(0, len(paragraphs or []) - 1)
                after_match = AFTER_PARAGRAPH_PATTERN.search(prompt)
                if after_match:
                    raw_index = _coerce_int(after_match.group("index"), default=-1)
                    if raw_index > 0:
                        after_index = raw_index - 1
                operations.append(
                    {
                        "op": "insert",
                        "after_paragraph": after_index,
                        "text": insert_text,
                    }
                )

        return operations

    def plan(self, user_prompt: str, paragraphs: list[Any], active_context: str = "") -> dict:
        # schema_hint = """
        # {
        #     "operations": [
        #         {
        #             "op": "insert | replace | delete_paragraph",
        #             "after_paragraph": 0,
        #             "paragraph": 0,
        #             "old_text": "string",
        #             "new_text": "string",
        #             "text": "string"
        #         }
        #     ],
        #     "summary": "string",
        #     "confidence": 0.0
        # }
        # """.strip()
        schema_hint = """
            {
            "summary": "string",
            "confidence": 0.0,
            "operations": [
                {
                "op": "replace",
                "paragraph_index": 2,
                "old_text": "точный существующий текст абзаца",
                "new_text": "новый текст абзаца",
                "reason": "string"
                },
                {
                "op": "insert_after",
                "paragraph_index": 4,
                "new_text": "текст нового абзаца",
                "reason": "string"
                },
                {
                "op": "delete",
                "paragraph_index": 7,
                "reason": "string"
                }
            ]
            }
            """

        system_prompt = """
            You are a precise academic document editor.
            You receive a DOCUMENT SNAPSHOT where each paragraph is labeled [index].
            Rules for operations:
            - Use "replace" to change existing paragraph text. Always provide exact old_text.
            - Use "insert_after" to add new paragraph after given index.
            - Use "delete" to remove a paragraph by index.
            - paragraph_index must reference an existing index from the snapshot.
            - old_text must match the existing text exactly (used for verification before applying).
            - Never invent paragraph indices that don't exist in the snapshot.
            - Prefer "replace" over delete+insert when modifying existing content.
            - When writing formulas, use LaTeX markers only.
            - Inline formulas must be wrapped as $...$.
            - Display formulas must be wrapped as $$...$$.
            - Do not output OMML, XML, MathML, or code fences for formulas.
            - Return only the minimal set of operations needed.
            """

        mdpi_requested = is_mdpi_request(user_prompt)
        mdpi_user_content = ""
        if mdpi_requested:
            extractor = get_extractor()
            section_guidance = ""
            if extractor is not None and extractor.is_available():
                template_prompt = extractor.system_prompt_block()
                section_guidance = extractor.detect_section_guidance(user_prompt)
            else:
                template_prompt = FALLBACK_MDPI_PROMPT_BLOCK
            system_prompt = f"{system_prompt.strip()}\n\n{template_prompt}"
            mdpi_user_content = (
                "\n\nRequested format:\n"
                "MDPI / Micromachines manuscript style\n\n"
                "Section guidance:\n"
                f"{section_guidance or 'No single target section detected; preserve existing structure unless restructuring is requested.'}\n\n"
                "Template guidance:\n"
                f"{template_prompt[:2500]}"
            )

        document_payload = format_doc_for_llm(paragraphs)
        user_content = (
            "User request:\n"
            f"{user_prompt}\n\n"
            "Active context:\n"
            f"{(active_context or '').strip()[:8000]}\n\n"
            "Document paragraphs (numbered):\n"
            f"{document_payload or '[empty document]'}"
            f"{mdpi_user_content}"
        )

        result: dict[str, Any] | None = None
        error_text = ""
        try:
            llm_result = self.llm_client.send_json_request(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                schema_hint=schema_hint,
                temperature=0.1,
            )
            if isinstance(llm_result, dict):
                result = llm_result
            else:
                result = {}
        except Exception as exc:
            error_text = str(exc)
            result = {}

        summary = str((result or {}).get("summary") or "").strip()
        normalized_operations = self._normalize_operations((result or {}).get("operations"))
        confidence = _coerce_confidence((result or {}).get("confidence"))

        planner_source = "llm"
        if not normalized_operations:
            heuristic_ops = self._heuristic_operations(user_prompt, paragraphs)
            if heuristic_ops:
                normalized_operations = heuristic_ops
                confidence = max(confidence, 0.55)
                planner_source = "heuristic"
                if not summary:
                    summary = "Edit plan was built with heuristic fallback because LLM returned no applicable operations."

        if not summary:
            if normalized_operations:
                summary = f"Planned operations: {len(normalized_operations)}."
            else:
                if error_text:
                    summary = "Failed to build edit plan due to LLM error."
                else:
                    summary = "No document changes are required."

        return {
            "operations": normalized_operations,
            "summary": summary,
            "confidence": confidence,
            "planner_source": planner_source,
            "planner_error": error_text,
            "raw_result": result or {},
        }
