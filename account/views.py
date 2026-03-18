import json
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes, force_str
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode, urlsafe_base64_encode

from document.models import Document
from main.models import Publication
from main.tasks import sync_user_publications_task
from main.utils import (
    build_filtered_publications,
    extract_selected_tags,
    make_generated_document,
    make_generated_response_for_anonymous,
    resolve_generator_template_for_request,
)

from .forms import ActivationSetPasswordForm, LoginForm, ProfileEditForm, RegisterForm


User = get_user_model()
PAGE_SIZE = 20


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
        User.objects.select_related("department__institute__university").prefetch_related("universities", "roles"),
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
