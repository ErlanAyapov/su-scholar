from django.urls import path

from .views import main_page, publication_detail_page, search_suggestions


urlpatterns = [
    path('', main_page, name='main'),
    path('search/suggestions/', search_suggestions, name='search_suggestions'),
    path('publications/<int:pk>/', publication_detail_page, name='publication_detail'),
]
