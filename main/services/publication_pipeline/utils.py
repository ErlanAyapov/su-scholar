import copy
import json
import re
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from dateutil import parser as date_parser


DEFAULT_ALLOWED_DOMAINS = {
    "scholar.google.com",
    "doi.org",
    "dx.doi.org",
    "mdpi.com",
    "sciencedirect.com",
    "springer.com",
    "link.springer.com",
    "crossref.org",
    "api.crossref.org",
    "openalex.org",
    "api.openalex.org",
    "webofscience.com",
    "scopus.com",
    "arxiv.org",
    "zenodo.org",
}

REPOSITORY_DOMAINS = {
    "arxiv.org",
    "zenodo.org",
    "figshare.com",
    "osf.io",
    "researchsquare.com",
}

SOURCE_TYPE_PRIORITY = {
    "doi_page": 100,
    "crossref": 98,
    "openalex": 96,
    "publisher_page": 94,
    "scopus": 90,
    "wos": 88,
    "google_scholar": 70,
    "repository": 60,
    "pdf_text": 50,
    "raw_html": 40,
    "other": 10,
}

VALID_SOURCE_TYPES = {
    "google_scholar",
    "publisher_page",
    "doi_page",
    "crossref",
    "openalex",
    "scopus",
    "wos",
    "repository",
    "pdf_text",
    "raw_html",
    "other",
}

VALID_PUB_TYPES = {"article", "conference_paper", "book_chapter", "monograph", "other"}
VALID_VENUE_KINDS = {"journal", "conference", "book", "other"}
VALID_VENUE_CHARACTERS = {
    "scientific_journal",
    "conf_proceedings",
    "collection",
    "monograph",
    "other",
}
VALID_STATUSES = {"submitted", "accepted", "published"}
VALID_OA_TYPES = {"", "gold", "green", "hybrid"}
VALID_AUTHOR_ROLES = {"first", "corresponding", "coauthor"}
VALID_IDENTIFIER_TYPES = {"doi", "scopus_eid", "wos_accession", "rinz", "gs", "isbn", "issn", "other"}
VALID_METRICS = {"sjr", "citescore", "jif", "snip"}
VALID_PROJECT_TYPES = {"gf", "pcf", "contract", "other"}

MAX_FETCHED_SOURCES = 5
MAX_HTML_BYTES = 2_000_000
MAX_CLEANED_TEXT_CHARS = 30_000
MAX_RAW_PROMPT_CHARS = 8_000
MAX_LINKS = 200

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
ISSN_RE = re.compile(r"\b\d{4}-\d{3}[\dX]\b", re.IGNORECASE)
ISBN_RE = re.compile(r"\b(?:97[89][-\s]?)?\d[\d\s-]{8,16}[\dX]\b", re.IGNORECASE)
QUARTILE_RE = re.compile(r"\b(?:q[\s\-]*([1-4])|quartile\s*([1-4]))\b", re.IGNORECASE)
METRIC_PATTERNS = {
    "sjr": re.compile(r"\bsjr\b[^0-9]{0,12}([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
    "citescore": re.compile(r"\bcitescore\b[^0-9]{0,12}([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
    "jif": re.compile(r"\b(?:jif|impact factor)\b[^0-9]{0,12}([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
    "snip": re.compile(r"\bsnip\b[^0-9]{0,12}([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
}

SCHEMA_TEMPLATE = {
    "publication": {
        "record_id": "",
        "pub_type": "",
        "title_original": "",
        "language": "",
        "year": None,
        "publication_date": "",
        "status": "published",
        "accepted_date": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "article_number": "",
        "total_pages": None,
        "doi": "",
        "abstract": "",
        "keywords": [],
        "candidate_keywords": [],
        "quartile": "",
        "quartile_year": None,
        "citations_count": None,
        "open_access": False,
        "oa_type": "",
        "report_period": "",
        "needs_review": False,
    },
    "venue": {
        "name": "",
        "kind": "journal",
        "character": "scientific_journal",
        "publisher": "",
        "issn": "",
        "isbn": "",
        "conference_name": "",
        "conference_country": "",
        "conference_city": "",
        "series": "",
    },
    "authors": [],
    "indexing": [],
    "tags": [],
    "area": "",
    "identifiers": [],
    "venue_metrics": [],
    "projects": [],
    "links": {
        "url_publisher": "",
        "url_open_access": "",
        "doi_url": "",
        "pdf_url": "",
        "html_url": "",
        "scholar_url": "",
        "related_urls": [],
        "repository_links": [],
        "supplementary_links": [],
    },
    "source_meta": {
        "source_type": "",
        "source_title": "",
        "source_url": "",
        "raw_publication_date": "",
        "raw_journal": "",
        "raw_authors": "",
        "raw_description": "",
    },
}


def build_empty_payload() -> dict[str, Any]:
    return copy.deepcopy(SCHEMA_TEMPLATE)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def smart_truncate(text: str, max_chars: int = MAX_CLEANED_TEXT_CHARS) -> str:
    value = (text or "").strip()
    if len(value) <= max_chars:
        return value

    chunk = max_chars // 3
    middle_start = max(0, len(value) // 2 - chunk // 2)
    return (
        value[:chunk]
        + "\n\n[...TRUNCATED MIDDLE CONTENT...]\n\n"
        + value[middle_start:middle_start + chunk]
        + "\n\n[...TRUNCATED END CONTENT...]\n\n"
        + value[-chunk:]
    )


def truncate_text(text: str, max_chars: int) -> str:
    value = text or ""
    return value[:max_chars]


def json_dumps(data: Any, *, indent: int = 2) -> str:
    return json.dumps(data, ensure_ascii=False, indent=indent)


def parse_json_loose(text: str) -> dict[str, Any]:
    source = (text or "").strip()
    if not source:
        raise ValueError("Empty LLM response")

    try:
        return json.loads(source)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", source, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def deep_fill_defaults(data: Any, template: Any) -> Any:
    if isinstance(template, dict):
        result = {}
        data = data if isinstance(data, dict) else {}
        for key, default_value in template.items():
            result[key] = deep_fill_defaults(data.get(key), default_value)
        for key, value in data.items():
            if key not in result:
                result[key] = value
        return result

    if isinstance(template, list):
        return data if isinstance(data, list) else copy.deepcopy(template)

    return template if data is None else data


def ensure_payload_shape(payload: dict[str, Any] | None) -> dict[str, Any]:
    return deep_fill_defaults(payload or {}, build_empty_payload())


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value

    source = normalize_whitespace(str(value))
    if not source:
        return None
    source = source.replace(",", "")
    try:
        return int(float(source))
    except (TypeError, ValueError):
        return None


def normalize_doi(value: str) -> str:
    raw_value = normalize_whitespace(value)
    if not raw_value:
        return ""

    lowered = raw_value.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if lowered.startswith(prefix):
            raw_value = raw_value[len(prefix):]
            break

    match = DOI_RE.search(raw_value)
    if match:
        return match.group(0).strip().upper()
    return raw_value.strip().upper()


def extract_doi(value: str) -> str:
    match = DOI_RE.search(value or "")
    return normalize_doi(match.group(0)) if match else ""


def parse_date_to_iso(value: str) -> str:
    raw_value = normalize_whitespace(value)
    if not raw_value:
        return ""

    try:
        parsed = date_parser.parse(raw_value, dayfirst=False, fuzzy=True, default=date(1900, 1, 1))
    except (TypeError, ValueError, OverflowError):
        return ""

    if parsed.year < 1900 or parsed.year > date.today().year + 2:
        return ""

    if YEAR_RE.fullmatch(raw_value):
        return ""

    return parsed.date().isoformat()


def infer_year(*values: Any) -> int | None:
    for value in values:
        if value is None:
            continue

        if isinstance(value, int) and 1900 <= value <= date.today().year + 2:
            return value

        match = YEAR_RE.search(str(value))
        if match:
            year = int(match.group(0))
            if 1900 <= year <= date.today().year + 2:
                return year
    return None


def normalize_url(url: str, base_url: str = "") -> str:
    value = normalize_whitespace(url)
    if not value:
        return ""

    absolute_url = urljoin(base_url, value) if base_url else value
    parsed = urlparse(absolute_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    normalized = parsed._replace(fragment="", netloc=parsed.netloc.lower())
    return urlunparse(normalized)


def canonicalize_url_for_dedupe(url: str) -> str:
    normalized = normalize_url(url)
    if not normalized:
        return ""

    parsed = urlparse(normalized)
    path = parsed.path.rstrip("/") or "/"
    query = "&".join(part for part in sorted(filter(None, parsed.query.split("&"))))
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def make_absolute_url(base_url: str, url: str) -> str:
    return normalize_url(url, base_url=base_url)


def get_hostname(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def domain_matches(hostname: str, domain: str) -> bool:
    host = (hostname or "").lower()
    needle = (domain or "").lower()
    return bool(host and needle and (host == needle or host.endswith(f".{needle}")))


def dedupe_strings(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        item = normalize_whitespace(value)
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def dedupe_urls(urls: list[str]) -> list[str]:
    seen = set()
    result = []
    for url in urls:
        normalized = normalize_url(url)
        if not normalized:
            continue
        key = canonicalize_url_for_dedupe(normalized)
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def dedupe_dicts(items: list[dict[str, Any]], key_builder) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for item in items:
        key = key_builder(item)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def normalize_person_name(value: str) -> str:
    text = normalize_whitespace(value)
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip(" ,;")


def normalized_name_key(value: str) -> str:
    text = normalize_person_name(value).lower()
    return re.sub(r"[^a-zа-яё0-9]+", "", text, flags=re.IGNORECASE)


def normalize_title_key(value: str) -> str:
    text = normalize_whitespace(value).lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def normalize_pub_type(value: str) -> str:
    raw_value = (value or "").strip().lower()
    mapping = {
        "journal article": "article",
        "journal-article": "article",
        "article": "article",
        "conference paper": "conference_paper",
        "conference-paper": "conference_paper",
        "proceedings-article": "conference_paper",
        "book chapter": "book_chapter",
        "book-chapter": "book_chapter",
        "chapter": "book_chapter",
        "monograph": "monograph",
        "book": "monograph",
    }
    return mapping.get(raw_value, raw_value if raw_value in VALID_PUB_TYPES else "other")


def normalize_language(value: str) -> str:
    raw_value = (value or "").strip().lower()
    if raw_value in {"en", "ru", "kk", ""}:
        return raw_value
    if raw_value.startswith("eng"):
        return "en"
    if raw_value.startswith("rus"):
        return "ru"
    if raw_value.startswith("kaz"):
        return "kk"
    return ""


def normalize_quartile(value: str) -> str:
    raw_value = normalize_whitespace(value).upper()
    if raw_value in {"Q1", "Q2", "Q3", "Q4"}:
        return raw_value

    match = QUARTILE_RE.search(raw_value)
    if not match:
        return ""
    number = match.group(1) or match.group(2)
    return f"Q{number}" if number in {"1", "2", "3", "4"} else ""


def normalize_metric_name(value: str) -> str:
    raw_value = normalize_whitespace(value).lower()
    mapping = {
        "sjr": "sjr",
        "citescore": "citescore",
        "impact factor": "jif",
        "jif": "jif",
        "snip": "snip",
    }
    return mapping.get(raw_value, raw_value if raw_value in VALID_METRICS else "")


def is_scholar_url(url: str) -> bool:
    return domain_matches(get_hostname(url), "scholar.google.com")


def is_scholar_noise_url(url: str) -> bool:
    normalized = normalize_url(url)
    if not normalized or not is_scholar_url(normalized):
        return False
    lowered = normalized.lower()
    return any(
        marker in lowered
        for marker in (
            "output=cite",
            "view_op=download_citation",
            "/scholar.bib",
            "citations?view_op=",
            "scholar?q=related:",
            "scholar?cites=",
        )
    )


def is_repository_url(url: str) -> bool:
    hostname = get_hostname(url)
    if any(domain_matches(hostname, domain) for domain in REPOSITORY_DOMAINS):
        return True
    return any(token in hostname for token in ("repository", "repo", "eprints", "dspace", "hdl.handle.net"))


def is_pdf_url(url: str) -> bool:
    parsed = urlparse(url or "")
    return parsed.path.lower().endswith(".pdf")


def is_openalex_url(url: str) -> bool:
    return domain_matches(get_hostname(url), "openalex.org") or domain_matches(get_hostname(url), "api.openalex.org")


def is_crossref_url(url: str) -> bool:
    return domain_matches(get_hostname(url), "crossref.org") or domain_matches(get_hostname(url), "api.crossref.org")


def is_doi_url(url: str) -> bool:
    return domain_matches(get_hostname(url), "doi.org") or domain_matches(get_hostname(url), "dx.doi.org")


def is_publisher_like_url(url: str) -> bool:
    normalized = normalize_url(url)
    if not normalized:
        return False
    if is_scholar_url(normalized) or is_doi_url(normalized) or is_openalex_url(normalized) or is_crossref_url(normalized):
        return False
    return not is_repository_url(normalized)


def get_source_priority(source_type: str) -> int:
    return SOURCE_TYPE_PRIORITY.get(source_type or "other", 0)


def merge_string_lists(*lists: list[str]) -> list[str]:
    values: list[str] = []
    for items in lists:
        values.extend(items or [])
    return dedupe_strings(values)


def ensure_list_of_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return dedupe_strings([str(item) for item in value if str(item).strip()])
    if isinstance(value, str):
        parts = re.split(r"[;,]\s*|\n+", value)
        return dedupe_strings(parts)
    return []


def extract_issn(value: str) -> str:
    match = ISSN_RE.search(value or "")
    return match.group(0).upper() if match else ""


def extract_isbn(value: str) -> str:
    match = ISBN_RE.search(value or "")
    return normalize_whitespace(match.group(0)) if match else ""


def extract_quartile_data(source_text: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    text_parts = [source_text or ""]
    if isinstance(meta, dict):
        for value in meta.values():
            if isinstance(value, list):
                text_parts.extend(str(item) for item in value if item)
            elif value:
                text_parts.append(str(value))
    haystack = "\n".join(part for part in text_parts if part)

    quartile = normalize_quartile(haystack)
    quartile_year = None
    venue_metrics = []

    quartile_match = QUARTILE_RE.search(haystack)
    if quartile_match:
        start = max(0, quartile_match.start() - 40)
        end = quartile_match.end() + 40
        local_context = haystack[start:end]
        quartile_year = infer_year(local_context)

    for metric_name, pattern in METRIC_PATTERNS.items():
        match = pattern.search(haystack)
        if not match:
            continue
        metric_year = infer_year(haystack[max(0, match.start() - 40):match.end() + 40])
        venue_metrics.append(
            {
                "metric": metric_name,
                "year": metric_year or quartile_year or infer_year(haystack),
                "value": match.group(1),
                "source": "heuristic_text",
            }
        )

    return {
        "quartile": quartile,
        "quartile_year": quartile_year,
        "venue_metrics": venue_metrics,
    }
