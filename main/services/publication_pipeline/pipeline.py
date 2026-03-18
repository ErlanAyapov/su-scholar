import hashlib
import json
import logging
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests
from django.conf import settings
from django.core.files.base import ContentFile

from main.models import Publication, PublicationFile

from .author_linking import AUTO_LINK_THRESHOLD, link_authors_to_users
from .db_updater import _update_publication_from_payload_with_details
from .extractors import extract_intermediate
from .fetchers import FetchResult, SafeFetcher
from .llm_client import LlmExtractionResult, PublicationLLMClient
from .merger import merge_payloads
from .normalizers import normalize_payload
from .source_detector import detect_source_type
from .utils import (
    DEFAULT_ALLOWED_DOMAINS,
    MAX_FETCHED_SOURCES,
    build_empty_payload,
    canonicalize_url_for_dedupe,
    dedupe_urls,
    domain_matches,
    ensure_payload_shape,
    extract_quartile_data,
    get_hostname,
    infer_year,
    is_crossref_url,
    is_doi_url,
    is_openalex_url,
    is_pdf_url,
    is_publisher_like_url,
    is_repository_url,
    is_scholar_noise_url,
    is_scholar_url,
    json_dumps,
    normalize_doi,
    normalize_quartile,
    preview_text,
    normalize_title_key,
    normalize_url,
)
from .validators import validate_payload


logger = logging.getLogger(__name__)


@dataclass
class ProcessedSource:
    url: str
    fetch: FetchResult | None
    source_type: str
    intermediate: dict[str, Any]
    structured: dict[str, Any]
    llm: LlmExtractionResult | None
    warnings: list[str]


def discover_additional_sources(payload: dict[str, Any]) -> list[str]:
    normalized = ensure_payload_shape(payload)
    links = normalized.get("links", {})
    urls = [
        links.get("url_publisher", ""),
        links.get("url_open_access", ""),
        links.get("doi_url", ""),
        links.get("pdf_url", ""),
        links.get("html_url", ""),
        links.get("scholar_url", ""),
    ]
    urls.extend(links.get("related_urls") or [])
    urls.extend(item.get("url", "") for item in (links.get("repository_links") or []) if isinstance(item, dict))
    urls.extend(item.get("url", "") for item in (links.get("supplementary_links") or []) if isinstance(item, dict))
    return dedupe_urls(urls)


class PublicationPipeline:
    def __init__(
        self,
        *,
        force_refresh: bool = False,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        max_sources: int = MAX_FETCHED_SOURCES,
    ):
        self.force_refresh = force_refresh
        self.max_sources = max_sources
        self.fetcher = SafeFetcher(allowed_domains=DEFAULT_ALLOWED_DOMAINS)
        self.llm_client = PublicationLLMClient(model=model, base_url=base_url, api_key=api_key)

    def _build_seed_urls(self, publication: Publication) -> list[str]:
        seeds = []
        if publication.url_publisher:
            seeds.append(publication.url_publisher)
        if publication.url_open_access:
            seeds.append(publication.url_open_access)
        if publication.doi:
            seeds.append(f"https://doi.org/{normalize_doi(publication.doi)}")
        for repo_link in publication.repo_links.all():
            seeds.append(repo_link.url)
        return dedupe_urls(seeds)

    def _build_debug_dir(self, publication_id: int) -> Path:
        debug_dir = Path(settings.BASE_DIR) / "llm_debug" / f"publication_{publication_id}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        return debug_dir

    def _write_debug_text(self, path: Path, content: str) -> None:
        path.write_text(content or "", encoding="utf-8")

    def _write_debug_json(self, path: Path, data: Any) -> None:
        path.write_text(json_dumps(data, indent=2), encoding="utf-8")

    def _discover_from_intermediate(self, intermediate: dict[str, Any]) -> list[str]:
        urls = []
        meta = intermediate.get("meta", {})
        for key, value in meta.items():
            lowered = key.lower()
            values = value if isinstance(value, list) else [value]
            for item in values:
                if not isinstance(item, str):
                    continue
                if "url" in lowered or "doi" in lowered:
                    urls.append(item)

        for link in intermediate.get("links", []):
            if not isinstance(link, dict):
                continue
            url = link.get("url", "")
            label = (link.get("label") or "").lower()
            url_lower = (url or "").lower()
            if any(token in label for token in ("publisher", "full text", "article", "pdf", "doi")) or any(
                token in url_lower for token in ("pdf", "download", "fulltext", "full-text")
            ):
                urls.append(url)

        return dedupe_urls(urls)

    def _log_source_summary(self, publication_id: int, source_type: str, source_url: str, payload: dict[str, Any]) -> None:
        publication_data = payload.get("publication", {})
        title = preview_text(publication_data.get("title_original", ""), 140)
        abstract = preview_text(publication_data.get("abstract", ""), 200)
        quartile = publication_data.get("quartile", "")
        logger.info(
            "Publication pipeline source publication_id=%s source_type=%s source_url=%s title=%s abstract=%s quartile=%s",
            publication_id,
            source_type,
            source_url,
            title,
            abstract,
            quartile or "",
        )

    def _store_pdf_file(
        self,
        publication: Publication,
        processed_source: ProcessedSource,
        *,
        force_refresh: bool,
    ) -> dict[str, Any]:
        fetch_result = processed_source.fetch
        if not fetch_result or not fetch_result.raw_bytes:
            return {"saved": False, "reason": "no_pdf_bytes"}

        is_pdf = (
            processed_source.source_type == "pdf_text"
            or "application/pdf" in (fetch_result.content_type or "")
            or fetch_result.final_url.lower().endswith(".pdf")
        )
        if not is_pdf:
            return {"saved": False, "reason": "not_pdf"}

        source_url = normalize_url(fetch_result.final_url) or fetch_result.final_url
        if not source_url:
            return {"saved": False, "reason": "missing_source_url"}

        existing = publication.files.filter(kind="pdf", source_url=source_url).first()
        if existing and not force_refresh:
            return {"saved": False, "reason": "already_saved", "file_id": existing.id, "source_url": source_url}

        filename_seed = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:12]
        filename = f"publication_{publication.id}_{filename_seed}.pdf"
        description = preview_text(
            processed_source.structured.get("publication", {}).get("title_original", "") or processed_source.intermediate.get("page_title", ""),
            180,
        )

        if existing:
            existing.source_url = source_url
            existing.description = description
            existing.file.save(filename, ContentFile(fetch_result.raw_bytes), save=False)
            existing.save(update_fields=["source_url", "description", "file"])
            return {"saved": True, "file_id": existing.id, "source_url": source_url}

        pdf_file = PublicationFile.objects.create(
            publication=publication,
            kind="pdf",
            source_url=source_url,
            description=description,
        )
        pdf_file.file.save(filename, ContentFile(fetch_result.raw_bytes), save=True)
        return {"saved": True, "file_id": pdf_file.id, "source_url": source_url}

    def _candidate_score(self, url: str) -> tuple[int, int]:
        normalized = normalize_url(url)
        if not normalized:
            return (-1, 0)
        if is_scholar_noise_url(normalized):
            return (-1, 0)
        if is_doi_url(normalized):
            return (100, 0)
        if is_crossref_url(normalized):
            return (98, 0)
        if is_openalex_url(normalized):
            return (96, 0)
        if is_publisher_like_url(normalized):
            return (94, 0)
        if is_repository_url(normalized):
            return (60, 0)
        if is_scholar_url(normalized):
            return (20, 0)
        return (10, -len(normalized))

    def _should_accept_candidate(self, url: str, *, seed_hosts: set[str]) -> bool:
        normalized = normalize_url(url)
        if not normalized or is_scholar_noise_url(normalized):
            return False

        hostname = get_hostname(normalized)
        if not hostname:
            return False

        if any(domain_matches(hostname, domain) for domain in DEFAULT_ALLOWED_DOMAINS):
            return True
        if hostname in seed_hosts:
            return True
        if is_doi_url(normalized) or is_openalex_url(normalized) or is_crossref_url(normalized):
            return True
        if is_publisher_like_url(normalized) or is_repository_url(normalized):
            return True
        return False

    def _prioritize_candidates(self, urls: list[str]) -> list[str]:
        return [
            url
            for url, _score in sorted(
                ((url, self._candidate_score(url)) for url in dedupe_urls(urls)),
                key=lambda item: item[1],
                reverse=True,
            )
            if _score[0] >= 0
        ]

    def _enrich_indexing(self, payload: dict[str, Any], source_types: list[str]) -> None:
        mapping = {
            "google_scholar": "Google Scholar",
            "scopus": "Scopus",
            "wos": "Web of Science",
            "crossref": "Crossref",
            "openalex": "OpenAlex",
        }
        current = set(payload.get("indexing") or [])
        for source_type in source_types:
            name = mapping.get(source_type)
            if name:
                current.add(name)
        payload["indexing"] = sorted(current)

    def _enrich_from_intermediate(self, payload: dict[str, Any], intermediate: dict[str, Any]) -> None:
        quartile_data = extract_quartile_data(
            intermediate.get("cleaned_text", ""),
            intermediate.get("meta", {}),
        )
        publication = payload.get("publication", {})
        if quartile_data.get("quartile") and not publication.get("quartile"):
            publication["quartile"] = quartile_data["quartile"]
            if quartile_data.get("quartile_year"):
                publication["quartile_year"] = quartile_data["quartile_year"]
            payload.setdefault("source_meta", {})["quartile_source"] = intermediate.get("source_type", "")

        existing_metrics = payload.get("venue_metrics") or []
        if quartile_data.get("venue_metrics"):
            payload["venue_metrics"] = existing_metrics + [
                {
                    **item,
                    "source": intermediate.get("source_type", "") or item.get("source") or "heuristic_text",
                }
                for item in quartile_data["venue_metrics"]
            ]

    def _collect_quartile_conflicts(self, payloads: list[dict[str, Any]], warnings: list[str]) -> bool:
        quartiles = {}
        years = {}
        for payload in payloads:
            publication = payload.get("publication", {})
            quartile = normalize_quartile(publication.get("quartile", ""))
            source_type = payload.get("source_meta", {}).get("source_type", "")
            if quartile:
                quartiles.setdefault(quartile, []).append(source_type)
            quartile_year = publication.get("quartile_year")
            if quartile_year:
                years.setdefault(quartile_year, []).append(source_type)

        conflict = len(quartiles) > 1 or len(years) > 1
        if conflict:
            warnings.append(
                f"Conflicting quartile data detected: quartiles={quartiles or {}}, quartile_years={years or {}}"
            )
        return conflict

    def _maybe_enrich_doi(self, payload: dict[str, Any], warnings: list[str]) -> None:
        publication = payload.get("publication", {})
        if publication.get("doi"):
            return

        title = publication.get("title_original", "")
        year = infer_year(publication.get("year"), publication.get("publication_date"))
        authors = payload.get("authors") or []
        first_author = authors[0]["full_name"] if authors else ""
        if not title:
            return

        doi, doi_url = self._lookup_doi(title=title, year=year, first_author=first_author)
        if not doi:
            return

        publication["doi"] = doi
        payload["links"]["doi_url"] = doi_url or f"https://doi.org/{doi}"
        if {"id_type": "doi", "value": doi} not in payload["identifiers"]:
            payload["identifiers"].append({"id_type": "doi", "value": doi})
        warnings.append("DOI enriched via external metadata lookup")

    def _lookup_doi(self, *, title: str, year: int | None, first_author: str) -> tuple[str, str]:
        openalex_doi = self._lookup_doi_via_openalex(title=title, year=year, first_author=first_author)
        if openalex_doi[0]:
            return openalex_doi
        return self._lookup_doi_via_crossref(title=title, year=year, first_author=first_author)

    def _candidate_matches(self, *, title: str, year: int | None, first_author: str, candidate_title: str, candidate_year: int | None, candidate_authors: list[str]) -> bool:
        title_score = SequenceMatcher(None, normalize_title_key(title), normalize_title_key(candidate_title)).ratio()
        if title_score < 0.92:
            return False
        if year and candidate_year and abs(year - candidate_year) > 1:
            return False
        if first_author:
            normalized_first_author = normalize_title_key(first_author)
            if candidate_authors and not any(normalized_first_author in normalize_title_key(name) for name in candidate_authors):
                return False
        return True

    def _lookup_doi_via_openalex(self, *, title: str, year: int | None, first_author: str) -> tuple[str, str]:
        try:
            response = self.fetcher.session.get(
                "https://api.openalex.org/works",
                params={"search": title, "per-page": 5},
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException:
            return "", ""

        for item in response.json().get("results", []):
            candidate_title = item.get("display_name", "")
            candidate_year = item.get("publication_year")
            candidate_authors = [
                authorship.get("author", {}).get("display_name", "")
                for authorship in item.get("authorships", [])
                if authorship.get("author", {}).get("display_name")
            ]
            if not self._candidate_matches(
                title=title,
                year=year,
                first_author=first_author,
                candidate_title=candidate_title,
                candidate_year=candidate_year,
                candidate_authors=candidate_authors,
            ):
                continue
            doi = normalize_doi(item.get("doi", ""))
            if doi:
                return doi, f"https://doi.org/{doi}"
        return "", ""

    def _lookup_doi_via_crossref(self, *, title: str, year: int | None, first_author: str) -> tuple[str, str]:
        try:
            response = self.fetcher.session.get(
                "https://api.crossref.org/works",
                params={"query.title": title, "rows": 5},
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException:
            return "", ""

        items = response.json().get("message", {}).get("items", [])
        for item in items:
            candidate_title = " ".join(item.get("title") or [])
            issued = item.get("issued", {}).get("date-parts", [])
            candidate_year = issued[0][0] if issued and issued[0] else None
            candidate_authors = [
                " ".join(filter(None, [author.get("given", ""), author.get("family", "")])).strip()
                for author in item.get("author", [])
            ]
            if not self._candidate_matches(
                title=title,
                year=year,
                first_author=first_author,
                candidate_title=candidate_title,
                candidate_year=candidate_year,
                candidate_authors=candidate_authors,
            ):
                continue
            doi = normalize_doi(item.get("DOI", ""))
            if doi:
                return doi, f"https://doi.org/{doi}"
        return "", ""

    def run(self, publication: Publication) -> dict[str, Any]:
        total_started = time.perf_counter()
        warnings: list[str] = []
        errors: list[str] = []

        seeds = self._build_seed_urls(publication)
        if not seeds:
            raise ValueError("Publication has no seed URL or DOI to start the pipeline")

        seed_hosts = {get_hostname(url) for url in seeds if get_hostname(url)}
        allowed_hosts = set(seed_hosts)
        debug_dir = self._build_debug_dir(publication.id)

        queue = list(seeds)
        visited = set()
        processed_sources: list[ProcessedSource] = []
        processed_payloads: list[dict[str, Any]] = []
        metrics: dict[str, Any] = {
            "publication_id": publication.id,
            "sources": [],
            "total_sources_fetched": 0,
            "pdf_files_saved": 0,
        }
        all_candidates: list[dict[str, Any]] = []

        while queue and len(processed_sources) < self.max_sources:
            current_url = queue.pop(0)
            current_key = canonicalize_url_for_dedupe(current_url)
            if not current_key or current_key in visited:
                continue
            visited.add(current_key)

            if not self._should_accept_candidate(current_url, seed_hosts=seed_hosts):
                warnings.append(f"Skipped non-allowed candidate URL: {current_url}")
                continue

            hostname = get_hostname(current_url)
            if hostname:
                allowed_hosts.add(hostname)

            try:
                fetch_result = self.fetcher.fetch_url(
                    current_url,
                    extra_allowed_hosts=allowed_hosts,
                    publication_id=publication.id,
                )
            except Exception as exc:
                warnings.append(f"Failed to fetch {current_url}: {exc}")
                metrics["sources"].append({"url": current_url, "fetch_error": str(exc)})
                continue

            final_host = get_hostname(fetch_result.final_url)
            if final_host:
                allowed_hosts.add(final_host)

            source_type = detect_source_type(fetch_result.final_url, fetch_result.text, content_type=fetch_result.content_type)
            intermediate = extract_intermediate(
                fetch_result.final_url,
                source_type,
                fetch_result.text,
                final_url=fetch_result.final_url,
                content_type=fetch_result.content_type,
            )

            if not processed_sources:
                self._write_debug_text(debug_dir / "01_source_primary.html", fetch_result.text)
                self._write_debug_text(debug_dir / "02_source_primary_cleaned.txt", intermediate.get("cleaned_text", ""))
                self._write_debug_json(debug_dir / "03_source_primary_links.json", intermediate.get("links", []))

            llm_result = None
            structured_payload = build_empty_payload()
            source_warnings = []

            if source_type == "pdf_text" and not intermediate.get("cleaned_text"):
                source_warnings.append(f"Fetched PDF source without extracted text: {fetch_result.final_url}")
            else:
                try:
                    llm_result = self.llm_client.extract(intermediate, publication_id=publication.id)
                    structured_payload = ensure_payload_shape(llm_result.payload)
                except Exception as exc:
                    source_warnings.append(f"LLM extraction failed for {fetch_result.final_url}: {exc}")

            structured_payload["source_meta"]["source_type"] = source_type
            structured_payload["source_meta"]["source_url"] = fetch_result.final_url
            structured_payload["source_meta"]["source_title"] = (
                structured_payload["source_meta"].get("source_title") or intermediate.get("page_title", "")
            )
            self._enrich_from_intermediate(structured_payload, intermediate)

            processed = ProcessedSource(
                url=current_url,
                fetch=fetch_result,
                source_type=source_type,
                intermediate=intermediate,
                structured=structured_payload,
                llm=llm_result,
                warnings=source_warnings,
            )
            processed_sources.append(processed)
            processed_payloads.append(structured_payload)
            warnings.extend(source_warnings)

            self._log_source_summary(publication.id, source_type, fetch_result.final_url, structured_payload)
            pdf_result = self._store_pdf_file(publication, processed, force_refresh=self.force_refresh)
            if pdf_result.get("saved"):
                metrics["pdf_files_saved"] = metrics.get("pdf_files_saved", 0) + 1
                logger.info(
                    "Publication pipeline pdf saved publication_id=%s source_url=%s file_id=%s",
                    publication.id,
                    pdf_result.get("source_url", ""),
                    pdf_result.get("file_id", ""),
                )
            elif pdf_result.get("reason") not in {"not_pdf", "no_pdf_bytes"}:
                logger.info(
                    "Publication pipeline pdf skipped publication_id=%s source_url=%s reason=%s",
                    publication.id,
                    fetch_result.final_url,
                    pdf_result.get("reason", ""),
                )

            if not processed_sources[:-1] and llm_result is not None:
                self._write_debug_json(debug_dir / "04_llm_primary.json", structured_payload)

            candidate_urls = discover_additional_sources(structured_payload)
            candidate_urls.extend(self._discover_from_intermediate(intermediate))
            prioritized = self._prioritize_candidates(candidate_urls)

            if source_type == "google_scholar":
                publisher_url = structured_payload.get("links", {}).get("url_publisher", "")
                if publisher_url and publisher_url not in prioritized:
                    prioritized.insert(0, publisher_url)

            accepted_candidates = []
            for candidate in prioritized:
                if candidate and self._should_accept_candidate(candidate, seed_hosts=seed_hosts):
                    accepted_candidates.append(candidate)
                    host = get_hostname(candidate)
                    if host:
                        allowed_hosts.add(host)
                    if canonicalize_url_for_dedupe(candidate) not in visited:
                        queue.append(candidate)

            all_candidates.append(
                {
                    "source_url": fetch_result.final_url,
                    "source_type": source_type,
                    "discovered_urls": accepted_candidates,
                }
            )

            metrics["sources"].append(
                {
                    "requested_url": fetch_result.requested_url,
                    "final_url": fetch_result.final_url,
                    "source_type": source_type,
                    "fetch_time_sec": round(fetch_result.elapsed_sec, 3),
                    "http_status": fetch_result.status_code,
                    "content_type": fetch_result.content_type,
                    "html_chars": len(fetch_result.text),
                    "cleaned_text_chars": len(intermediate.get("cleaned_text", "")),
                    "prompt_tokens_est": llm_result.prompt_tokens_est if llm_result else 0,
                    "response_tokens_est": llm_result.response_tokens_est if llm_result else 0,
                    "llm_time_sec": round(llm_result.elapsed_sec, 3) if llm_result else 0,
                }
            )

        self._write_debug_json(debug_dir / "05_additional_sources.json", all_candidates)

        if not processed_payloads:
            total_time_sec = round(time.perf_counter() - total_started, 3)
            metrics["total_pipeline_time_sec"] = total_time_sec
            self._write_debug_json(debug_dir / "09_metrics.json", metrics)
            return {
                "success": False,
                "publication_id": publication.id,
                "sources_processed": 0,
                "source_types": [],
                "updated_fields": [],
                "created_authors": [],
                "created_identifiers": [],
                "warnings": warnings,
                "errors": ["No sources were processed successfully"],
                "metrics": metrics,
                "final_payload": build_empty_payload(),
            }

        merge_started = time.perf_counter()
        merged_payload = merge_payloads(processed_payloads)
        metrics["merge_time_sec"] = round(time.perf_counter() - merge_started, 3)

        source_types = [item.source_type for item in processed_sources]
        self._enrich_indexing(merged_payload, source_types)
        self._maybe_enrich_doi(merged_payload, warnings)
        if self._collect_quartile_conflicts(processed_payloads, warnings):
            merged_payload["publication"]["needs_review"] = True
        self._write_debug_json(debug_dir / "06_merged.json", merged_payload)

        normalize_started = time.perf_counter()
        normalized_payload = normalize_payload(merged_payload)
        metrics["normalization_time_sec"] = round(time.perf_counter() - normalize_started, 3)
        self._write_debug_json(debug_dir / "07_normalized.json", normalized_payload)

        linked_authors, author_logs = link_authors_to_users(normalized_payload.get("authors", []), return_logs=True)
        normalized_payload["authors"] = linked_authors
        if any(author.get("needs_review") for author in linked_authors):
            normalized_payload["publication"]["needs_review"] = True
        metrics["author_matching"] = {
            "total_authors": len(linked_authors),
            "auto_linked": sum(1 for author in linked_authors if author.get("match_confidence", 0.0) >= AUTO_LINK_THRESHOLD),
            "review_linked": sum(
                1
                for author in linked_authors
                if author.get("matched_user_id") and author.get("match_confidence", 0.0) < AUTO_LINK_THRESHOLD
            ),
        }
        self._write_debug_json(debug_dir / "08_author_matching.json", author_logs)

        validation = validate_payload(normalized_payload)
        final_payload = validation["payload"]
        self._write_debug_json(debug_dir / "08_final_validated.json", final_payload)
        warnings.extend(validation["warnings"])
        errors.extend(validation["errors"])

        update_started = time.perf_counter()
        update_result = _update_publication_from_payload_with_details(
            publication,
            validation["payload"] if validation["is_valid"] else validation["safe_payload"],
            force_refresh=self.force_refresh if validation["is_valid"] else False,
            destructive_sync=validation["is_valid"],
            sync_relations=validation["is_valid"],
        )
        metrics["db_update_time_sec"] = round(time.perf_counter() - update_started, 3)

        metrics["total_sources_fetched"] = len(processed_sources)
        metrics["total_chars_html"] = sum(len(item.fetch.text) for item in processed_sources if item.fetch)
        metrics["total_cleaned_text_chars"] = sum(len(item.intermediate.get("cleaned_text", "")) for item in processed_sources)
        metrics["total_pipeline_time_sec"] = round(time.perf_counter() - total_started, 3)
        self._write_debug_json(debug_dir / "09_metrics.json", metrics)

        logger.info(
            "Publication pipeline final publication_id=%s title=%s abstract=%s updated_fields=%s linked_users=%s pdf_files_saved=%s",
            publication.id,
            preview_text(final_payload.get("publication", {}).get("title_original", ""), 140),
            preview_text(final_payload.get("publication", {}).get("abstract", ""), 200),
            update_result.updated_fields,
            sorted(set(update_result.linked_users)),
            metrics.get("pdf_files_saved", 0),
        )

        return {
            "success": True,
            "publication_id": publication.id,
            "sources_processed": len(processed_sources),
            "source_types": sorted(set(source_types)),
            "updated_fields": update_result.updated_fields,
            "created_authors": update_result.created_authors,
            "created_identifiers": update_result.created_identifiers,
            "linked_user_ids": sorted(set(update_result.linked_users)),
            "warnings": warnings,
            "errors": errors,
            "metrics": metrics,
            "final_payload": final_payload,
        }


def run_publication_pipeline(publication_id: int, force_refresh: bool = False) -> dict[str, Any]:
    publication = (
        Publication.objects.select_related("language", "pub_type", "venue")
        .prefetch_related("repo_links", "publicationauthor_set__author", "identifiers", "indexing", "tags")
        .get(id=publication_id)
    )
    return PublicationPipeline(force_refresh=force_refresh).run(publication)
