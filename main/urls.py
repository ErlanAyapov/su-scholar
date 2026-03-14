from django.urls import path

from .views import HomePageView, MainPageView, PublicationDetailView, SearchPageView, SearchSuggestionsView


urlpatterns = [
    path('', MainPageView.as_view(), name='main'),
    path('advanced_search/', HomePageView.as_view(), name='advanced_search'),
    path('search/', SearchPageView.as_view(), name='main_search'),
    path('search/suggestions/', SearchSuggestionsView.as_view(), name='search_suggestions'),
    path('publications/<int:pk>/', PublicationDetailView.as_view(), name='publication_detail'),
]
