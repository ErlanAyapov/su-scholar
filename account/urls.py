from django.urls import path

from .views import employees_list, employees_list_chunk


urlpatterns = [
    path('employees/', employees_list, name='employees'),
    path('employees/load/', employees_list_chunk, name='employees_load'),
]
