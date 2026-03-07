from django.urls import path

from .views import document_main_page


urlpatterns = [
    path("documents/", document_main_page, name="document_main"),
]
