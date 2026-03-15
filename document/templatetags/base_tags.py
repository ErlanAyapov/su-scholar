from django import template
from django.db.models import Q

from document.models import DocumentGenerator

register = template.Library()


def _extract_user(user_or_request):
    if user_or_request is None:
        return None
    return getattr(user_or_request, "user", user_or_request)


@register.simple_tag
def get_document_generators(user_or_request, page=""):
    user = _extract_user(user_or_request)
    page_value = (page or "").strip()

    queryset = DocumentGenerator.objects.select_related("user")
    if user and user.is_authenticated:
        queryset = queryset.filter(Q(access_to_all=True) | Q(user=user))
    else:
        queryset = queryset.filter(access_to_all=True)

    if page_value:
        queryset = queryset.filter(Q(page=page_value) | Q(page="") | Q(page__isnull=True))

    return queryset.distinct().order_by("-created_at", "-id")
