import os
import tempfile
from datetime import date
import logging

from django.contrib.auth import get_user_model
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db.models import Count, Max, Min, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from document.models import Document, DocumentGenerator
from main.models import DepartmentArea, IndexingDatabase, Language, Publication, PublicationType, Tag, Venue
from utils.document_generator import generate_document, generate_docx

User = get_user_model()
logger = logging.getLogger(__name__)
FILTER_PARAM_KEYS = ("type", "lang", "status", "oa", "y_min", "y_max", "db", "quartile", "area", "venue", "staff_user")
REPORT_FORM_KEYS = ("csrfmiddlewaretoken", "action", "template_id", "template_key", "report_title")


def _parse_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_queryset_count(queryset) -> int:
    try:
        return int(queryset.count())
    except Exception:  # noqa: BLE001
        return -1


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


def _has_active_search(params):
    if (params.get("show") or "").strip().lower() == "all":
        return True

    if (params.get("search") or "").strip():
        return True

    for key in FILTER_PARAM_KEYS:
        if (params.get(key) or "").strip():
            return True

    return any(tag_id for tag_id in params.getlist("tags"))


def _extract_selected_tags(params):
    return [str(tag_id) for tag_id in params.getlist("tags") if tag_id]


def _build_filtered_publications(params, selected_tags):
    queryset = (
        Publication.objects.select_related("pub_type", "language", "venue", "area")
        .prefetch_related("tags", "indexing", "authors")
        .all()
    )
    staff_user_id = _parse_int(params.get("staff_user"))
    queryset, staff_user = _apply_staff_user_filter(queryset, staff_user_id)

    search_text = (params.get("search") or "").strip()
    if search_text:
        queryset = queryset.filter(
            Q(title_original__icontains=search_text)
            | Q(doi__icontains=search_text)
            | Q(keywords__icontains=search_text)
            | Q(venue__name__icontains=search_text)
            | Q(authors__full_name__icontains=search_text)
            | Q(record_id__icontains=search_text)
        )

    type_id = _parse_int(params.get("type"))
    if type_id:
        queryset = queryset.filter(pub_type_id=type_id)

    lang_id = _parse_int(params.get("lang"))
    if lang_id:
        queryset = queryset.filter(language_id=lang_id)

    status = params.get("status")
    if status in {"submitted", "accepted", "published"}:
        queryset = queryset.filter(status=status)

    oa = params.get("oa")
    if oa in {"0", "1"}:
        queryset = queryset.filter(open_access=(oa == "1"))

    y_min = _parse_int(params.get("y_min"))
    if y_min:
        queryset = queryset.filter(year__gte=y_min)

    y_max = _parse_int(params.get("y_max"))
    if y_max:
        queryset = queryset.filter(year__lte=y_max)

    db_id = _parse_int(params.get("db"))
    if db_id:
        queryset = queryset.filter(indexing__id=db_id)

    quartile = (params.get("quartile") or "").strip().upper()
    if quartile in {"Q1", "Q2", "Q3", "Q4"}:
        queryset = queryset.filter(quartile=quartile)

    area_id = _parse_int(params.get("area"))
    if area_id:
        queryset = queryset.filter(area_id=area_id)

    venue_id = _parse_int(params.get("venue"))
    if venue_id:
        queryset = queryset.filter(venue_id=venue_id)

    if selected_tags:
        tag_ids = [_parse_int(tag_id) for tag_id in selected_tags]
        tag_ids = [tag_id for tag_id in tag_ids if tag_id]
        if tag_ids:
            queryset = queryset.filter(tags__id__in=tag_ids)

    return queryset.distinct().order_by("-year", "-id"), staff_user


def _available_generators_for_user(user):
    queryset = DocumentGenerator.objects.select_related("user").order_by("title")
    if user.is_authenticated:
        return queryset.filter(Q(access_to_all=True) | Q(user=user)).distinct()
    return queryset.filter(access_to_all=True)


def _available_report_templates_for_user(user):
    return [
        {
            "key": f"g:{template.id}",
            "title": template.title,
            "source": "generator",
        }
        for template in _available_generators_for_user(user)
    ]


def _resolve_generator_template_for_request(user, raw_template_value: str) -> DocumentGenerator | None:
    value = (raw_template_value or "").strip()
    if not value:
        return None

    if value.isdigit():
        return _available_generators_for_user(user).filter(pk=int(value)).first()

    if value.startswith("g:"):
        template_id = _parse_int(value.split(":", 1)[1])
        if not template_id:
            return None
        return _available_generators_for_user(user).filter(pk=template_id).first()
    return None


def _build_report_filename(base_title: str, extension: str) -> str:
    slug = slugify(base_title) or "report"
    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    return f"{slug}-{timestamp}.{extension}"


def _make_generated_document(
    generator_template: DocumentGenerator,
    publications_queryset,
    user,
    requested_title: str = "",
) -> Document:
    publications_count = _safe_queryset_count(publications_queryset)
    logger.info(
        "Generation start template_id=%s user=%s template_file_type=%s publications_count=%s requested_title=%s",
        generator_template.id,
        user.id if user else None,
        generator_template.file_type,
        publications_count,
        (requested_title or "").strip()[:120],
    )
    context = {
        "publications": publications_queryset,
        "user": user,
    }
    rendered_text = generate_document(generator_template, context)
    logger.info(
        "Text render result template_id=%s text_len=%s non_empty=%s",
        generator_template.id,
        len(rendered_text or ""),
        bool((rendered_text or "").strip()),
    )
    title = (requested_title or "").strip() or generator_template.title
    title = title[:255]

    document = Document(
        title=title,
        content=rendered_text,
        user=user,
        generated_by=generator_template,
    )

    docx_generated = False
    docx_error: Exception | None = None
    if generator_template.file and generator_template.file.name.lower().endswith(".docx"):
        logger.info("DOCX render branch template_id=%s source_file=%s", generator_template.id, generator_template.file.name)
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as temp_output:
            output_path = temp_output.name
        try:
            try:
                generate_docx(generator_template, context, output_path)
                with open(output_path, "rb") as generated_file:
                    document.file.save(
                        _build_report_filename(title, "docx"),
                        File(generated_file),
                        save=False,
                    )
                document.file_type = "docx"
                docx_generated = True
            except Exception as exc:  # noqa: BLE001
                docx_error = exc
                logger.exception(
                    "DOCX generation failed for template_id=%s. Falling back to TXT. error=%s",
                    generator_template.id,
                    exc,
                )
                docx_generated = False
        finally:
            try:
                os.remove(output_path)
            except OSError:
                pass

    if not docx_generated:
        fallback_text = rendered_text or ""
        if not fallback_text.strip() and docx_error is not None:
            fallback_text = (
                "DOCX генерациясы сәтсіз аяқталды.\n"
                f"Шаблон ID: {generator_template.id}\n"
                f"Қате: {docx_error}\n"
                "Синоним цикл тегтері мен шаблон құрылымын тексеріңіз.\n"
            )
            document.content = fallback_text
            logger.warning(
                "Generated diagnostic TXT fallback template_id=%s user=%s publications_count=%s",
                generator_template.id,
                user.id if user else None,
                publications_count,
            )
        elif not fallback_text.strip():
            logger.warning(
                "Generated TXT content is empty template_id=%s user=%s publications_count=%s",
                generator_template.id,
                user.id if user else None,
                publications_count,
            )
        text_bytes = fallback_text.encode("utf-8")
        document.file.save(
            _build_report_filename(title, "txt"),
            ContentFile(text_bytes),
            save=False,
        )
        document.file_type = "txt"

    document.save()
    logger.info(
        "Generation finished document_id=%s generated_by=%s file_type=%s file_name=%s",
        document.id,
        generator_template.id,
        document.file_type,
        document.file.name if document.file else "",
    )
    return document


def _attachment_response(file_bytes: bytes, filename: str, content_type: str) -> HttpResponse:
    response = HttpResponse(file_bytes, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _make_generated_response_for_anonymous(
    generator_template: DocumentGenerator,
    publications_queryset,
    requested_title: str = "",
) -> HttpResponse:
    publications_count = _safe_queryset_count(publications_queryset)
    logger.info(
        "Anonymous generation start template_id=%s template_file_type=%s publications_count=%s requested_title=%s",
        generator_template.id,
        generator_template.file_type,
        publications_count,
        (requested_title or "").strip()[:120],
    )
    context = {
        "publications": publications_queryset,
        "user": None,
    }
    rendered_text = generate_document(generator_template, context)
    title = (requested_title or "").strip() or generator_template.title
    title = title[:255]

    if generator_template.file and generator_template.file.name.lower().endswith(".docx"):
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as temp_output:
            output_path = temp_output.name
        try:
            try:
                generate_docx(generator_template, context, output_path)
                with open(output_path, "rb") as generated_file:
                    payload = generated_file.read()
                logger.info(
                    "Anonymous generation finished template_id=%s file_type=docx size=%s",
                    generator_template.id,
                    len(payload),
                )
                return _attachment_response(
                    payload,
                    _build_report_filename(title, "docx"),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Anonymous DOCX generation failed for template_id=%s. Falling back to TXT. error=%s",
                    generator_template.id,
                    exc,
                )
        finally:
            try:
                os.remove(output_path)
            except OSError:
                pass

    fallback_text = rendered_text or ""
    if not fallback_text.strip():
        fallback_text = (
            "Есеп генерациясы сәтсіз аяқталды.\n"
            f"Шаблон ID: {generator_template.id}\n"
            "Шаблон синтаксисі мен синонимдерді тексеріңіз.\n"
        )
        logger.warning(
            "Anonymous TXT fallback produced diagnostic content template_id=%s publications_count=%s",
            generator_template.id,
            publications_count,
        )
    payload = fallback_text.encode("utf-8")
    logger.info(
        "Anonymous generation finished template_id=%s file_type=txt size=%s",
        generator_template.id,
        len(payload),
    )
    return _attachment_response(payload, _build_report_filename(title, "txt"), "text/plain; charset=utf-8")


def _main_query_from_params(params):
    query_params = params.copy()
    query_params.pop("page", None)
    for key in REPORT_FORM_KEYS:
        query_params.pop(key, None)
    return query_params.urlencode()


def _redirect_main_with_filters(params):
    query = _main_query_from_params(params)
    target = reverse("main")
    if query:
        target = f"{target}?{query}"
    return redirect(target)


def _handle_report_generation(request):
    user_id = request.user.id if request.user.is_authenticated else None
    selected_template = (request.POST.get("template_key") or request.POST.get("template_id") or "").strip()
    if not selected_template:
        logger.warning("Generation cancelled: template key missing user=%s", user_id)
        return _redirect_main_with_filters(request.POST)

    generator_template = _resolve_generator_template_for_request(request.user, selected_template)
    if not generator_template:
        logger.warning(
            "Generation cancelled: template not resolved user=%s selected_template=%s",
            user_id,
            selected_template,
        )
        return _redirect_main_with_filters(request.POST)

    selected_tags = _extract_selected_tags(request.POST)
    publications_queryset, _ = _build_filtered_publications(request.POST, selected_tags)
    requested_title = (request.POST.get("report_title") or "").strip()
    logger.info(
        "Generation request accepted user=%s template_id=%s filters_tags=%s",
        user_id,
        generator_template.id,
        len(selected_tags),
    )

    if not request.user.is_authenticated:
        return _make_generated_response_for_anonymous(
            generator_template=generator_template,
            publications_queryset=publications_queryset,
            requested_title=requested_title,
        )

    document = _make_generated_document(
        generator_template=generator_template,
        publications_queryset=publications_queryset,
        user=request.user,
        requested_title=requested_title,
    )
    if not document.file:
        return redirect("document_detail", pk=document.pk)
    return redirect(f"{reverse('document_file', kwargs={'pk': document.pk})}?download=1")


def main_page(request):
    if request.method == "POST" and request.POST.get("action") == "generate_report":
        return _handle_report_generation(request)

    params = request.GET
    show_results = _has_active_search(params)
    staff_user = None
    selected_tags = _extract_selected_tags(params)

    if show_results:
        qs, staff_user = _build_filtered_publications(params, selected_tags)
        total_count = qs.count()
        paginator = Paginator(qs, 20)
        page_obj = paginator.get_page(params.get("page"))
    else:
        qs = Publication.objects.none()
        total_count = 0
        paginator = Paginator(qs, 20)
        page_obj = paginator.get_page(1)

    base_query = _main_query_from_params(params)

    years = Publication.objects.aggregate(min=Min("year"), max=Max("year"))
    current_year = date.today().year
    quick_year_from = current_year - 5
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
        "show_results": show_results,
        "quick_year_from": quick_year_from,
        "report_templates": _available_report_templates_for_user(request.user),
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

