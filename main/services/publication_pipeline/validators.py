from typing import Any

from .utils import (
    VALID_AUTHOR_ROLES,
    VALID_IDENTIFIER_TYPES,
    VALID_METRICS,
    VALID_OA_TYPES,
    VALID_PROJECT_TYPES,
    VALID_SOURCE_TYPES,
    VALID_STATUSES,
    VALID_VENUE_CHARACTERS,
    VALID_VENUE_KINDS,
    build_empty_payload,
    ensure_payload_shape,
    int_or_none,
)


def _build_safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe_payload = ensure_payload_shape(payload)
    safe_payload["authors"] = []
    safe_payload["projects"] = []
    safe_payload["venue_metrics"] = []
    safe_payload["links"]["repository_links"] = []
    safe_payload["links"]["supplementary_links"] = []
    safe_payload["tags"] = []
    return safe_payload


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = ensure_payload_shape(payload)
    errors: list[str] = []
    warnings: list[str] = []

    publication = normalized["publication"]
    venue = normalized["venue"]
    authors = normalized["authors"]
    identifiers = normalized["identifiers"]
    metrics = normalized["venue_metrics"]
    projects = normalized["projects"]
    source_meta = normalized["source_meta"]

    if not publication.get("title_original"):
        errors.append("publication.title_original is required")

    if not publication.get("year") and not publication.get("publication_date"):
        errors.append("Either publication.year or publication.publication_date is required")

    if not isinstance(authors, list):
        errors.append("authors must be a list")
    else:
        for index, author in enumerate(authors, start=1):
            if not isinstance(author, dict):
                errors.append(f"authors[{index}] must be an object")
                continue
            if int_or_none(author.get("order")) is None:
                errors.append(f"authors[{index}].order is required")
            if author.get("role") not in VALID_AUTHOR_ROLES:
                errors.append(f"authors[{index}].role is invalid")

    if venue.get("kind") not in VALID_VENUE_KINDS:
        errors.append("venue.kind is invalid")
    if venue.get("character") not in VALID_VENUE_CHARACTERS:
        errors.append("venue.character is invalid")
    if publication.get("status") not in VALID_STATUSES:
        errors.append("publication.status is invalid")
    if publication.get("oa_type") not in VALID_OA_TYPES:
        errors.append("publication.oa_type is invalid")
    if source_meta.get("source_type") not in VALID_SOURCE_TYPES:
        errors.append("source_meta.source_type is invalid")

    for index, identifier in enumerate(identifiers, start=1):
        if identifier.get("id_type") not in VALID_IDENTIFIER_TYPES:
            errors.append(f"identifiers[{index}].id_type is invalid")
        if not identifier.get("value"):
            errors.append(f"identifiers[{index}].value is required")

    for index, metric in enumerate(metrics, start=1):
        if metric.get("metric") not in VALID_METRICS:
            errors.append(f"venue_metrics[{index}].metric is invalid")
        if int_or_none(metric.get("year")) is None:
            errors.append(f"venue_metrics[{index}].year is required")
        if metric.get("value") in (None, ""):
            errors.append(f"venue_metrics[{index}].value is required")

    for index, project in enumerate(projects, start=1):
        if project.get("project_type") not in VALID_PROJECT_TYPES:
            errors.append(f"projects[{index}].project_type is invalid")
        if not project.get("name"):
            errors.append(f"projects[{index}].name is required")

    if publication.get("needs_review"):
        warnings.append("publication marked as needs_review")

    if publication.get("abstract") and len(publication.get("abstract", "").split()) < 20:
        warnings.append("abstract looks short and may be snippet-like")

    return {
        "is_valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "payload": normalized,
        "safe_payload": _build_safe_payload(normalized),
    }
