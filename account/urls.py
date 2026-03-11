from django.urls import path

from .views import (
    account_logout,
    account_page,
    employee_profile,
    employee_profile_sync,
    employees_list,
    employees_list_chunk,
)


urlpatterns = [
    path('account/', account_page, name='account_page'),
    path('account/logout/', account_logout, name='account_logout'),
    path('employees/<int:user_id>/', employee_profile, name='employee_profile'),
    path('employees/<int:user_id>/sync/', employee_profile_sync, name='employee_profile_sync'),
    path('employees/', employees_list, name='employees'),
    path('employees/load/', employees_list_chunk, name='employees_load'),
]
