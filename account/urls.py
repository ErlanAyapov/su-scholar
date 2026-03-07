from django.urls import path

from .views import account_logout, account_page, employees_list, employees_list_chunk


urlpatterns = [
    path('account/', account_page, name='account_page'),
    path('account/logout/', account_logout, name='account_logout'),
    path('employees/', employees_list, name='employees'),
    path('employees/load/', employees_list_chunk, name='employees_load'),
]
