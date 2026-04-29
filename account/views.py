import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from celery.result import AsyncResult
from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes, force_str
from django.utils import timezone
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode, urlsafe_base64_encode

from document.models import Document
from main.models import Author, Project, Publication, PublicationAuthor, PublicationProject
from main.tasks import sync_user_profile_full_cycle_task, sync_user_publications_task
from main.utils import (
    build_filtered_publications,
    extract_selected_tags,
    make_generated_document,
    make_generated_response_for_anonymous,
    resolve_generator_template_for_request,
)

from .forms import (
    ActivationSetPasswordForm,
    InactiveUserCreateForm,
    LoginForm,
    ProfileEditForm,
    ProfileSyncFieldsForm,
    RegisterForm,
)


User = get_user_model()
PAGE_SIZE = 20
GRANT_PROJECT_TYPES = {"gf", "pcf"}
_PERIOD_LABEL = "\u043a \u043f\u0440\u043e\u0448\u043b\u043e\u043c\u0443 \u043f\u0435\u0440\u0438\u043e\u0434\u0443"
_FUNDING_VALUE_RE = re.compile(
    r"(?P<currency>\u20b8|kzt|\u0442\u0435\u043d\u0433\u0435|\u0442\u0433)?\s*"
    r"(?P<amount>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>\u043c\u043b\u0440\u0434|\u043c\u043b\u043d|\u0442\u044b\u0441|billion|million|mln|bn|thousand|k)?",
    re.IGNORECASE,
)


def _normalize_safe_next_url(request, next_url: str, default_url: str) -> str:
    candidate = (next_url or "").strip()
    if not candidate:
        return default_url
    if url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return candidate
    return default_url


def _get_safe_next_url(request, default_url: str) -> str:
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    return _normalize_safe_next_url(request, next_url, default_url)


def _get_public_base_url(request) -> str:
    app_public_url = str(getattr(settings, "APP_PUBLIC_URL", "") or "").strip()
    if app_public_url:
        return app_public_url.rstrip("/")
    return request.build_absolute_uri("/").rstrip("/")


def _build_public_url(request, path: str) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{_get_public_base_url(request)}{normalized_path}"


def _user_from_uid(uidb64: str):
    try:
        user_id = force_str(urlsafe_base64_decode(uidb64))
    except (TypeError, ValueError, OverflowError):
        return None
    return User.objects.filter(pk=user_id).first()


def _employees_queryset():
    return User.objects.filter(is_user=False).order_by(
        'last_name',
        'first_name',
        'father_name',
        'id',
    )


def _employee_publications_queryset(profile_user):
    full_name_parts = [profile_user.last_name.strip(), profile_user.first_name.strip()]
    author_filter = Q()
    for part in full_name_parts:
        if part:
            author_filter &= Q(authors__full_name__icontains=part)

    publication_filter = Q(created_by=profile_user)
    publication_filter |= Q(authors__user=profile_user)
    if author_filter:
        publication_filter |= author_filter
    if profile_user.orc_id:
        publication_filter |= Q(authors__orcid__iexact=profile_user.orc_id)

    return (
        Publication.objects.filter(publication_filter)
        .select_related("venue", "pub_type", "language", "created_by")
        .prefetch_related("authors", "indexing", "tags")
        .distinct()
        .order_by("-year", "-id")
    )


def _format_int(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def _format_percent_change(current, previous) -> str:
    current_decimal = Decimal(str(current or 0))
    previous_decimal = Decimal(str(previous or 0))

    if previous_decimal <= 0:
        return "0%" if current_decimal <= 0 else "+100%"

    delta = ((current_decimal - previous_decimal) / previous_decimal) * Decimal("100")
    rounded = int(delta.quantize(Decimal("1")))
    sign = "+" if rounded >= 0 else ""
    return f"{sign}{rounded}%"


def _format_absolute_change(current, previous) -> str:
    delta = int(current or 0) - int(previous or 0)
    return f"{delta:+d}"


def _format_change_caption(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or normalized == "--":
        return "--"
    return f"{normalized} {_PERIOD_LABEL}"


def _parse_funding_amount_mln(text: str):
    source = str(text or "").strip().lower()
    if not source:
        return None

    for match in _FUNDING_VALUE_RE.finditer(source):
        unit = (match.group("unit") or "").strip()
        if not unit:
            continue

        amount_raw = (match.group("amount") or "").replace(",", ".")
        try:
            amount = Decimal(amount_raw)
        except (InvalidOperation, ValueError):
            continue

        if unit in {"\u043c\u043b\u0440\u0434", "billion", "bn"}:
            return amount * Decimal("1000")
        if unit in {"\u0442\u044b\u0441", "thousand", "k"}:
            return amount * Decimal("0.001")
        return amount
    return None


def _extract_project_funding_mln(project: Project):
    for source in (project.funding_source, project.name):
        amount_mln = _parse_funding_amount_mln(source)
        if amount_mln is not None:
            return amount_mln
    return None


def _format_funding_mln(value):
    if value is None:
        return "--"

    amount_mln = Decimal(str(value))
    if amount_mln < 0:
        amount_mln = Decimal("0")

    if amount_mln >= Decimal("1000"):
        amount_bln = amount_mln / Decimal("1000")
        if amount_bln >= Decimal("10"):
            raw = f"{int(amount_bln.quantize(Decimal('1'))):,}".replace(",", " ")
        else:
            raw = format(amount_bln.quantize(Decimal("0.1")).normalize(), "f")
        return f"{raw} \u043c\u043b\u0440\u0434"

    if amount_mln >= Decimal("100"):
        raw = f"{int(amount_mln.quantize(Decimal('1'))):,}".replace(",", " ")
        return f"{raw} \u043c\u043b\u043d"

    raw = format(amount_mln.quantize(Decimal("0.1")).normalize(), "f")
    return f"{raw} \u043c\u043b\u043d"


def _collect_profile_dashboard_metrics(profile_user, publications_qs):
    publication_ids = list(
        publications_qs.order_by()
        .values_list("id", flat=True)
        .distinct()
    )
    publication_count = publications_qs.count()
    publication_stats = publications_qs.aggregate(
        citations_total=Sum("citations_count"),
        open_access_count=Count("id", filter=Q(open_access=True), distinct=True),
    )
    citations_total = int(publication_stats["citations_total"] or 0)
    open_access_count = int(publication_stats["open_access_count"] or 0)
    open_access_share = round((open_access_count / publication_count) * 100) if publication_count else 0

    yearly_rows = list(
        publications_qs.order_by()
        .values("year")
        .annotate(
            publication_total=Count("id", distinct=True),
            citations_total=Sum("citations_count"),
        )
        .order_by("-year")
    )
    current_year = yearly_rows[0]["year"] if yearly_rows else timezone.now().year
    previous_year = current_year - 1
    yearly_map = {int(row["year"]): row for row in yearly_rows if row.get("year") is not None}

    current_publication_total = int((yearly_map.get(current_year) or {}).get("publication_total") or 0)
    previous_publication_total = int((yearly_map.get(previous_year) or {}).get("publication_total") or 0)
    current_citations_total = int((yearly_map.get(current_year) or {}).get("citations_total") or 0)
    previous_citations_total = int((yearly_map.get(previous_year) or {}).get("citations_total") or 0)

    publication_change = _format_percent_change(current_publication_total, previous_publication_total)
    citations_change = _format_percent_change(current_citations_total, previous_citations_total)

    publication_project_links = list(
        PublicationProject.objects.filter(publication_id__in=publication_ids)
        .values("project_id", "publication__year")
        .distinct()
    )
    project_ids = sorted(
        {
            int(link["project_id"])
            for link in publication_project_links
            if link.get("project_id") is not None
        }
    )
    project_items = list(Project.objects.filter(id__in=project_ids).only("id", "name", "project_type", "funding_source"))
    projects_by_id = {int(project.id): project for project in project_items}

    grant_ids = {
        project_id
        for project_id, project in projects_by_id.items()
        if project.project_type in GRANT_PROJECT_TYPES
    }

    projects_by_year = {}
    grants_by_year = {}
    for link in publication_project_links:
        project_id = link.get("project_id")
        year = link.get("publication__year")
        if project_id is None or year is None:
            continue
        project_id = int(project_id)
        year = int(year)
        projects_by_year.setdefault(year, set()).add(project_id)
        if project_id in grant_ids:
            grants_by_year.setdefault(year, set()).add(project_id)

    grant_count = len(grant_ids)
    current_grants_total = len(grants_by_year.get(current_year, set()))
    previous_grants_total = len(grants_by_year.get(previous_year, set()))
    grant_change = _format_absolute_change(current_grants_total, previous_grants_total)

    funding_by_project_mln = {}
    for project_id, project in projects_by_id.items():
        funding_amount_mln = _extract_project_funding_mln(project)
        if funding_amount_mln is not None:
            funding_by_project_mln[project_id] = funding_amount_mln

    funding_total_mln = None
    funding_change = "--"
    if funding_by_project_mln:
        funding_total_mln = sum(funding_by_project_mln.values(), Decimal("0"))
        current_funding_mln = sum(
            (
                funding_by_project_mln[project_id]
                for project_id in projects_by_year.get(current_year, set())
                if project_id in funding_by_project_mln
            ),
            Decimal("0"),
        )
        previous_funding_mln = sum(
            (
                funding_by_project_mln[project_id]
                for project_id in projects_by_year.get(previous_year, set())
                if project_id in funding_by_project_mln
            ),
            Decimal("0"),
        )
        funding_change = _format_percent_change(current_funding_mln, previous_funding_mln)

    collaborators_count = (
        Author.objects.filter(publications__id__in=publication_ids)
        .exclude(user=profile_user)
        .distinct()
        .count()
    )

    metric_texts = {
        "profileMetricPublicationsValue": _format_int(publication_count),
        "profileMetricPublicationsDelta": _format_change_caption(publication_change),
        "profileMetricCitationsValue": _format_int(citations_total),
        "profileMetricCitationsDelta": _format_change_caption(citations_change),
        "profileMetricGrantsValue": _format_int(grant_count),
        "profileMetricGrantsDelta": _format_change_caption(grant_change),
        "profileMetricFundingValue": _format_funding_mln(funding_total_mln),
        "profileMetricFundingDelta": _format_change_caption(funding_change),
        "profileFinanceProjectsCount": _format_int(len(project_ids)),
        "profileFinanceFundingTotal": _format_funding_mln(funding_total_mln),
        "profileFinanceCollaboratorsCount": _format_int(collaborators_count),
        "profileFinanceOpenAccessShare": f"{open_access_share}%",
    }

    return {
        "publication_count": publication_count,
        "citations_total": citations_total,
        "open_access_count": open_access_count,
        "grant_count": grant_count,
        "projects_total": len(project_ids),
        "collaborators_total": collaborators_count,
        "open_access_share": open_access_share,
        "publication_count_display": metric_texts["profileMetricPublicationsValue"],
        "citations_total_display": metric_texts["profileMetricCitationsValue"],
        "grant_count_display": metric_texts["profileMetricGrantsValue"],
        "funding_total_display": metric_texts["profileMetricFundingValue"],
        "publication_change_caption": metric_texts["profileMetricPublicationsDelta"],
        "citations_change_caption": metric_texts["profileMetricCitationsDelta"],
        "grant_change_caption": metric_texts["profileMetricGrantsDelta"],
        "funding_change_caption": metric_texts["profileMetricFundingDelta"],
        "projects_total_display": metric_texts["profileFinanceProjectsCount"],
        "collaborators_total_display": metric_texts["profileFinanceCollaboratorsCount"],
        "open_access_share_display": metric_texts["profileFinanceOpenAccessShare"],
        "metric_texts": metric_texts,
    }


def _can_manage_profile_sync(request_user, profile_user) -> bool:
    return bool(
        request_user
        and request_user.is_authenticated
        and (request_user.id == profile_user.id or request_user.is_staff)
    )


def _sync_profile_field_values(profile_user) -> dict[str, str]:
    return {
        "scopus_id": str(profile_user.scopus_id or "").strip(),
        "wos_id": str(profile_user.wos_id or "").strip(),
        "google_scholar": str(profile_user.google_scholar or "").strip(),
        "researchgate": str(profile_user.researchgate or "").strip(),
        "satbayev_profile_url": str(profile_user.satbayev_profile_url or "").strip(),
    }


def _sync_profile_availability(profile_user) -> dict:
    values = _sync_profile_field_values(profile_user)
    has_scopus = bool(values["scopus_id"])
    has_wos = bool(values["wos_id"])
    has_scholar = bool(values["google_scholar"])
    has_researchgate = bool(values["researchgate"])
    has_satbayev = bool(values["satbayev_profile_url"])
    has_any = has_scopus or has_wos or has_scholar or has_researchgate or has_satbayev
    import_ready = has_scopus or has_wos or has_scholar or bool(str(profile_user.orc_id or "").strip())
    only_satbayev = has_satbayev and not (has_scopus or has_wos or has_scholar or has_researchgate)

    return {
        "has_any": has_any,
        "import_ready": import_ready,
        "only_satbayev": only_satbayev,
        "has_scopus": has_scopus,
        "has_wos": has_wos,
        "has_scholar": has_scholar,
        "has_researchgate": has_researchgate,
        "has_satbayev": has_satbayev,
        "has_orcid": bool(str(profile_user.orc_id or "").strip()),
        "missing_fields": [
            key
            for key, present in (
                ("scopus_id", has_scopus),
                ("wos_id", has_wos),
                ("google_scholar", has_scholar),
                ("researchgate", has_researchgate),
                ("satbayev_profile_url", has_satbayev),
            )
            if not present
        ],
        "values": values,
    }


def _decode_request_payload(request) -> dict:
    payload = {}
    content_type = (request.content_type or "").split(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
    return payload


def _reset_employee_profile_data(profile_user) -> dict:
    publication_ids = list(
        _employee_publications_queryset(profile_user)
        .values_list("id", flat=True)
        .distinct()
    )
    affected_author_ids = set()
    affected_project_ids = set()

    if publication_ids:
        affected_author_ids = set(
            PublicationAuthor.objects.filter(publication_id__in=publication_ids).values_list("author_id", flat=True)
        )
        affected_project_ids = set(
            PublicationProject.objects.filter(publication_id__in=publication_ids).values_list("project_id", flat=True)
        )

    publications_deleted = len(publication_ids)
    if publication_ids:
        Publication.objects.filter(id__in=publication_ids).delete()

    used_author_ids = set(
        PublicationAuthor.objects.filter(author_id__in=affected_author_ids).values_list("author_id", flat=True)
    )
    orphan_author_ids = sorted(set(affected_author_ids) - used_author_ids)
    authors_deleted = len(orphan_author_ids)
    if orphan_author_ids:
        Author.objects.filter(id__in=orphan_author_ids).delete()

    used_project_ids = set(
        PublicationProject.objects.filter(project_id__in=affected_project_ids).values_list("project_id", flat=True)
    )
    orphan_project_ids = sorted(set(affected_project_ids) - used_project_ids)
    projects_deleted = len(orphan_project_ids)
    if orphan_project_ids:
        Project.objects.filter(id__in=orphan_project_ids).delete()

    clear_fields = {
        "orc_id": "",
        "scopus_id": "",
        "wos_id": "",
        "researchgate": "",
        "google_scholar": "",
        "satbayev_profile_url": "",
        "journal_links": [],
        "journal_ids": [],
    }
    for field_name, value in clear_fields.items():
        setattr(profile_user, field_name, value)
    profile_user.save(update_fields=list(clear_fields.keys()))

    return {
        "publications_deleted": publications_deleted,
        "authors_deleted": authors_deleted,
        "projects_deleted": projects_deleted,
        "cleared_fields": sorted(clear_fields.keys()),
    }


@login_required(login_url="account_login")
def employee_create(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Forbidden")

    form = InactiveUserCreateForm(request.POST or None)
    created_user = None

    if request.method == "POST" and form.is_valid():
        created_user = form.save()
        return redirect(
            f"{reverse('employee_create')}?created=1&user_id={created_user.id}"
        )

    created = request.GET.get("created") == "1"
    created_id_raw = (request.GET.get("user_id") or "").strip()
    created_profile_url = ""
    if created and created_id_raw.isdigit():
        created_profile_url = reverse("employee_profile", kwargs={"user_id": int(created_id_raw)})

    return render(
        request,
        "account/employee_create.html",
        {
            "form": form,
            "created": created,
            "created_profile_url": created_profile_url,
        },
    )


def employees_list(request):
    paginator = Paginator(_employees_queryset(), PAGE_SIZE)
    page_obj = paginator.get_page(1)
    return render(
        request,
        'account/staff_list.html',
        {
            'page_obj': page_obj,
            'next_page': page_obj.next_page_number() if page_obj.has_next() else None,
        },
    )


def employees_list_chunk(request):
    page_number = request.GET.get('page', 1)
    paginator = Paginator(_employees_queryset(), PAGE_SIZE)
    page_obj = paginator.get_page(page_number)

    html = ''.join(
        render_to_string('account/user_card.html', {'user': user}, request=request)
        for user in page_obj.object_list
    )

    return JsonResponse(
        {
            'html': html,
            'has_next': page_obj.has_next(),
            'next_page': page_obj.next_page_number() if page_obj.has_next() else None,
        }
    )


def employee_profile(request, user_id: int):
    profile_user = get_object_or_404(
        User.objects.select_related("department__institute__university").prefetch_related("universities", "roles"),
        pk=user_id,
    )
    can_edit_profile = bool(request.user.is_authenticated and request.user.id == profile_user.id)
    can_manage_sync = _can_manage_profile_sync(request.user, profile_user)
    profile_form = None
    profile_sync_form = None
    profile_updated = request.GET.get("updated") == "1"
    sync_modal_open = request.GET.get("sync_open") == "1"

    if can_edit_profile:
        if request.method == "POST":
            profile_form = ProfileEditForm(request.POST, request.FILES, instance=profile_user)
            if profile_form.is_valid():
                profile_form.save()
                profile_url = reverse("employee_profile", kwargs={"user_id": profile_user.id})
                return redirect(f"{profile_url}?updated=1")
        else:
            profile_form = ProfileEditForm(instance=profile_user)

    if can_manage_sync:
        profile_sync_form = ProfileSyncFieldsForm(instance=profile_user)

    publications_qs = _employee_publications_queryset(profile_user)
    dashboard_metrics = _collect_profile_dashboard_metrics(profile_user, publications_qs)
    top_venues = (
        publications_qs.order_by()
        .exclude(venue__name="")
        .values("venue__name")
        .annotate(total=Count("id", distinct=True))
        .order_by("-total", "venue__name")[:5]
    )

    recent_publications = list(publications_qs[:5])
    generated_documents_qs = (
        Document.objects.filter(user=profile_user, is_deleted=False)
        .select_related("generated_by")
        .order_by("-updated_at", "-id")
    )

    external_links = []
    if profile_user.satbayev_profile_url:
        external_links.append({"label": "Satbayev", "url": profile_user.satbayev_profile_url})
    if profile_user.scopus_id:
        external_links.append(
            {
                "label": "Scopus",
                "url": f"https://www.scopus.com/authid/detail.uri?authorId={profile_user.scopus_id}",
            }
        )
    if profile_user.orc_id:
        external_links.append({"label": "ORCID", "url": f"https://orcid.org/{profile_user.orc_id}"})
    if profile_user.google_scholar:
        external_links.append({"label": "Google Scholar", "url": profile_user.google_scholar})
    if profile_user.researchgate:
        external_links.append({"label": "ResearchGate", "url": profile_user.researchgate})
    if profile_user.wos_id:
        external_links.append({"label": "Web of Science", "url": profile_user.wos_id})

    context = {
        "profile_user": profile_user,
        **dashboard_metrics,
        "generated_documents_count": generated_documents_qs.count(),
        "generated_reports_count": generated_documents_qs.filter(generated_by__isnull=False).count(),
        "template_count": profile_user.synthetic_documents.count(),
        "recent_publications": recent_publications,
        "recent_generated_documents": list(generated_documents_qs[:8]),
        "top_venues": list(top_venues),
        "external_links": external_links,
        "can_edit_profile": can_edit_profile,
        "can_manage_sync": can_manage_sync,
        "profile_form": profile_form,
        "profile_sync_form": profile_sync_form,
        "profile_updated": profile_updated,
        "profile_sync_url": reverse("employee_profile_sync", kwargs={"user_id": profile_user.id}),
        "profile_sync_fields_url": reverse("employee_profile_sync_fields", kwargs={"user_id": profile_user.id}),
        "profile_sync_start_url": reverse("employee_profile_sync_start", kwargs={"user_id": profile_user.id}),
        "profile_sync_reset_url": reverse("employee_profile_sync_reset", kwargs={"user_id": profile_user.id}),
        "profile_sync_status_url_template": reverse(
            "employee_profile_sync_status",
            kwargs={"user_id": profile_user.id, "task_id": "__TASK_ID__"},
        ),
        "profile_publications_fragment_url": reverse(
            "employee_profile_publications_fragment",
            kwargs={"user_id": profile_user.id},
        ),
        "profile_sync_availability": _sync_profile_availability(profile_user),
        "sync_reset_enabled": bool(getattr(settings, "DEBUG", False)),
        "sync_modal_open": sync_modal_open,
        "sync_sources": [
            {"key": "all", "label": "Барлығы"},
            {"key": "orcid", "label": "ORCID"},
            {"key": "scopus", "label": "Scopus"},
            {"key": "scholar", "label": "Scholar"},
            {"key": "wos", "label": "WoS"},
        ],
    }
    return render(request, "account/user_profile.html", context)


@login_required(login_url="account_login")
def employee_profile_sync(request, user_id: int):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    profile_user = get_object_or_404(User, pk=user_id)
    if not _can_manage_profile_sync(request.user, profile_user):
        return JsonResponse({"detail": "Forbidden"}, status=403)

    payload = {}
    content_type = (request.content_type or "").split(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}

    source = str(payload.get("source") or request.POST.get("source") or "all").strip().lower()
    allowed_sources = {"all", "orcid", "scopus", "scholar", "wos"}
    if source not in allowed_sources:
        return JsonResponse({"detail": "Invalid source"}, status=400)

    force_raw = payload.get("force", request.POST.get("force", "0"))
    force = str(force_raw).strip().lower() in {"1", "true", "yes", "on"}
    task = sync_user_publications_task.delay(
        user_id=profile_user.id,
        source=source,
        initiated_by_id=request.user.id,
        force=force,
    )
    return JsonResponse(
        {
            "status": "queued",
            "task_id": task.id,
            "source": source,
            "target_user_id": profile_user.id,
        },
        status=202,
    )


@login_required(login_url="account_login")
def employee_profile_sync_fields(request, user_id: int):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    profile_user = get_object_or_404(User, pk=user_id)
    if not _can_manage_profile_sync(request.user, profile_user):
        return JsonResponse({"detail": "Forbidden"}, status=403)

    payload = _decode_request_payload(request)
    data = payload or request.POST
    form = ProfileSyncFieldsForm(data, instance=profile_user)
    if not form.is_valid():
        return JsonResponse(
            {"detail": "Validation failed", "errors": form.errors.get_json_data()},
            status=400,
        )

    form.save()
    profile_user.refresh_from_db()
    return JsonResponse(
        {
            "ok": True,
            "fields": _sync_profile_field_values(profile_user),
            "availability": _sync_profile_availability(profile_user),
        }
    )


@login_required(login_url="account_login")
def employee_profile_sync_start(request, user_id: int):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    profile_user = get_object_or_404(User, pk=user_id)
    if not _can_manage_profile_sync(request.user, profile_user):
        return JsonResponse({"detail": "Forbidden"}, status=403)

    payload = _decode_request_payload(request)
    mode = str(payload.get("mode") or request.POST.get("mode") or "full").strip().lower()
    force_raw = payload.get("force", request.POST.get("force", "0"))
    force = str(force_raw).strip().lower() in {"1", "true", "yes", "on"}

    availability = _sync_profile_availability(profile_user)
    if not availability["has_any"]:
        return JsonResponse(
            {
                "detail": "At least one sync field is required.",
                "availability": availability,
            },
            status=400,
        )

    satbayev_only = mode == "satbayev_only"
    if satbayev_only and not availability["has_satbayev"]:
        return JsonResponse(
            {
                "detail": "Satbayev profile URL is required for satbayev_only mode.",
                "availability": availability,
            },
            status=400,
        )

    task = sync_user_profile_full_cycle_task.delay(
        user_id=profile_user.id,
        initiated_by_id=request.user.id,
        force=force,
        satbayev_only=satbayev_only,
    )
    return JsonResponse(
        {
            "status": "queued",
            "task_id": task.id,
            "satbayev_only": satbayev_only,
            "availability": availability,
        },
        status=202,
    )


@login_required(login_url="account_login")
def employee_profile_sync_reset(request, user_id: int):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    if not bool(getattr(settings, "DEBUG", False)):
        return JsonResponse(
            {"detail": "Profile reset is available only when DEBUG=True."},
            status=403,
        )

    profile_user = get_object_or_404(User, pk=user_id)
    if not _can_manage_profile_sync(request.user, profile_user):
        return JsonResponse({"detail": "Forbidden"}, status=403)

    payload = _decode_request_payload(request)
    confirm_raw = payload.get("confirm", request.POST.get("confirm", "0"))
    confirm = str(confirm_raw).strip().lower() in {"1", "true", "yes", "on"}
    if not confirm:
        return JsonResponse({"detail": "Reset confirmation is required."}, status=400)

    with transaction.atomic():
        stats = _reset_employee_profile_data(profile_user)
        profile_user.refresh_from_db()

    return JsonResponse(
        {
            "ok": True,
            "detail": "Profile data has been reset.",
            "stats": stats,
            "fields": _sync_profile_field_values(profile_user),
            "availability": _sync_profile_availability(profile_user),
        }
    )


@login_required(login_url="account_login")
def employee_profile_sync_status(request, user_id: int, task_id: str):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    profile_user = get_object_or_404(User, pk=user_id)
    if not _can_manage_profile_sync(request.user, profile_user):
        return JsonResponse({"detail": "Forbidden"}, status=403)

    async_result = AsyncResult(task_id)
    state = str(async_result.state or "PENDING").upper()
    is_ready = bool(async_result.ready())
    payload = {
        "task_id": task_id,
        "state": state,
        "ready": is_ready,
    }

    if not is_ready:
        return JsonResponse(payload)

    if async_result.successful():
        payload["result"] = async_result.result
        payload["ok"] = True
    else:
        payload["ok"] = False
        payload["error"] = str(async_result.result)
    return JsonResponse(payload)


@login_required(login_url="account_login")
def employee_profile_publications_fragment(request, user_id: int):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    profile_user = get_object_or_404(User, pk=user_id)
    if not _can_manage_profile_sync(request.user, profile_user):
        return JsonResponse({"detail": "Forbidden"}, status=403)

    publications_qs = _employee_publications_queryset(profile_user)
    dashboard_metrics = _collect_profile_dashboard_metrics(profile_user, publications_qs)
    recent_publications = list(publications_qs[:5])
    html = render_to_string(
        "account/partials/recent_publications_list.html",
        {"recent_publications": recent_publications},
        request=request,
    )
    return JsonResponse(
        {
            "ok": True,
            "html": html,
            "publication_count": dashboard_metrics["publication_count"],
            "metric_texts": dashboard_metrics["metric_texts"],
        }
    )


def employee_profile_export(request, user_id: int):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    profile_user = get_object_or_404(User, pk=user_id)
    selected_template = (request.POST.get("template_key") or request.POST.get("template_id") or "").strip()
    if not selected_template:
        return redirect("employee_profile", user_id=user_id)

    generator_template = resolve_generator_template_for_request(request.user, selected_template)
    if not generator_template:
        return redirect("employee_profile", user_id=user_id)

    template_page = (generator_template.page or "").strip()
    if template_page not in {"", "employee"}:
        return redirect("employee_profile", user_id=user_id)

    filter_payload = request.POST.copy()
    filter_payload["staff_user"] = str(profile_user.id)
    selected_tags = extract_selected_tags(filter_payload)
    publications_queryset, _ = build_filtered_publications(filter_payload, selected_tags)

    requested_title = (request.POST.get("report_title") or "").strip()
    if not requested_title:
        full_name = profile_user.get_full_name().strip() or profile_user.username
        requested_title = f"{generator_template.title} - {full_name}"

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


def account_page(request):
    if request.user.is_authenticated:
        return redirect("employee_profile", user_id=request.user.id)
    return render(request, "account/account_page.html")


def account_login(request):
    if request.user.is_authenticated:
        return redirect("employee_profile", user_id=request.user.id)

    form = LoginForm(request=request, data=request.POST or None)
    default_redirect = reverse("account_page")
    next_url = _get_safe_next_url(request, default_url=default_redirect)

    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return redirect(next_url)

    return render(
        request,
        "account/login.html",
        {
            "login_form": form,
            "next_url": next_url,
            "activated": request.GET.get("activated") == "1",
        },
    )


def account_register(request):
    if request.user.is_authenticated:
        return redirect("employee_profile", user_id=request.user.id)

    form = RegisterForm(request.POST or None)
    default_redirect = reverse("account_page")
    next_url = _get_safe_next_url(request, default_url=default_redirect)

    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect(next_url)

    return render(
        request,
        "account/register.html",
        {
            "register_form": form,
            "next_url": next_url,
        },
    )


def register_email_status(request):
    email = request.GET.get("email", "").strip().lower()
    if not email:
        return JsonResponse({"detail": "Email is required"}, status=400)

    user = User.objects.filter(email__iexact=email).first()
    if not user:
        return JsonResponse(
            {
                "status": "new",
                "button_label": "Тіркелу",
                "detail": "Email бойынша аккаунт табылмады. Тіркелуді жалғастырыңыз.",
            }
        )

    if not user.is_user:
        full_name = f"{user.first_name} {user.last_name}".strip() or user.username
        return JsonResponse(
            {
                "status": "needs_activation",
                "button_label": "Аккаунты активациялау",
                "detail": f"{full_name} үшін аккаунт табылды. Белсендіру хатын жіберуге болады.",
            }
        )

    return JsonResponse(
        {
            "status": "already_registered",
            "button_label": "Тіркелу",
            "login_url": reverse("account_login"),
            "detail": "Бұл email бойынша аккаунт бар. Кіру бөлімін қолданыңыз.",
        }
    )


def register_send_activation_email(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    payload = {}
    content_type = (request.content_type or "").split(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}

    email = str(payload.get("email") or request.POST.get("email") or "").strip().lower()
    if not email:
        return JsonResponse({"detail": "Email is required"}, status=400)

    user = User.objects.filter(email__iexact=email).first()
    if not user:
        return JsonResponse({"detail": "Бұл email жүйеде табылмады."}, status=404)

    if user.is_user:
        return JsonResponse({"detail": "Бұл email бойынша аккаунт әлдеқашан тіркелген."}, status=400)

    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    activation_path = reverse("account_activate", kwargs={"uidb64": uidb64, "token": token})

    raw_next_url = str(payload.get("next") or request.POST.get("next") or "").strip()
    safe_next_url = _normalize_safe_next_url(request, raw_next_url, default_url="")
    if safe_next_url:
        activation_path = f"{activation_path}?{urlencode({'next': safe_next_url})}"

    referral_path = f"{reverse('account_register')}?{urlencode({'ref': user.pk})}"
    activation_url = _build_public_url(request, activation_path)
    referral_url = _build_public_url(request, referral_path)

    display_name = f"{user.first_name} {user.last_name}".strip() or user.username or "әріптес"
    subject = "SU Scholar: аккаунтты белсендіру"
    message = (
        f"Сәлеметсіз бе, {display_name}!\n\n"
        "SU Scholar платформасына қосылғаныңызға рақмет.\n"
        "Аккаунтты белсендіру үшін төмендегі сілтеме бойынша өтіңіз:\n"
        f"{activation_url}\n\n"
        "Сізге жеке реферальды сілтеме:\n"
        f"{referral_url}\n\n"
        "Сілтеме ашылғаннан кейін сіз құпиясөз орнату бетіне өтесіз.\n" 
        f"Сіздің username: {user.username}\n"
        "Егер бұл сұранысты сіз жібермесеңіз, бұл хатты елемеңіз."
    )

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception:
        return JsonResponse({"detail": "Хатты жіберу мүмкін болмады. SMTP баптауларын тексеріңіз."}, status=500)

    return JsonResponse({"detail": "Белсендіру хаты жіберілді. Email-ді тексеріңіз."})


def account_activate(request, uidb64: str, token: str):
    user = _user_from_uid(uidb64)
    if not user or not default_token_generator.check_token(user, token):
        return render(
            request,
            "account/set_password.html",
            {
                "form": None,
                "invalid_link": True,
            },
        )

    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])

    set_password_url = reverse("account_set_password", kwargs={"uidb64": uidb64, "token": token})
    safe_next_url = _normalize_safe_next_url(request, request.GET.get("next", ""), default_url="")
    if safe_next_url:
        set_password_url = f"{set_password_url}?{urlencode({'next': safe_next_url})}"
    return redirect(set_password_url)


def account_set_password(request, uidb64: str, token: str):
    user = _user_from_uid(uidb64)
    if not user or not default_token_generator.check_token(user, token):
        return render(
            request,
            "account/set_password.html",
            {
                "form": None,
                "invalid_link": True,
            },
        )

    form = ActivationSetPasswordForm(user, request.POST or None)
    next_url = _get_safe_next_url(request, default_url=reverse("account_page"))

    if request.method == "POST" and form.is_valid():
        form.save()
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])

        login_url = reverse("account_login")
        login_params = {"next": next_url, "activated": "1"} if next_url else {"activated": "1"}
        return redirect(f"{login_url}?{urlencode(login_params)}")

    return render(
        request,
        "account/set_password.html",
        {
            "form": form,
            "invalid_link": False,
            "next_url": next_url,
        },
    )


def account_logout(request):
    logout(request)
    return redirect("account_login")


def search_employee_from_email(request):
    email = request.GET.get("email", "").strip().lower()
    if not email:
        return JsonResponse({"detail": "Email is required"}, status=400)

    user = User.objects.filter(email__iexact=email).first()
    if not user:
        return JsonResponse({"detail": "User not found"}, status=404)

    return JsonResponse({
        "id": user.id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "profile_url": reverse("employee_profile", kwargs={"user_id": user.id}),
    })


def activate_user(request, user_id: int):
    if not request.user.is_authenticated or not request.user.is_staff:
        return JsonResponse({"detail": "Forbidden"}, status=403)

    user = get_object_or_404(User, pk=user_id)
    user.is_active = True
    user.save()
    return JsonResponse({"detail": "User activated"})