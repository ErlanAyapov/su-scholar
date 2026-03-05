from django.core.paginator import Paginator
from django.db.models import Count, Max, Min, Q
from django.shortcuts import get_object_or_404, render

from main.models import DepartmentArea, IndexingDatabase, Language, Publication, PublicationType, Tag, Venue


def _parse_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def main_page(request):
    qs = (
        Publication.objects.select_related("pub_type", "language", "venue", "area")
        .prefetch_related("tags", "indexing", "authors")
        .all()
    )

    search_text = (request.GET.get("search") or "").strip()
    if search_text:
        qs = qs.filter(
            Q(title_original__icontains=search_text)
            | Q(doi__icontains=search_text)
            | Q(keywords__icontains=search_text)
            | Q(venue__name__icontains=search_text)
            | Q(authors__full_name__icontains=search_text)
            | Q(record_id__icontains=search_text)
        )

    type_id = _parse_int(request.GET.get("type"))
    if type_id:
        qs = qs.filter(pub_type_id=type_id)

    lang_id = _parse_int(request.GET.get("lang"))
    if lang_id:
        qs = qs.filter(language_id=lang_id)

    status = request.GET.get("status")
    if status in {"submitted", "accepted", "published"}:
        qs = qs.filter(status=status)

    oa = request.GET.get("oa")
    if oa in {"0", "1"}:
        qs = qs.filter(open_access=(oa == "1"))

    y_min = _parse_int(request.GET.get("y_min"))
    if y_min:
        qs = qs.filter(year__gte=y_min)

    y_max = _parse_int(request.GET.get("y_max"))
    if y_max:
        qs = qs.filter(year__lte=y_max)

    db_id = _parse_int(request.GET.get("db"))
    if db_id:
        qs = qs.filter(indexing__id=db_id)

    quartile = (request.GET.get("quartile") or "").strip().upper()
    if quartile in {"Q1", "Q2", "Q3", "Q4"}:
        qs = qs.filter(quartile=quartile)

    area_id = _parse_int(request.GET.get("area"))
    if area_id:
        qs = qs.filter(area_id=area_id)

    venue_id = _parse_int(request.GET.get("venue"))
    if venue_id:
        qs = qs.filter(venue_id=venue_id)

    selected_tags = [str(tag_id) for tag_id in request.GET.getlist("tags") if tag_id]
    if selected_tags:
        tag_ids = [_parse_int(tag_id) for tag_id in selected_tags]
        tag_ids = [tag_id for tag_id in tag_ids if tag_id]
        if tag_ids:
            qs = qs.filter(tags__id__in=tag_ids)

    qs = qs.distinct().order_by("-year", "-id")
    total_count = qs.count()

    paginator = Paginator(qs, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    query_params = request.GET.copy()
    query_params.pop("page", None)
    base_query = query_params.urlencode()

    years = Publication.objects.aggregate(min=Min("year"), max=Max("year"))
    ctx = {
        "pub_types": PublicationType.objects.annotate(c=Count("publication", distinct=True)).order_by("-c", "name"),
        "languages": Language.objects.annotate(c=Count("publication", distinct=True)).order_by("-c", "name"),
        "indexing_dbs": IndexingDatabase.objects.annotate(c=Count("publication", distinct=True)).order_by("-c", "name"),
        "areas": DepartmentArea.objects.annotate(c=Count("publication", distinct=True)).order_by("-c", "name"),
        "tags_top": Tag.objects.annotate(c=Count("publication", distinct=True)).order_by("-c", "name")[:20],
        "venues_top": Venue.objects.annotate(c=Count("publications", distinct=True)).order_by("-c", "name")[:15],
        "year_min": years["min"] or 1900,
        "year_max": years["max"] or 2026,
        "selected_tags": selected_tags,
        "results": page_obj.object_list,
        "page_obj": page_obj,
        "total_count": total_count,
        "base_query": base_query,
    }
    return render(request, "main/main.html", ctx)


def publication_detail_page(request, pk: int):
    publication = get_object_or_404(
        Publication.objects.select_related(
            "pub_type",
            "language",
            "venue",
            "area",
            "created_by",
        ).prefetch_related(
            "indexing",
            "tags",
            "authors",
            "identifiers",
            "files",
            "repo_links",
            "venue__metrics",
            "publicationauthor_set__author",
            "publicationproject_set__project",
        ),
        pk=pk,
    )

    ctx = {
        "publication": publication,
        "author_links": publication.publicationauthor_set.all().order_by("order"),
        "project_links": publication.publicationproject_set.all(),
    }
    return render(request, "main/publication_detail.html", ctx)
