from django.core.paginator import Paginator
from django.db.models import Count, Max, Min, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.contrib.auth import get_user_model

from main.models import DepartmentArea, IndexingDatabase, Language, Publication, PublicationType, Tag, Venue

User = get_user_model()


def _parse_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _apply_staff_user_filter(queryset, staff_user_id):
    staff_user = None
    if not staff_user_id:
        return queryset, staff_user

    staff_user = User.objects.filter(id=staff_user_id).first()
    if not staff_user:
        return queryset, None

    full_name_parts = [staff_user.last_name.strip(), staff_user.first_name.strip()]
    author_filter = Q()
    for part in full_name_parts:
        if part:
            author_filter &= Q(authors__full_name__icontains=part)

    user_filter = Q(created_by=staff_user)
    if author_filter:
        user_filter |= author_filter
    if staff_user.orc_id:
        user_filter |= Q(authors__orcid__iexact=staff_user.orc_id)

    return queryset.filter(user_filter), staff_user


def main_page(request):
    qs = (
        Publication.objects.select_related("pub_type", "language", "venue", "area")
        .prefetch_related("tags", "indexing", "authors")
        .all()
    )

    staff_user_id = _parse_int(request.GET.get("staff_user"))
    qs, staff_user = _apply_staff_user_filter(qs, staff_user_id)

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
        "staff_user": staff_user,
    }
    return render(request, "main/main.html", ctx)


def search_suggestions(request):
    query = (request.GET.get("q") or "").strip()
    if len(query) < 3:
        return JsonResponse({"suggestions": []})

    staff_user_id = _parse_int(request.GET.get("staff_user"))
    base_qs, _ = _apply_staff_user_filter(Publication.objects.all(), staff_user_id)

    suggestions = []
    seen_values = set()

    def add_suggestions(values, kind, prefix=""):
        for raw_value in values:
            value = (raw_value or "").strip()
            if not value:
                continue
            lowered = value.casefold()
            if lowered in seen_values:
                continue
            seen_values.add(lowered)
            suggestions.append(
                {
                    "value": value,
                    "label": f"{prefix}{value}",
                    "kind": kind,
                }
            )
            if len(suggestions) >= 10:
                return True
        return False

    if add_suggestions(
        base_qs.filter(title_original__icontains=query)
        .values_list("title_original", flat=True)[:5],
        kind="title",
    ):
        return JsonResponse({"suggestions": suggestions})

    if add_suggestions(
        base_qs.filter(authors__full_name__icontains=query)
        .exclude(authors__full_name="")
        .values_list("authors__full_name", flat=True)
        .distinct()[:3],
        kind="author",
        prefix="Автор: ",
    ):
        return JsonResponse({"suggestions": suggestions})

    if add_suggestions(
        base_qs.filter(venue__name__icontains=query)
        .exclude(venue__name="")
        .values_list("venue__name", flat=True)
        .distinct()[:3],
        kind="venue",
        prefix="Дереккөз: ",
    ):
        return JsonResponse({"suggestions": suggestions})

    add_suggestions(
        base_qs.filter(doi__icontains=query)
        .exclude(doi="")
        .values_list("doi", flat=True)
        .distinct()[:3],
        kind="doi",
        prefix="DOI: ",
    )

    return JsonResponse({"suggestions": suggestions})


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
