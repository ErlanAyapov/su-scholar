import json

from .utils import (
    VALID_SOURCE_TYPES,
    domain_matches,
    get_hostname,
    is_crossref_url,
    is_doi_url,
    is_openalex_url,
    is_pdf_url,
    is_repository_url,
    is_scholar_url,
)


def _looks_like_json_payload(source: str) -> bool:
    text = (source or "").strip()
    return text.startswith("{") or text.startswith("[")


def detect_source_type(url: str, html: str | None = None, content_type: str | None = None) -> str:
    hostname = get_hostname(url)
    source = html or ""
    content_type = (content_type or "").lower()
    lowered = source.lower()

    if "application/pdf" in content_type or is_pdf_url(url):
        return "pdf_text"

    if is_scholar_url(url):
        return "google_scholar"

    if is_doi_url(url):
        return "doi_page"

    if is_crossref_url(url):
        return "crossref"

    if is_openalex_url(url):
        return "openalex"

    if domain_matches(hostname, "scopus.com"):
        return "scopus"

    if any(domain_matches(hostname, domain) for domain in ("webofscience.com", "clarivate.com")):
        return "wos"

    if is_repository_url(url):
        return "repository"

    if _looks_like_json_payload(source):
        try:
            payload = json.loads(source)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            if "message" in payload and "items" in payload.get("message", {}):
                return "crossref"
            if "results" in payload and any(key in payload for key in ("meta", "group_by")):
                return "openalex"
            if any(key in payload for key in ("doi", "display_name", "authorships")):
                return "openalex"
            return "raw_html"

    if any(marker in lowered for marker in ("gs_rt", "scholar.google", "cited by", "related articles")):
        return "google_scholar"

    if any(marker in lowered for marker in ("scopus", "eid:", "source title", "all science journal classification")):
        return "scopus"

    if any(marker in lowered for marker in ("web of science", "accession number", "clarivate")):
        return "wos"

    if any(marker in lowered for marker in ("crossref", "\"container-title\"", "\"issued\"", "\"publisher\"")):
        return "crossref"

    if any(marker in lowered for marker in ("openalex", "\"authorships\"", "\"primary_location\"", "\"host_venue\"")):
        return "openalex"

    if any(marker in lowered for marker in ("citation_title", "citation_doi", "citation_author", "dc.title", "prism.doi")):
        return "publisher_page"

    if any(marker in lowered for marker in ("doi.org/", "digital object identifier")):
        return "doi_page"

    if content_type.startswith("text/html"):
        return "publisher_page" if hostname else "raw_html"

    source_type = "raw_html" if source else "other"
    return source_type if source_type in VALID_SOURCE_TYPES else "other"
