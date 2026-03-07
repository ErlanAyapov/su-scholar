from django.contrib.auth import get_user_model, login, logout
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string

from .forms import LoginForm, RegisterForm


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


def account_page(request):
    if request.user.is_authenticated:
        return render(request, "account/account_page.html")

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
