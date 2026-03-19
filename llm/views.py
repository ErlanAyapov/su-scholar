from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from llm.models import ChatSession, ChatShareImport, ChatShareLink, Message


@require_GET
def llm_page(request):
    return render(request, "llm/main.html")


def _resolve_owned_session(user, session_id: int | None) -> ChatSession | None:
    if not user.is_authenticated:
        return None
    if not session_id:
        return None
    return ChatSession.objects.filter(id=session_id, user=user).first()


def _parse_session_id(raw_value) -> int | None:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


@login_required
@require_POST
def llm_share_create(request):
    try:
        payload = json.loads((request.body or b"{}").decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}

    session_id = _parse_session_id(payload.get("session_id"))
    chat_session = _resolve_owned_session(request.user, session_id)
    if not chat_session:
        return JsonResponse({"detail": "Chat session not found"}, status=404)

    now = timezone.now()
    share_link = (
        ChatShareLink.objects.filter(
            session=chat_session,
            created_by=request.user,
            is_active=True,
            expires_at__gt=now,
        )
        .order_by("-created_at")
        .first()
    )
    if not share_link:
        share_link = ChatShareLink.objects.create(
            session=chat_session,
            created_by=request.user,
        )

    share_url = request.build_absolute_uri(
        reverse("llm_shared_chat", kwargs={"token": share_link.token}),
    )
    return JsonResponse(
        {
            "share_url": share_url,
            "expires_at": share_link.expires_at.isoformat() if share_link.expires_at else None,
        }
    )


def _clone_shared_session_for_user(chat_session: ChatSession, user) -> ChatSession:
    copied_title = f"Shared: {chat_session.title or ChatSession.DEFAULT_TITLE}"[:500]
    cloned_session = ChatSession.objects.create(
        user=user,
        title=copied_title,
    )
    source_messages = list(chat_session.messages.order_by("created", "id"))
    if source_messages:
        Message.objects.bulk_create(
            [
                Message(
                    chat=cloned_session,
                    body=item.body,
                    sended_from=item.sended_from,
                )
                for item in source_messages
            ]
        )
    return cloned_session


@require_GET
def llm_shared_chat(request, token: str):
    token_value = str(token or "").strip()
    if not token_value:
        raise Http404("Shared chat not found")

    share_link = (
        ChatShareLink.objects.select_related("session", "session__user")
        .prefetch_related("session__messages")
        .filter(token=token_value, is_active=True)
        .first()
    )
    if not share_link or share_link.is_expired:
        raise Http404("Shared chat not found")

    share_link.last_opened_at = timezone.now()
    share_link.save(update_fields=["last_opened_at"])

    if request.user.is_authenticated:
        source_session = share_link.session
        if request.user.id == source_session.user_id:
            return redirect(f"{reverse('llm_page')}?session={source_session.id}")

        with transaction.atomic():
            imported = (
                ChatShareImport.objects.select_related("session")
                .filter(share_link=share_link, user=request.user)
                .first()
            )
            if imported:
                target_session = imported.session
            else:
                target_session = _clone_shared_session_for_user(source_session, request.user)
                ChatShareImport.objects.create(
                    share_link=share_link,
                    user=request.user,
                    session=target_session,
                )

        return redirect(f"{reverse('llm_page')}?session={target_session.id}")

    read_only_messages = list(share_link.session.messages.order_by("created", "id"))
    return render(
        request,
        "llm/shared_chat.html",
        {
            "source_session": share_link.session,
            "messages": read_only_messages,
            "share_link": share_link,
        },
    )
