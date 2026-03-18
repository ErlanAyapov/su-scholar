from django.urls import path

from .views import llm_page

urlpatterns = [
    path('', llm_page, name='llm_page'),
]