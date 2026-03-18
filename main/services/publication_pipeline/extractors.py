import json
from collections import defaultdict
from typing import Any

from bs4 import BeautifulSoup

from .utils import MAX_LINKS, json_dumps, make_absolute_url, normalize_whitespace


NOISE_SELECTORS = (
    "script",
    "style",
    "noscript",
    "svg",
    "img",
    "header",
    "footer",
    "aside",
    "nav",
)


def _append_meta_value(target: dict[str, Any], key: str, value: str) -> None:
    if not key or not value:
        return
    if key not in target:
        target[key] = value
        return
    existing = target[key]
    if isinstance(existing, list):
        if value not in existing:
            existing.append(value)
        return
    if existing != value:
        target[key] = [existing, value]


def _extract_meta(soup: BeautifulSoup) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("name") or tag.get("property") or tag.get("http-equiv") or "").strip()
        value = normalize_whitespace(tag.get("content", ""))
        if not key or not value:
            continue
        _append_meta_value(meta, key, value)
    return meta


def _extract_json_ld(soup: BeautifulSoup) -> list[Any]:
    payloads = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = (tag.string or tag.get_text()).strip()
        if not text:
            continue
        try:
            payloads.append(json.loads(text))
        except json.JSONDecodeError:
            payloads.append(text[:8000])
    return payloads


def _extract_links(soup: BeautifulSoup, base_url: str) -> list[dict[str, str]]:
    seen = set()
    links = []
    for anchor in soup.find_all("a", href=True):
        url = make_absolute_url(base_url, anchor.get("href", ""))
        label = normalize_whitespace(anchor.get_text(" ", strip=True))
        if not url:
            continue
        key = (url.lower(), label.lower())
        if key in seen:
            continue
        seen.add(key)
        links.append({"url": url, "label": label})
        if len(links) >= MAX_LINKS:
            break
    return links


def _extract_headings(soup: BeautifulSoup) -> dict[str, list[str]]:
    headings: dict[str, list[str]] = defaultdict(list)
    for level in ("h1", "h2"):
        for tag in soup.find_all(level):
            text = normalize_whitespace(tag.get_text(" ", strip=True))
            if text:
                headings[level].append(text)
    return dict(headings)


def _extract_cleaned_text(soup: BeautifulSoup) -> str:
    cleaned = BeautifulSoup(str(soup), "html.parser")
    for selector in NOISE_SELECTORS:
        for tag in cleaned.select(selector):
            tag.decompose()
    body = cleaned.body or cleaned
    return normalize_whitespace(body.get_text(" ", strip=True))


def _extract_json_intermediate(source_url: str, source_type: str, raw_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        payload = {"raw_text": raw_text[:8000]}

    meta = {}
    if isinstance(payload, dict):
        for key in ("title", "display_name", "doi", "publisher", "type", "publication_year", "abstract"):
            value = payload.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                meta[key] = value

        message = payload.get("message")
        if isinstance(message, dict):
            for key in ("title", "DOI", "publisher", "type", "issued", "container-title", "author", "abstract"):
                value = message.get(key)
                if value:
                    meta[f"message.{key}"] = value

    page_title = ""
    if isinstance(payload, dict):
        page_title = normalize_whitespace(
            str(payload.get("title") or payload.get("display_name") or payload.get("id") or "")
        )

    return {
        "source_url": source_url,
        "source_type": source_type,
        "page_title": page_title,
        "cleaned_text": json_dumps(payload, indent=2),
        "meta": meta,
        "json_ld": [],
        "links": [],
        "headings": {"h1": [page_title] if page_title else [], "h2": []},
        "raw_html": raw_text,
        "content_type": "application/json",
    }


def extract_intermediate(
    source_url: str,
    source_type: str,
    html: str,
    *,
    final_url: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    source_url = final_url or source_url
    content_type = (content_type or "").lower()
    raw_text = html or ""

    if "application/json" in content_type or raw_text.strip().startswith("{") or raw_text.strip().startswith("["):
        return _extract_json_intermediate(source_url, source_type, raw_text)

    if source_type == "pdf_text" and not raw_text.strip():
        return {
            "source_url": source_url,
            "source_type": source_type,
            "page_title": "",
            "cleaned_text": "",
            "meta": {},
            "json_ld": [],
            "links": [],
            "headings": {"h1": [], "h2": []},
            "raw_html": "",
            "content_type": content_type,
        }

    soup = BeautifulSoup(raw_text, "html.parser")
    page_title = normalize_whitespace(soup.title.get_text(" ", strip=True) if soup.title else "")
    meta = _extract_meta(soup)
    json_ld = _extract_json_ld(soup)
    links = _extract_links(soup, source_url)
    headings = _extract_headings(soup)
    cleaned_text = _extract_cleaned_text(soup) if raw_text else ""

    return {
        "source_url": source_url,
        "source_type": source_type,
        "page_title": page_title,
        "cleaned_text": cleaned_text,
        "meta": meta,
        "json_ld": json_ld,
        "links": links,
        "headings": headings,
        "raw_html": raw_text,
        "content_type": content_type,
    }
