from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string


User = get_user_model()
PAGE_SIZE = 20


def _employees_queryset():
    return User.objects.filter(is_user=False).order_by(
        'last_name',
        'first_name',
        'father_name',
        'id',
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
