from django.urls import path

from .views import (
    document_detail,
    document_edit_page,
    document_file,
    documents_collection,
    onlyoffice_callback,
    onlyoffice_config,
)


urlpatterns = [
    path("documents", documents_collection, name="document_main"),
    path("documents/", documents_collection),
    path("documents/<int:pk>", document_detail, name="document_detail"),
    path("documents/<int:pk>/", document_detail),
    path("documents/<int:pk>/edit", document_edit_page, name="document_edit"),
    path("documents/<int:pk>/edit/", document_edit_page),
    path("onlyoffice/config/<int:pk>", onlyoffice_config, name="onlyoffice_config"),
    path("onlyoffice/config/<int:pk>/", onlyoffice_config),
    path("files/<int:pk>", document_file, name="document_file"),
    path("files/<int:pk>/", document_file),
    path("onlyoffice/callback/<int:pk>", onlyoffice_callback, name="onlyoffice_callback"),
    path("onlyoffice/callback/<int:pk>/", onlyoffice_callback),
]
