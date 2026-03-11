from django.urls import path

from .views import HomePageView, PublicationDetailView, SearchPageView, SearchSuggestionsView


urlpatterns = [
    path('', HomePageView.as_view(), name='main'),
    path('search/', SearchPageView.as_view(), name='main_search'),
    path('search/suggestions/', SearchSuggestionsView.as_view(), name='search_suggestions'),
    path('publications/<int:pk>/', PublicationDetailView.as_view(), name='publication_detail'),
]
