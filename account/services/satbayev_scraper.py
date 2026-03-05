import re
from difflib import SequenceMatcher
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

GOOGLE_SEARCH_URL = "https://www.google.com/search"
BING_SEARCH_URL = "https://www.bing.com/search"
BRAVE_SEARCH_URL = "https://search.brave.com/search"
SATBAYEV_TEACHERS_PREFIX = "https://official.satbayev.university/ru/teachers"

MAX_CANDIDATES_TO_CHECK = 12
MIN_NAME_SCORE = 0.62

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

ORCID_RE = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b", re.IGNORECASE)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
ISSN_RE = re.compile(r"\b\d{4}-?\d{3}[\dX]\b", re.IGNORECASE)
SCOPUS_EID_RE = re.compile(r"\b2-s2\.0-\d+\b", re.IGNORECASE)
SCOPUS_AUTHOR_RE = re.compile(r"authorId=(\d+)", re.IGNORECASE)

TRANSLIT_MAP = {
    "а": "a",
    "ә": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "ғ": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "қ": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "ң": "n",
    "о": "o",
    "ө": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ұ": "u",
    "ү": "u",
    "ф": "f",
    "х": "h",
    "һ": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ы": "y",
    "і": "i",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "ь": "",
    "ъ": "",
}

NORMALIZE_MAP = str.maketrans(
    {
        "ё": "е",
        "ә": "а",
        "ғ": "г",
        "қ": "к",
        "ң": "н",
        "ө": "о",
        "ұ": "у",
        "ү": "у",
        "һ": "х",
        "і": "и",
    }
)


def build_user_full_name(user) -> str:
    return " ".join(
        part.strip() for part in [user.last_name, user.first_name, user.father_name] if part and part.strip()
    )


def _unique_keep_order(values):
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _normalize_text(text: str) -> str:
    value = (text or "").strip().lower().translate(NORMALIZE_MAP)
    value = re.sub(r"[^a-zа-я0-9\s-]+", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _name_match_score(expected_name: str, profile_name: str) -> float:
    expected = _normalize_text(expected_name)
    actual = _normalize_text(profile_name)
    if not expected or not actual:
        return 0.0

    expected_tokens = expected.split()
    actual_tokens = actual.split()
    actual_set = set(actual_tokens)

    ratio = SequenceMatcher(None, expected, actual).ratio()
    overlap = len(set(expected_tokens) & actual_set) / max(len(set(expected_tokens)), 1)

    required = expected_tokens[:2]
    required_score = (
        sum(1 for token in required if token in actual_set) / len(required)
        if required
        else 0.0
    )
    return (ratio * 0.5) + (overlap * 0.3) + (required_score * 0.2)


def _extract_google_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for a_tag in soup.select("a[href]"):
        href = a_tag.get("href", "")

        if href.startswith("/url?"):
            parsed = urlparse(href)
            query = parse_qs(parsed.query)
            target = query.get("q", [None])[0]
            if target:
                links.append(target)
            continue

        if href.startswith("http://") or href.startswith("https://"):
            links.append(href)

    links = _unique_keep_order(links)
    return [link for link in links if SATBAYEV_TEACHERS_PREFIX in link]


def _extract_bing_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = [a_tag.get("href", "") for a_tag in soup.select("li.b_algo h2 a[href]")]
    links = _unique_keep_order([link for link in links if link])
    return [link for link in links if SATBAYEV_TEACHERS_PREFIX in link]


def _extract_brave_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for a_tag in soup.select("a[href]"):
        href = a_tag.get("href", "")
        if SATBAYEV_TEACHERS_PREFIX in href:
            links.append(href)
    links = _unique_keep_order(links)
    return [link for link in links if SATBAYEV_TEACHERS_PREFIX in link]


def _search_candidates(url: str, params: dict, extractor, timeout: int) -> list[str]:
    try:
        response = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException:
        return []
    return extractor(response.text)


def _build_slug_candidates(full_name: str) -> list[str]:
    parts = [part for part in full_name.split() if part]
    if not parts:
        return []

    transliterated = []
    for part in parts:
        converted = "".join(TRANSLIT_MAP.get(char.lower(), char.lower()) for char in part)
        converted = re.sub(r"[^a-z0-9]+", "-", converted).strip("-")
        if converted:
            transliterated.append(converted)

    if not transliterated:
        return []

    slugs = ["-".join(transliterated)]
    if len(transliterated) >= 2:
        slugs.append("-".join(transliterated[:2]))
    if len(transliterated) >= 3:
        slugs.append("-".join([transliterated[0], transliterated[1], transliterated[-1]]))
    slugs = _unique_keep_order(slugs)
    return [f"{SATBAYEV_TEACHERS_PREFIX}/{slug}" for slug in slugs]


def _collect_candidate_urls(full_name: str, timeout: int = 15) -> list[str]:
    queries = [
        f'site:official.satbayev.university/ru/teachers "{full_name}"',
        f"site:official.satbayev.university/ru/teachers {full_name}",
        f"site:official.satbayev.university teachers {full_name}",
    ]

    urls = []
    for query in queries:
        urls.extend(
            _search_candidates(
                BRAVE_SEARCH_URL,
                params={"q": query},
                extractor=_extract_brave_links,
                timeout=timeout,
            )
        )
        if len(set(urls)) >= MAX_CANDIDATES_TO_CHECK:
            break

        urls.extend(
            _search_candidates(
                GOOGLE_SEARCH_URL,
                params={"q": query, "hl": "ru"},
                extractor=_extract_google_links,
                timeout=timeout,
            )
        )
        if len(set(urls)) >= MAX_CANDIDATES_TO_CHECK:
            break

        urls.extend(
            _search_candidates(
                BING_SEARCH_URL,
                params={"q": query, "setlang": "ru"},
                extractor=_extract_bing_links,
                timeout=timeout,
            )
        )
        if len(set(urls)) >= MAX_CANDIDATES_TO_CHECK:
            break

    urls.extend(_build_slug_candidates(full_name))
    return _unique_keep_order(urls)


def _parse_profile_links(links: list[str]) -> dict:
    parsed = {
        "scopus_id": "",
        "orc_id": "",
        "wos_id": "",
        "researchgate": "",
        "google_scholar": "",
    }

    for link in links:
        lowered = link.lower()

        if "scopus.com" in lowered:
            match = SCOPUS_AUTHOR_RE.search(link)
            if match:
                parsed["scopus_id"] = match.group(1)

        if "orcid.org" in lowered:
            match = ORCID_RE.search(link)
            if match:
                parsed["orc_id"] = match.group(0)

        if "scholar.google." in lowered:
            parsed["google_scholar"] = link

        if "webofscience.com" in lowered:
            parsed["wos_id"] = link

        if "researchgate.net" in lowered:
            parsed["researchgate"] = link

    return parsed


def _extract_journal_data(pub_block, base_url: str) -> tuple[list[str], list[dict]]:
    if pub_block is None:
        return [], []

    journal_links = []
    for anchor in pub_block.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not href or href.startswith("javascript:"):
            continue
        journal_links.append(urljoin(base_url, href))
    journal_links = _unique_keep_order(journal_links)

    text = pub_block.get_text(" ", strip=True)
    dois = _unique_keep_order([match.upper() for match in DOI_RE.findall(text)])
    issns = _unique_keep_order([match.replace("-", "").upper() for match in ISSN_RE.findall(text)])
    eids = _unique_keep_order([match for match in SCOPUS_EID_RE.findall(text)])

    journal_ids = []
    journal_ids.extend({"type": "doi", "value": value} for value in dois)
    journal_ids.extend({"type": "issn", "value": value} for value in issns)
    journal_ids.extend({"type": "eid", "value": value} for value in eids)
    return journal_links, journal_ids


def extract_teacher_profile(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    header_name = soup.select_one(".teacher-header h1")
    profile_name = header_name.get_text(" ", strip=True) if header_name else ""

    email_tag = soup.select_one("a[href^='mailto:']")
    email = ""
    if email_tag:
        email = email_tag.get("href", "").replace("mailto:", "").strip()

    links_container = soup.select_one(".teacher-header .links") or soup.select_one(".links")
    external_links = []
    if links_container:
        for link in links_container.select("a[href]"):
            href = link.get("href", "").strip()
            if href:
                external_links.append(urljoin(page_url, href))
    external_links = _unique_keep_order(external_links)

    parsed_links = _parse_profile_links(external_links)
    journal_links, journal_ids = _extract_journal_data(soup.select_one("#pub"), page_url)

    return {
        "profile_name": profile_name,
        "satbayev_profile_url": page_url,
        "email": email,
        "journal_links": journal_links,
        "journal_ids": journal_ids,
        **parsed_links,
    }


def _find_best_profile_data(full_name: str, timeout: int = 15) -> dict | None:
    candidate_urls = _collect_candidate_urls(full_name=full_name, timeout=timeout)
    best_profile = None
    best_score = 0.0

    for url in candidate_urls[:MAX_CANDIDATES_TO_CHECK]:
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException:
            continue

        profile_data = extract_teacher_profile(response.text, url)
        profile_name = profile_data.get("profile_name", "")
        score = _name_match_score(full_name, profile_name)
        if score > best_score:
            best_score = score
            best_profile = profile_data
        if score >= 0.95:
            break

    if not best_profile or best_score < MIN_NAME_SCORE:
        return None

    best_profile["match_score"] = round(best_score, 3)
    return best_profile


def find_teacher_page_url(full_name: str, timeout: int = 15) -> str | None:
    profile_data = _find_best_profile_data(full_name=full_name, timeout=timeout)
    if not profile_data:
        return None
    return profile_data.get("satbayev_profile_url")


def load_teacher_profile_data(full_name: str, timeout: int = 15) -> dict | None:
    return _find_best_profile_data(full_name=full_name, timeout=timeout)
