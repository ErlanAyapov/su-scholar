from typing import Any

from .utils import (
    build_empty_payload,
    dedupe_dicts,
    dedupe_strings,
    ensure_payload_shape,
    get_source_priority,
    is_publisher_like_url,
    is_scholar_noise_url,
    merge_string_lists,
    normalized_name_key,
)


def _sorted_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [ensure_payload_shape(payload) for payload in payloads if payload],
        key=lambda payload: get_source_priority(payload.get("source_meta", {}).get("source_type", "")),
        reverse=True,
    )


def _best_scalar(payloads: list[dict[str, Any]], path: tuple[str, ...], *, validator=None):
    for payload in payloads:
        value = payload
        for key in path:
            if not isinstance(value, dict):
                value = ""
                break
            value = value.get(key)
        if value in (None, "", []):
            continue
        if validator and not validator(value):
            continue
        return value
    return "" if path[-1] not in {"year", "total_pages", "quartile_year", "citations_count"} else None


def _best_numeric(payloads: list[dict[str, Any]], path: tuple[str, ...]):
    values = []
    for payload in payloads:
        value = payload
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if isinstance(value, int):
            values.append(value)
    return max(values) if values else None


def _abstract_score(payload: dict[str, Any]) -> tuple[int, int]:
    publication = payload.get("publication", {})
    abstract = (publication.get("abstract") or "").strip()
    if not abstract:
        return (-1, -1)
    priority = get_source_priority(payload.get("source_meta", {}).get("source_type", ""))
    penalty = 15 if publication.get("needs_review") else 0
    return (priority - penalty, len(abstract))


def _merge_authors(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base: list[dict[str, Any]] = []
    for payload in payloads:
        authors = payload.get("authors") or []
        if authors:
            base = [dict(author) for author in authors]
            break

    if not base:
        return []

    seen = {normalized_name_key(author.get("full_name") or author.get("raw_name") or "") for author in base}
    for payload in payloads:
        authors = payload.get("authors") or []
        for source_author in authors:
            source_key = normalized_name_key(source_author.get("full_name") or source_author.get("raw_name") or "")
            if not source_key:
                continue
            target = next(
                (
                    author
                    for author in base
                    if normalized_name_key(author.get("full_name") or author.get("raw_name") or "") == source_key
                ),
                None,
            )
            if target is None:
                if source_key not in seen:
                    base.append(dict(source_author))
                    seen.add(source_key)
                continue

            for key in ("raw_name", "orcid", "affiliations"):
                if not target.get(key) and source_author.get(key):
                    target[key] = source_author[key]
            if source_author.get("role") == "corresponding" and target.get("role") != "corresponding":
                target["role"] = "corresponding"

    return base


def _merge_identifiers(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for payload in payloads:
        for item in payload.get("identifiers") or []:
            items.append(
                {
                    "id_type": (item.get("id_type") or "").strip(),
                    "value": (item.get("value") or "").strip(),
                }
            )
    return dedupe_dicts(items, lambda item: ((item.get("id_type") or "").lower(), (item.get("value") or "").lower()))


def _merge_metrics(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for payload in payloads:
        items.extend(payload.get("venue_metrics") or [])
    return dedupe_dicts(
        items,
        lambda item: ((item.get("metric") or "").lower(), item.get("year"), str(item.get("value") or "").strip()),
    )


def _merge_projects(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for payload in payloads:
        items.extend(payload.get("projects") or [])
    return dedupe_dicts(
        items,
        lambda item: (
            (item.get("name") or "").strip().lower(),
            (item.get("contract_number") or "").strip().lower(),
        ),
    )


def _merge_object_links(payloads: list[dict[str, Any]], field_name: str) -> list[dict[str, Any]]:
    items = []
    for payload in payloads:
        for item in payload.get("links", {}).get(field_name) or []:
            if isinstance(item, dict):
                items.append({"url": item.get("url", ""), "label": item.get("label", "")})
    return dedupe_dicts(items, lambda item: (item.get("url") or "").lower())


def merge_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    sorted_payloads = _sorted_payloads(payloads)
    if not sorted_payloads:
        return build_empty_payload()

    result = build_empty_payload()
    best_source = sorted_payloads[0]

    result["publication"]["title_original"] = _best_scalar(sorted_payloads, ("publication", "title_original"))
    result["publication"]["pub_type"] = _best_scalar(sorted_payloads, ("publication", "pub_type"))
    result["publication"]["language"] = _best_scalar(sorted_payloads, ("publication", "language"))
    result["publication"]["year"] = _best_numeric(sorted_payloads, ("publication", "year"))
    result["publication"]["publication_date"] = _best_scalar(sorted_payloads, ("publication", "publication_date"))
    result["publication"]["status"] = _best_scalar(sorted_payloads, ("publication", "status")) or "published"
    result["publication"]["accepted_date"] = _best_scalar(sorted_payloads, ("publication", "accepted_date"))
    result["publication"]["volume"] = _best_scalar(sorted_payloads, ("publication", "volume"))
    result["publication"]["issue"] = _best_scalar(sorted_payloads, ("publication", "issue"))
    result["publication"]["pages"] = _best_scalar(sorted_payloads, ("publication", "pages"))
    result["publication"]["article_number"] = _best_scalar(sorted_payloads, ("publication", "article_number"))
    result["publication"]["total_pages"] = _best_numeric(sorted_payloads, ("publication", "total_pages"))
    result["publication"]["doi"] = _best_scalar(sorted_payloads, ("publication", "doi"))
    result["publication"]["abstract"] = max(
        sorted_payloads,
        key=_abstract_score,
    ).get("publication", {}).get("abstract", "")
    result["publication"]["keywords"] = merge_string_lists(
        *[payload.get("publication", {}).get("keywords") or [] for payload in sorted_payloads]
    )
    result["publication"]["candidate_keywords"] = merge_string_lists(
        *[payload.get("publication", {}).get("candidate_keywords") or [] for payload in sorted_payloads]
    )
    result["publication"]["quartile"] = _best_scalar(sorted_payloads, ("publication", "quartile"))
    result["publication"]["quartile_year"] = _best_numeric(sorted_payloads, ("publication", "quartile_year"))
    result["publication"]["citations_count"] = _best_numeric(sorted_payloads, ("publication", "citations_count"))
    result["publication"]["open_access"] = any(
        bool(payload.get("publication", {}).get("open_access")) for payload in sorted_payloads
    )
    result["publication"]["oa_type"] = _best_scalar(sorted_payloads, ("publication", "oa_type"))
    result["publication"]["report_period"] = _best_scalar(sorted_payloads, ("publication", "report_period"))
    result["publication"]["needs_review"] = any(
        bool(payload.get("publication", {}).get("needs_review")) for payload in sorted_payloads
    )

    for field_name in result["venue"].keys():
        result["venue"][field_name] = _best_scalar(sorted_payloads, ("venue", field_name)) or result["venue"][field_name]

    result["authors"] = _merge_authors(sorted_payloads)
    result["indexing"] = dedupe_strings(
        [item for payload in sorted_payloads for item in (payload.get("indexing") or [])]
    )
    result["tags"] = dedupe_strings([item for payload in sorted_payloads for item in (payload.get("tags") or [])])
    result["area"] = _best_scalar(sorted_payloads, ("area",))
    result["identifiers"] = _merge_identifiers(sorted_payloads)
    result["venue_metrics"] = _merge_metrics(sorted_payloads)
    result["projects"] = _merge_projects(sorted_payloads)

    result["links"]["url_publisher"] = _best_scalar(
        sorted_payloads,
        ("links", "url_publisher"),
        validator=is_publisher_like_url,
    )
    result["links"]["url_open_access"] = _best_scalar(sorted_payloads, ("links", "url_open_access"))
    result["links"]["doi_url"] = _best_scalar(sorted_payloads, ("links", "doi_url"))
    result["links"]["pdf_url"] = _best_scalar(sorted_payloads, ("links", "pdf_url"))
    html_url = _best_scalar(sorted_payloads, ("links", "html_url"))
    result["links"]["html_url"] = "" if is_scholar_noise_url(str(html_url)) else html_url
    result["links"]["scholar_url"] = _best_scalar(sorted_payloads, ("links", "scholar_url"))
    result["links"]["related_urls"] = dedupe_strings(
        [item for payload in sorted_payloads for item in (payload.get("links", {}).get("related_urls") or [])]
    )
    result["links"]["repository_links"] = _merge_object_links(sorted_payloads, "repository_links")
    result["links"]["supplementary_links"] = _merge_object_links(sorted_payloads, "supplementary_links")

    result["source_meta"] = dict(best_source.get("source_meta", {}))
    result["source_meta"]["source_type"] = best_source.get("source_meta", {}).get("source_type", "")
    result["source_meta"]["source_url"] = (
        best_source.get("source_meta", {}).get("source_url")
        or best_source.get("links", {}).get("url_publisher")
        or best_source.get("source_meta", {}).get("source_url")
    )

    if not result["links"]["url_publisher"] and is_publisher_like_url(result["links"]["html_url"]):
        result["links"]["url_publisher"] = result["links"]["html_url"]

    return result
