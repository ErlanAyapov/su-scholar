import logging
import os
import tempfile
from datetime import date

from django.contrib.auth import get_user_model
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Count, Max, Min, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from account.models import Department, Institute, University
from document.models import Document, DocumentGenerator
from main.models import DepartmentArea, IndexingDatabase, Language, NewsItem, Project, Publication, PublicationType, Tag, Venue
from utils.document_generator import generate_document, generate_docx

User = get_user_model()
logger = logging.getLogger(__name__)
FILTER_PARAM_KEYS = ("type", "lang", "status", "oa", "y_min", "y_max", "db", "quartile", "area", "venue", "staff_user")
REPORT_FORM_KEYS = ("csrfmiddlewaretoken", "action", "template_id", "template_key", "report_title")


def parse_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_queryset_count(queryset) -> int:
    try:
        return int(queryset.count())
    except Exception:  # noqa: BLE001
        return -1


def apply_staff_user_filter(queryset, staff_user_id):
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


def has_active_search(params):
    if (params.get("show") or "").strip().lower() == "all":
        return True

    if (params.get("search") or "").strip():
        return True

    for key in FILTER_PARAM_KEYS:
        if (params.get(key) or "").strip():
            return True

    return any(tag_id for tag_id in params.getlist("tags"))


def resolve_search_tab(params) -> str:
    raw_tab = (params.get("tab") or "").strip().lower()
    return "researchers" if raw_tab == "researchers" else "documents"


def extract_selected_tags(params):
    return [str(tag_id) for tag_id in params.getlist("tags") if tag_id]


def filter_publications_case_insensitive(queryset, search_text: str):
    text = (search_text or "").strip()
    if not text:
        return queryset

    if connection.vendor != "sqlite":
        return queryset.filter(
            Q(title_original__icontains=text)
            | Q(doi__icontains=text)
            | Q(keywords__icontains=text)
            | Q(venue__name__icontains=text)
            | Q(authors__full_name__icontains=text)
            | Q(record_id__icontains=text)
        )

    search_fold = text.casefold()
    matched_ids: list[int] = []
    for publication in queryset:
        venue_name = publication.venue.name if getattr(publication, "venue", None) else ""
        author_names = " ".join(author.full_name or "" for author in publication.authors.all())
        haystack = " ".join(
            [
                publication.title_original or "",
                publication.doi or "",
                publication.keywords or "",
                venue_name,
                author_names,
                publication.record_id or "",
            ]
        ).casefold()
        if search_fold in haystack:
            matched_ids.append(publication.id)

    if not matched_ids:
        return queryset.none()
    return queryset.filter(id__in=matched_ids)


def build_filtered_publications(params, selected_tags):
    queryset = (
        Publication.objects.select_related("pub_type", "language", "venue", "area")
        .prefetch_related("tags", "indexing", "authors")
        .all()
    )
    staff_user_id = parse_int(params.get("staff_user"))
    queryset, staff_user = apply_staff_user_filter(queryset, staff_user_id)

    search_text = (params.get("search") or "").strip()

    type_id = parse_int(params.get("type"))
    if type_id:
        queryset = queryset.filter(pub_type_id=type_id)

    lang_id = parse_int(params.get("lang"))
    if lang_id:
        queryset = queryset.filter(language_id=lang_id)

    status = params.get("status")
    if status in {"submitted", "accepted", "published"}:
        queryset = queryset.filter(status=status)

    oa = params.get("oa")
    if oa in {"0", "1"}:
        queryset = queryset.filter(open_access=(oa == "1"))

    y_min = parse_int(params.get("y_min"))
    if y_min:
        queryset = queryset.filter(year__gte=y_min)

    y_max = parse_int(params.get("y_max"))
    if y_max:
        queryset = queryset.filter(year__lte=y_max)

    db_id = parse_int(params.get("db"))
    if db_id:
        queryset = queryset.filter(indexing__id=db_id)

    quartile = (params.get("quartile") or "").strip().upper()
    if quartile in {"Q1", "Q2", "Q3", "Q4"}:
        queryset = queryset.filter(quartile=quartile)

    area_id = parse_int(params.get("area"))
    if area_id:
        queryset = queryset.filter(area_id=area_id)

    venue_id = parse_int(params.get("venue"))
    if venue_id:
        queryset = queryset.filter(venue_id=venue_id)

    if selected_tags:
        tag_ids = [parse_int(tag_id) for tag_id in selected_tags]
        tag_ids = [tag_id for tag_id in tag_ids if tag_id]
        if tag_ids:
            queryset = queryset.filter(tags__id__in=tag_ids)

    if search_text:
        queryset = filter_publications_case_insensitive(queryset, search_text)

    return queryset.distinct().order_by("-year", "-id"), staff_user


def available_generators_for_user(user):
    queryset = DocumentGenerator.objects.select_related("user").order_by("title")
    if user.is_authenticated:
        return queryset.filter(Q(access_to_all=True) | Q(user=user)).distinct()
    return queryset.filter(access_to_all=True)


def available_report_templates_for_user(user):
    return [
        {
            "key": f"g:{template.id}",
            "title": template.title,
            "source": "generator",
        }
        for template in available_generators_for_user(user)
    ]


def resolve_generator_template_for_request(user, raw_template_value: str) -> DocumentGenerator | None:
    value = (raw_template_value or "").strip()
    if not value:
        return None

    if value.isdigit():
        return available_generators_for_user(user).filter(pk=int(value)).first()

    if value.startswith("g:"):
        template_id = parse_int(value.split(":", 1)[1])
        if not template_id:
            return None
        return available_generators_for_user(user).filter(pk=template_id).first()
    return None


def build_report_filename(base_title: str, extension: str) -> str:
    slug = slugify(base_title) or "report"
    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    return f"{slug}-{timestamp}.{extension}"


def query_from_params(params, exclude_keys=()):
    query_params = params.copy()
    for key in exclude_keys:
        query_params.pop(key, None)
    return query_params.urlencode()


def make_generated_document(
    generator_template: DocumentGenerator,
    publications_queryset,
    user,
    requested_title: str = "",
) -> Document:
    publications_count = safe_queryset_count(publications_queryset)
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
                        build_report_filename(title, "docx"),
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
            build_report_filename(title, "txt"),
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


def attachment_response(file_bytes: bytes, filename: str, content_type: str) -> HttpResponse:
    response = HttpResponse(file_bytes, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def make_generated_response_for_anonymous(
    generator_template: DocumentGenerator,
    publications_queryset,
    requested_title: str = "",
) -> HttpResponse:
    publications_count = safe_queryset_count(publications_queryset)
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
                return attachment_response(
                    payload,
                    build_report_filename(title, "docx"),
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
    return attachment_response(payload, build_report_filename(title, "txt"), "text/plain; charset=utf-8")


def main_query_from_params(params):
    return query_from_params(
        params,
        exclude_keys=("page", *REPORT_FORM_KEYS),
    )


def redirect_main_with_filters(params):
    query = main_query_from_params(params)
    target = reverse("main_search")
    if query:
        target = f"{target}?{query}"
    return redirect(target)


def handle_report_generation(request):
    user_id = request.user.id if request.user.is_authenticated else None
    selected_template = (request.POST.get("template_key") or request.POST.get("template_id") or "").strip()
    if not selected_template:
        logger.warning("Generation cancelled: template key missing user=%s", user_id)
        return redirect_main_with_filters(request.POST)

    generator_template = resolve_generator_template_for_request(request.user, selected_template)
    if not generator_template:
        logger.warning(
            "Generation cancelled: template not resolved user=%s selected_template=%s",
            user_id,
            selected_template,
        )
        return redirect_main_with_filters(request.POST)

    selected_tags = extract_selected_tags(request.POST)
    publications_queryset, _ = build_filtered_publications(request.POST, selected_tags)
    requested_title = (request.POST.get("report_title") or "").strip()
    logger.info(
        "Generation request accepted user=%s template_id=%s filters_tags=%s",
        user_id,
        generator_template.id,
        len(selected_tags),
    )

    if not request.user.is_authenticated:
        return make_generated_response_for_anonymous(
            generator_template=generator_template,
            publications_queryset=publications_queryset,
            requested_title=requested_title,
        )

    document = make_generated_document(
        generator_template=generator_template,
        publications_queryset=publications_queryset,
        user=request.user,
        requested_title=requested_title,
    )
    if not document.file:
        return redirect("document_detail", pk=document.pk)
    return redirect(f"{reverse('document_file', kwargs={'pk': document.pk})}?download=1")


def build_main_page_context(request):
    params = request.GET
    show_results = has_active_search(params)
    staff_user = None
    selected_tags = extract_selected_tags(params)

    if show_results:
        qs, staff_user = build_filtered_publications(params, selected_tags)
        total_count = qs.count()
        paginator = Paginator(qs, 20)
        page_obj = paginator.get_page(params.get("page"))
    else:
        qs = Publication.objects.none()
        total_count = 0
        paginator = Paginator(qs, 20)
        page_obj = paginator.get_page(1)

    base_query = main_query_from_params(params)

    years = Publication.objects.aggregate(min=Min("year"), max=Max("year"))
    current_year = date.today().year
    quick_year_from = current_year - 5
    recent_publications = (
        Publication.objects.filter(created_by=40).select_related("pub_type", "venue")
        .prefetch_related("authors")
        .order_by("-year", "-id")[:5]
    )
    return {
        "pub_types": PublicationType.objects.annotate(c=Count("publication", distinct=True)).order_by("-c", "name"),
        "languages": Language.objects.annotate(c=Count("publication", distinct=True)).order_by("-c", "name"),
        "indexing_dbs": IndexingDatabase.objects.annotate(c=Count("publication", distinct=True)).order_by("-c", "name"),
        "areas": DepartmentArea.objects.annotate(c=Count("publication", distinct=True)).order_by("-c", "name"),
        "tags_top": Tag.objects.annotate(c=Count("publication", distinct=True)).order_by("-c", "name")[:20],
        "venues_top": Venue.objects.annotate(c=Count("publications", distinct=True)).order_by("-c", "name")[:15],
        "news_items": NewsItem.objects.prefetch_related("media").order_by("-publication_date", "-id")[:3],
        "recent_publications": recent_publications,
        "home_stats": {
            "researchers": User.objects.count(),
            "publications": Publication.objects.count(),
            "projects": Project.objects.count(),
            "venues": Venue.objects.count(),
        },
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
        "report_templates": available_report_templates_for_user(request.user),
    }


def build_researchers_page_context(request):
    params = request.GET
    last_name = (params.get("researcher_last_name") or "").strip()
    first_name = (params.get("researcher_first_name") or "").strip()
    keyword = (params.get("research_q") or "").strip()
    university_id = parse_int(params.get("researcher_university"))
    institute_id = parse_int(params.get("researcher_institute"))
    department_id = parse_int(params.get("researcher_department"))
    gender = (params.get("researcher_gender") or "").strip().lower()
    staff_scope = (params.get("researcher_scope") or "").strip().lower()
    has_orcid = (params.get("researcher_has_orcid") or "").strip()
    has_scopus = (params.get("researcher_has_scopus") or "").strip()
    has_scholar = (params.get("researcher_has_scholar") or "").strip()

    selected_department = None
    if department_id:
        selected_department = (
            Department.objects.select_related("institute__university")
            .filter(id=department_id)
            .first()
        )
        if selected_department:
            if not institute_id:
                institute_id = selected_department.institute_id
            if not university_id:
                university_id = selected_department.institute.university_id

    if institute_id and not university_id:
        selected_institute = (
            Institute.objects.select_related("university")
            .filter(id=institute_id)
            .first()
        )
        if selected_institute:
            university_id = selected_institute.university_id

    show_results = any(
        [
            last_name,
            first_name,
            keyword,
            university_id,
            institute_id,
            department_id,
            gender in {"male", "female"},
            staff_scope in {"staff", "users"},
            has_orcid == "1",
            has_scopus == "1",
            has_scholar == "1",
        ]
    )

    queryset = User.objects.select_related("department__institute__university").annotate(
        publication_total=Count("created_publications", distinct=True)
    )

    if staff_scope == "staff":
        queryset = queryset.filter(is_user=False)
    elif staff_scope == "users":
        queryset = queryset.filter(is_user=True)

    if last_name:
        queryset = queryset.filter(last_name__icontains=last_name)

    if first_name:
        queryset = queryset.filter(first_name__icontains=first_name)

    if keyword:
        queryset = queryset.filter(
            Q(username__icontains=keyword)
            | Q(email__icontains=keyword)
            | Q(father_name__icontains=keyword)
            | Q(orc_id__icontains=keyword)
            | Q(scopus_id__icontains=keyword)
            | Q(wos_id__icontains=keyword)
        )

    if university_id:
        queryset = queryset.filter(department__institute__university_id=university_id)

    if institute_id:
        queryset = queryset.filter(department__institute_id=institute_id)

    if department_id:
        queryset = queryset.filter(department_id=department_id)

    if gender in {"male", "female"}:
        queryset = queryset.filter(gender=gender)

    if has_orcid == "1":
        queryset = queryset.exclude(orc_id="")

    if has_scopus == "1":
        queryset = queryset.exclude(scopus_id="")

    if has_scholar == "1":
        queryset = queryset.exclude(google_scholar="")

    queryset = queryset.order_by("last_name", "first_name", "id")
    if show_results:
        total_count = queryset.count()
        paginator = Paginator(queryset, 20)
        page_obj = paginator.get_page(params.get("page"))
    else:
        total_count = 0
        empty_qs = User.objects.none()
        paginator = Paginator(empty_qs, 20)
        page_obj = paginator.get_page(1)

    return {
        "research_universities": University.objects.order_by("name"),
        "research_institutes": Institute.objects.select_related("university").order_by("name"),
        "research_departments": Department.objects.select_related("institute__university").order_by("name"),
        "researcher_results": page_obj.object_list,
        "researcher_page_obj": page_obj,
        "researcher_total_count": total_count,
        "researcher_show_results": show_results,
        "researcher_base_query": query_from_params(params, exclude_keys=("page",)),
        "researcher_last_name": last_name,
        "researcher_first_name": first_name,
        "research_q": keyword,
        "researcher_university": university_id,
        "researcher_institute": institute_id,
        "researcher_department": department_id,
        "researcher_gender": gender,
        "researcher_scope": staff_scope,
        "researcher_has_orcid": has_orcid,
        "researcher_has_scopus": has_scopus,
        "researcher_has_scholar": has_scholar,
    }


def build_search_page_context(request):
    active_tab = resolve_search_tab(request.GET)
    context = {
        "active_tab": active_tab,
    }
    if active_tab == "researchers":
        context.update(build_researchers_page_context(request))
        return context

    context.update(build_main_page_context(request))
    return context


def build_search_suggestions_payload(request):
    query = (request.GET.get("q") or "").strip()
    if len(query) < 3:
        return {"suggestions": []}

    staff_user_id = parse_int(request.GET.get("staff_user"))
    base_qs, _ = apply_staff_user_filter(Publication.objects.all(), staff_user_id)

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
        base_qs.filter(title_original__icontains=query).values_list("title_original", flat=True)[:5],
        kind="title",
    ):
        return {"suggestions": suggestions}

    if add_suggestions(
        base_qs.filter(authors__full_name__icontains=query)
        .exclude(authors__full_name="")
        .values_list("authors__full_name", flat=True)
        .distinct()[:3],
        kind="author",
        prefix="Автор: ",
    ):
        return {"suggestions": suggestions}

    if add_suggestions(
        base_qs.filter(venue__name__icontains=query)
        .exclude(venue__name="")
        .values_list("venue__name", flat=True)
        .distinct()[:3],
        kind="venue",
        prefix="Дереккөз: ",
    ):
        return {"suggestions": suggestions}

    add_suggestions(
        base_qs.filter(doi__icontains=query)
        .exclude(doi="")
        .values_list("doi", flat=True)
        .distinct()[:3],
        kind="doi",
        prefix="DOI: ",
    )

    return {"suggestions": suggestions}


def build_publication_detail_context(pk: int):
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
    author_links = [
        link
        for link in publication.publicationauthor_set.select_related("author").order_by("order", "id")
        if link.author and (link.author.full_name or "").strip()
    ]

    return {
        "publication": publication,
        "author_links": author_links,
        "project_links": publication.publicationproject_set.all(),
    }
