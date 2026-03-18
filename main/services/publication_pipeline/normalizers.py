from typing import Any

from .author_linking import enrich_author_payload
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
    dedupe_dicts,
    dedupe_strings,
    ensure_list_of_strings,
    ensure_payload_shape,
    extract_isbn,
    extract_issn,
    infer_year,
    int_or_none,
    is_pdf_url,
    is_publisher_like_url,
    is_repository_url,
    is_scholar_noise_url,
    make_absolute_url,
    normalize_doi,
    normalize_language,
    normalize_metric_name,
    normalize_person_name,
    normalize_pub_type,
    normalize_quartile,
    parse_date_to_iso,
)


def _normalize_publication(publication: dict[str, Any], source_meta: dict[str, Any], links: dict[str, Any]) -> dict[str, Any]:
    normalized = build_empty_payload()["publication"]
    normalized.update(publication or {})

    normalized["pub_type"] = normalize_pub_type(normalized.get("pub_type", ""))
    normalized["language"] = normalize_language(normalized.get("language", ""))
    normalized["publication_date"] = parse_date_to_iso(normalized.get("publication_date", ""))
    normalized["accepted_date"] = parse_date_to_iso(normalized.get("accepted_date", ""))
    normalized["year"] = infer_year(
        normalized.get("year"),
        normalized.get("publication_date"),
        source_meta.get("raw_publication_date"),
    )
    normalized["status"] = normalized.get("status") if normalized.get("status") in VALID_STATUSES else "published"
    normalized["doi"] = normalize_doi(normalized.get("doi", ""))
    normalized["keywords"] = ensure_list_of_strings(normalized.get("keywords"))
    normalized["candidate_keywords"] = ensure_list_of_strings(normalized.get("candidate_keywords"))
    normalized["quartile"] = normalize_quartile(normalized.get("quartile", ""))
    normalized["quartile_year"] = int_or_none(normalized.get("quartile_year"))
    if normalized["quartile"] and not normalized["quartile_year"]:
        normalized["quartile_year"] = normalized.get("year")
    normalized["citations_count"] = int_or_none(normalized.get("citations_count"))
    normalized["open_access"] = bool(normalized.get("open_access"))
    normalized["oa_type"] = normalized.get("oa_type") if normalized.get("oa_type") in VALID_OA_TYPES else ""
    normalized["needs_review"] = bool(normalized.get("needs_review"))

    pages = str(normalized.get("pages") or "").strip()
    total_pages = int_or_none(normalized.get("total_pages"))
    if pages.isdigit():
        total_pages = int(pages)
        pages = ""
    normalized["pages"] = pages
    normalized["total_pages"] = total_pages

    if normalized["doi"] and not links.get("doi_url"):
        links["doi_url"] = f"https://doi.org/{normalized['doi']}"

    if source_meta.get("source_type") == "google_scholar" and normalized.get("abstract"):
        normalized["needs_review"] = True

    source_url = (source_meta.get("source_url") or "").lower()
    if source_meta.get("source_type") == "publisher_page" and "mdpi.com" in source_url:
        normalized["open_access"] = True
        normalized["oa_type"] = normalized["oa_type"] or "gold"

    return normalized


def _normalize_venue(venue: dict[str, Any]) -> dict[str, Any]:
    normalized = build_empty_payload()["venue"]
    normalized.update(venue or {})
    normalized["kind"] = normalized.get("kind") if normalized.get("kind") in VALID_VENUE_KINDS else "journal"
    normalized["character"] = (
        normalized.get("character") if normalized.get("character") in VALID_VENUE_CHARACTERS else "scientific_journal"
    )
    normalized["issn"] = extract_issn(normalized.get("issn", "")) or normalized.get("issn", "")
    normalized["isbn"] = extract_isbn(normalized.get("isbn", "")) or normalized.get("isbn", "")
    return normalized


def _normalize_authors(authors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    corresponding_seen = False
    for index, author in enumerate(authors or [], start=1):
        enriched = enrich_author_payload(author)
        full_name = normalize_person_name(enriched.get("full_name") or enriched.get("raw_name") or "")
        if not full_name:
            continue

        role = enriched.get("role") if enriched.get("role") in VALID_AUTHOR_ROLES else ""
        if role == "corresponding":
            corresponding_seen = True
        order = int_or_none(enriched.get("order")) or index

        result.append(
            {
                "full_name": full_name,
                "raw_name": normalize_person_name(enriched.get("raw_name") or full_name),
                "orcid": (enriched.get("orcid") or "").strip(),
                "affiliations": normalize_person_name(enriched.get("affiliations") or ""),
                "is_department_staff": bool(enriched.get("is_department_staff")),
                "order": order,
                "role": role or ("first" if index == 1 else "coauthor"),
                "name_normalized": enriched.get("name_normalized", ""),
                "name_translit": enriched.get("name_translit", ""),
                "name_initials": enriched.get("name_initials", ""),
                "surname": enriched.get("surname", ""),
                "matched_author_id": enriched.get("matched_author_id"),
                "matched_user_id": enriched.get("matched_user_id"),
                "match_confidence": enriched.get("match_confidence"),
                "match_method": enriched.get("match_method", ""),
                "needs_review": bool(enriched.get("needs_review")),
            }
        )

    result.sort(key=lambda item: item.get("order") or 0)
    deduped = dedupe_dicts(result, lambda item: (item.get("full_name") or "").lower())
    if deduped and not any(item.get("role") == "first" for item in deduped):
        deduped[0]["role"] = "first"
    if corresponding_seen:
        for item in deduped:
            if item.get("role") == "first" and item is not deduped[0]:
                item["role"] = "coauthor"
    return deduped


def _normalize_identifiers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    identifiers = []
    for item in payload.get("identifiers") or []:
        id_type = (item.get("id_type") or "").strip().lower()
        value = (item.get("value") or "").strip()
        if not value:
            continue
        if id_type == "doi":
            value = normalize_doi(value)
        if id_type not in VALID_IDENTIFIER_TYPES:
            id_type = "other"
        identifiers.append({"id_type": id_type, "value": value})

    publication_doi = payload.get("publication", {}).get("doi", "")
    if publication_doi:
        identifiers.append({"id_type": "doi", "value": publication_doi})

    venue = payload.get("venue", {})
    if venue.get("issn"):
        identifiers.append({"id_type": "issn", "value": venue["issn"]})
    if venue.get("isbn"):
        identifiers.append({"id_type": "isbn", "value": venue["isbn"]})

    return dedupe_dicts(
        identifiers,
        lambda item: ((item.get("id_type") or "").lower(), (item.get("value") or "").lower()),
    )


def _normalize_links(links: dict[str, Any], base_url: str) -> dict[str, Any]:
    normalized = build_empty_payload()["links"]
    normalized.update(links or {})

    for key in ("url_publisher", "url_open_access", "doi_url", "pdf_url", "html_url", "scholar_url"):
        normalized[key] = make_absolute_url(base_url, normalized.get(key, ""))

    if normalized["html_url"] and is_scholar_noise_url(normalized["html_url"]):
        normalized["html_url"] = ""

    if not normalized["url_publisher"] and is_publisher_like_url(normalized["html_url"]):
        normalized["url_publisher"] = normalized["html_url"]

    normalized["related_urls"] = dedupe_strings(
        [make_absolute_url(base_url, item) for item in (normalized.get("related_urls") or [])]
    )

    repository_links = []
    for item in normalized.get("repository_links") or []:
        url = make_absolute_url(base_url, item.get("url", "") if isinstance(item, dict) else "")
        label = item.get("label", "") if isinstance(item, dict) else ""
        if url and is_repository_url(url):
            repository_links.append({"url": url, "label": label})
    normalized["repository_links"] = dedupe_dicts(repository_links, lambda item: item.get("url", "").lower())

    supplementary_links = []
    for item in normalized.get("supplementary_links") or []:
        url = make_absolute_url(base_url, item.get("url", "") if isinstance(item, dict) else "")
        label = item.get("label", "") if isinstance(item, dict) else ""
        if url:
            supplementary_links.append({"url": url, "label": label})
    normalized["supplementary_links"] = dedupe_dicts(supplementary_links, lambda item: item.get("url", "").lower())

    if normalized["pdf_url"] and not is_pdf_url(normalized["pdf_url"]):
        if normalized["pdf_url"].lower().endswith("pdf") is False:
            normalized["pdf_url"] = normalized["pdf_url"]

    return normalized


def _normalize_metrics(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in items or []:
        metric = normalize_metric_name(item.get("metric") or item.get("metric_type") or "")
        year = int_or_none(item.get("year"))
        value = item.get("value")
        if metric not in VALID_METRICS or year is None or value in (None, ""):
            continue
        normalized.append(
            {
                "metric": metric,
                "year": year,
                "value": value,
                "source": (item.get("source") or "").strip(),
            }
        )
    return dedupe_dicts(normalized, lambda item: (item["metric"], item["year"], str(item["value"])))


def _normalize_projects(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in items or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        project_type = item.get("project_type") if item.get("project_type") in VALID_PROJECT_TYPES else "other"
        normalized.append(
            {
                "name": name,
                "project_type": project_type,
                "contract_number": (item.get("contract_number") or "").strip(),
                "funding_source": (item.get("funding_source") or "").strip(),
            }
        )
    return dedupe_dicts(
        normalized,
        lambda item: ((item.get("name") or "").lower(), (item.get("contract_number") or "").lower()),
    )


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = ensure_payload_shape(payload)
    source_meta = normalized.get("source_meta", {})
    base_url = source_meta.get("source_url", "")

    normalized["source_meta"]["source_type"] = (
        source_meta.get("source_type") if source_meta.get("source_type") in VALID_SOURCE_TYPES else "other"
    )
    normalized["source_meta"]["source_url"] = make_absolute_url(base_url, source_meta.get("source_url", "")) or base_url
    normalized["source_meta"]["source_title"] = (source_meta.get("source_title") or "").strip()
    normalized["source_meta"]["raw_publication_date"] = (source_meta.get("raw_publication_date") or "").strip()
    normalized["source_meta"]["raw_journal"] = (source_meta.get("raw_journal") or "").strip()
    normalized["source_meta"]["raw_authors"] = (source_meta.get("raw_authors") or "").strip()
    normalized["source_meta"]["raw_description"] = (source_meta.get("raw_description") or "").strip()

    normalized["links"] = _normalize_links(normalized.get("links", {}), normalized["source_meta"]["source_url"])
    normalized["publication"] = _normalize_publication(
        normalized.get("publication", {}),
        normalized["source_meta"],
        normalized["links"],
    )
    normalized["venue"] = _normalize_venue(normalized.get("venue", {}))
    normalized["authors"] = _normalize_authors(normalized.get("authors", []))
    normalized["indexing"] = dedupe_strings(normalized.get("indexing") or [])
    normalized["tags"] = dedupe_strings(normalized.get("tags") or [])
    normalized["area"] = (normalized.get("area") or "").strip()
    normalized["identifiers"] = _normalize_identifiers(normalized)
    normalized["venue_metrics"] = _normalize_metrics(normalized.get("venue_metrics", []))
    normalized["projects"] = _normalize_projects(normalized.get("projects", []))

    if not normalized["publication"]["year"]:
        normalized["publication"]["year"] = infer_year(
            normalized["publication"]["publication_date"],
            normalized["source_meta"]["raw_publication_date"],
        )

    source_type = normalized["source_meta"].get("source_type")
    if source_type == "scopus" and "Scopus" not in normalized["indexing"]:
        normalized["indexing"].append("Scopus")
    if source_type == "wos" and "Web of Science" not in normalized["indexing"]:
        normalized["indexing"].append("Web of Science")
    if source_type == "google_scholar" and "Google Scholar" not in normalized["indexing"]:
        normalized["indexing"].append("Google Scholar")
    if source_type == "crossref" and "Crossref" not in normalized["indexing"]:
        normalized["indexing"].append("Crossref")
    if source_type == "openalex" and "OpenAlex" not in normalized["indexing"]:
        normalized["indexing"].append("OpenAlex")

    return normalized
