import json

from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from document.models import Document
from main.models import Publication
from main.tasks import sync_user_publications_task

from .forms import LoginForm, ProfileEditForm, RegisterForm


User = get_user_model()
PAGE_SIZE = 20


def _get_safe_next_url(request, default_url: str) -> str:
    next_url = (request.POST.get("next") or request.GET.get("next") or "").strip()
    if not next_url:
        return default_url
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return next_url
    return default_url


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


def _can_manage_profile_sync(request_user, profile_user) -> bool:
    return bool(
        request_user
        and request_user.is_authenticated
        and (request_user.id == profile_user.id or request_user.is_staff)
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
        User.objects.select_related("department").prefetch_related("universities", "roles"),
        pk=user_id,
    )
    can_edit_profile = bool(request.user.is_authenticated and request.user.id == profile_user.id)
    can_manage_sync = _can_manage_profile_sync(request.user, profile_user)
    profile_form = None
    profile_updated = request.GET.get("updated") == "1"

    if can_edit_profile:
        if request.method == "POST":
            profile_form = ProfileEditForm(request.POST, request.FILES, instance=profile_user)
            if profile_form.is_valid():
                profile_form.save()
                profile_url = reverse("employee_profile", kwargs={"user_id": profile_user.id})
                return redirect(f"{profile_url}?updated=1")
        else:
            profile_form = ProfileEditForm(instance=profile_user)

    publications_qs = _employee_publications_queryset(profile_user)
    publication_count = publications_qs.count()
    publication_stats = publications_qs.aggregate(
        citations_total=Sum("citations_count"),
        open_access_count=Count("id", filter=Q(open_access=True), distinct=True),
    )
    top_venues = (
        publications_qs.order_by()
        .exclude(venue__name="")
        .values("venue__name")
        .annotate(total=Count("id", distinct=True))
        .order_by("-total", "venue__name")[:5]
    )

    recent_publications = list(publications_qs[:12])
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
        "publication_count": publication_count,
        "citations_total": publication_stats["citations_total"] or 0,
        "open_access_count": publication_stats["open_access_count"] or 0,
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
        "profile_updated": profile_updated,
        "profile_sync_url": reverse("employee_profile_sync", kwargs={"user_id": profile_user.id}),
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


def account_logout(request):
    logout(request)
    return redirect("account_login")
