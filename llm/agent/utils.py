from __future__ import annotations

from django.db.models import Q

from main.models import Project, Publication


def _normalize_terms(keywords) -> list[str]:
    if isinstance(keywords, str):
        raw_items = keywords.replace(",", " ").split()
    elif isinstance(keywords, (list, tuple, set)):
        raw_items = []
        for item in keywords:
            raw_items.extend(str(item or "").replace(",", " ").split())
    else:
        raw_items = []

    terms = []
    seen = set()
    for item in raw_items:
        cleaned = " ".join(str(item or "").strip().split())
        normalized = cleaned.lower()
        if not cleaned or normalized in seen:
            continue
        seen.add(normalized)
        terms.append(cleaned)
    return terms


def search_from_db(keywords):
    terms = _normalize_terms(keywords)
    if not terms:
        return {"publications": [], "projects": []}

    publication_query = Q()
    project_query = Q()

    for term in terms:
        publication_query |= (
            Q(title_original__icontains=term)
            | Q(abstract__icontains=term)
            | Q(keywords__icontains=term)
            | Q(doi__icontains=term)
            | Q(venue__name__icontains=term)
            | Q(authors__full_name__icontains=term)
        )
        project_query |= Q(name__icontains=term) | Q(description__icontains=term)

    publications = []
    publication_queryset = (
        Publication.objects.filter(private=False)
        .filter(publication_query)
        .select_related("venue")
        .prefetch_related("authors")
        .distinct()
        .order_by("-year", "-id")[:20]
    )
    for publication in publication_queryset:
        authors = ", ".join(
            author.full_name.strip()
            for author in publication.authors.all()
            if (author.full_name or "").strip()
        )
        publications.append(
            {
                "id": publication.id,
                "title": publication.title_original or "",
                "abstract": publication.abstract or "",
                "year": publication.year,
                "venue": publication.venue.name if publication.venue_id and publication.venue else "",
                "keywords": publication.keywords or "",
                "authors": authors,
                "doi": publication.doi or "",
            }
        )

    projects = list(
        Project.objects.filter(project_query)
        .values("id", "name", "description")
        .distinct()
        .order_by("-id")[:20]
    )

    return {
        "publications": publications,
        "projects": projects,
    }


def trim_db_result(data, max_publications=20, max_projects=20, max_text_len=2000):
    publications = []
    for item in data.get("publications", [])[:max_publications]:
        publications.append(
            {
                "id": item.get("id"),
                "title": (item.get("title") or "")[:300],
                "abstract": (item.get("abstract") or "")[:max_text_len],
                "year": item.get("year"),
                "venue": (item.get("venue") or "")[:300],
                "keywords": (item.get("keywords") or "")[:500],
                "authors": (item.get("authors") or "")[:500],
                "doi": (item.get("doi") or "")[:120],
            }
        )

    projects = []
    for item in data.get("projects", [])[:max_projects]:
        projects.append(
            {
                "id": item.get("id"),
                "name": (item.get("name") or "")[:300],
                "description": (item.get("description") or "")[:max_text_len],
            }
        )

    return {
        "publications": publications,
        "projects": projects,
    }
