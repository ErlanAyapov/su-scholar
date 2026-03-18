import ipaddress
import logging
import socket
import time
from dataclasses import dataclass
from typing import Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .utils import DEFAULT_ALLOWED_DOMAINS, MAX_HTML_BYTES, domain_matches, get_hostname, normalize_url


logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int | None
    content_type: str
    text: str
    headers: dict[str, str]
    elapsed_sec: float
    source_bytes: int
    truncated: bool = False
    error: str = ""
    raw_bytes: bytes = b""


def _iter_resolved_ips(hostname: str) -> set[str]:
    try:
        records = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return set()

    addresses = set()
    for record in records:
        address = record[4][0]
        addresses.add(address)
    return addresses


def _is_private_or_blocked_ip(ip_value: str) -> bool:
    ip = ipaddress.ip_address(ip_value)
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_reserved,
            ip.is_multicast,
            ip.is_unspecified,
            str(ip) == "169.254.169.254",
        )
    )


def validate_url(
    url: str,
    *,
    allowed_domains: Iterable[str] | None = None,
    extra_allowed_hosts: Iterable[str] | None = None,
    allow_all_public_hosts: bool = False,
) -> str:
    normalized = normalize_url(url)
    if not normalized:
        raise ValueError(f"Unsupported URL: {url!r}")

    hostname = get_hostname(normalized)
    if not hostname:
        raise ValueError(f"URL has no hostname: {url!r}")

    try:
        if _is_private_or_blocked_ip(hostname):
            raise ValueError(f"Blocked IP target: {url}")
    except ValueError as exc:
        if "does not appear to be an IPv4 or IPv6 address" not in str(exc):
            raise

    if not allow_all_public_hosts:
        allowed = {item.lower() for item in (allowed_domains or DEFAULT_ALLOWED_DOMAINS)}
        dynamic = {item.lower() for item in (extra_allowed_hosts or [])}
        if not any(domain_matches(hostname, domain) for domain in allowed | dynamic):
            raise ValueError(f"Hostname is not allowlisted: {hostname}")

    for ip_value in _iter_resolved_ips(hostname):
        if _is_private_or_blocked_ip(ip_value):
            raise ValueError(f"Resolved private or blocked IP for hostname {hostname}")

    return normalized


def validate_public_url_target(url: str, *, allow_all_public_hosts: bool = False) -> str:
    normalized = normalize_url(url)
    if not normalized:
        raise ValueError(f"Unsupported URL: {url!r}")

    hostname = get_hostname(normalized)
    if not hostname:
        raise ValueError(f"URL has no hostname: {url!r}")

    try:
        if _is_private_or_blocked_ip(hostname):
            raise ValueError(f"Blocked IP target: {url}")
    except ValueError as exc:
        if "does not appear to be an IPv4 or IPv6 address" not in str(exc):
            raise

    for ip_value in _iter_resolved_ips(hostname):
        if _is_private_or_blocked_ip(ip_value):
            raise ValueError(f"Resolved private or blocked IP for hostname {hostname}")

    return validate_url(url, allow_all_public_hosts=allow_all_public_hosts)


class SafeFetcher:
    def __init__(
        self,
        *,
        timeout: int = 25,
        max_redirects: int = 5,
        max_bytes: int = MAX_HTML_BYTES,
        max_pdf_bytes: int = 25_000_000,
        allowed_domains: Iterable[str] | None = None,
        allow_all_public_hosts: bool = False,
        retries: int = 2,
    ):
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self.max_pdf_bytes = max_pdf_bytes
        self.allowed_domains = set(allowed_domains or DEFAULT_ALLOWED_DOMAINS)
        self.allow_all_public_hosts = allow_all_public_hosts
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.session.max_redirects = max_redirects

        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            backoff_factor=0.4,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def fetch_url(
        self,
        url: str,
        *,
        extra_allowed_hosts: Iterable[str] | None = None,
        publication_id: int | None = None,
    ) -> FetchResult:
        requested_url = validate_url(
            url,
            allowed_domains=self.allowed_domains,
            extra_allowed_hosts=extra_allowed_hosts,
            allow_all_public_hosts=self.allow_all_public_hosts,
        )

        started = time.perf_counter()
        response = self.session.get(requested_url, timeout=self.timeout, allow_redirects=True, stream=True)
        elapsed_sec = time.perf_counter() - started
        response.raise_for_status()

        try:
            final_url = validate_url(
                response.url,
                allowed_domains=self.allowed_domains,
                extra_allowed_hosts=extra_allowed_hosts,
                allow_all_public_hosts=self.allow_all_public_hosts,
            )
        except ValueError as exc:
            if "allowlisted" not in str(exc):
                raise
            final_url = validate_public_url_target(
                response.url,
                allow_all_public_hosts=self.allow_all_public_hosts,
            )

        content_type = (response.headers.get("Content-Type") or "").lower()
        is_pdf_target = "application/pdf" in content_type or final_url.lower().endswith(".pdf")
        byte_limit = self.max_pdf_bytes if is_pdf_target else self.max_bytes
        raw_chunks = []
        source_bytes = 0
        truncated = False

        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            source_bytes += len(chunk)
            if source_bytes > byte_limit:
                remaining = byte_limit - sum(len(item) for item in raw_chunks)
                if remaining > 0:
                    raw_chunks.append(chunk[:remaining])
                truncated = True
                break
            raw_chunks.append(chunk)

        raw_bytes = b"".join(raw_chunks)
        text = ""
        if not is_pdf_target:
            encoding = response.encoding or response.apparent_encoding or "utf-8"
            text = raw_bytes.decode(encoding, errors="replace")

        logger.info(
            "Fetched source publication_id=%s url=%s final_url=%s status=%s bytes=%s truncated=%s",
            publication_id or "",
            requested_url,
            final_url,
            response.status_code,
            source_bytes,
            truncated,
        )

        return FetchResult(
            requested_url=requested_url,
            final_url=final_url,
            status_code=response.status_code,
            content_type=content_type,
            text=text,
            raw_bytes=raw_bytes,
            headers=dict(response.headers),
            elapsed_sec=elapsed_sec,
            source_bytes=source_bytes,
            truncated=truncated,
        )


def fetch_url(url: str, publication_id: int | None = None) -> FetchResult:
    return SafeFetcher().fetch_url(url, publication_id=publication_id)
