from django.contrib.auth import get_user_model, login, logout
from django.db.models import Count, Q, Sum
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.template.loader import render_to_string

from document.models import Document
from main.models import Publication

from .forms import LoginForm, ProfileEditForm, RegisterForm


User = get_user_model()
PAGE_SIZE = 20


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
        "profile_form": profile_form,
        "profile_updated": profile_updated,
    }
    return render(request, "account/user_profile.html", context)


def account_page(request):
    if request.user.is_authenticated:
        return redirect("employee_profile", user_id=request.user.id)

    register_form = RegisterForm(prefix="register")
    login_form = LoginForm(request=request, prefix="login")

    if request.method == "POST":
        if "register_submit" in request.POST:
            register_form = RegisterForm(request.POST, prefix="register")
            if register_form.is_valid():
                user = register_form.save()
                login(request, user)
                return redirect("account_page")
        elif "login_submit" in request.POST:
            login_form = LoginForm(request=request, data=request.POST, prefix="login")
            if login_form.is_valid():
                login(request, login_form.get_user())
                return redirect("account_page")

    return render(
        request,
        "account/account_page.html",
        {
            "register_form": register_form,
            "login_form": login_form,
        },
    )


def account_logout(request):
    if request.method == "POST" and request.user.is_authenticated:
        logout(request)
    return redirect("account_page")
