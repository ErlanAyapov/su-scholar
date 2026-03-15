from django.urls import path

from .views import (
    AnalyticsPageView,
    HomePageView,
    MainPageView,
    ProjectsGrantsDemoView,
    PublicationDetailView,
    SearchPageView,
    SearchSuggestionsView,
)


urlpatterns = [
    path('', MainPageView.as_view(), name='main'),
    path('advanced_search/', HomePageView.as_view(), name='advanced_search'),
    path('analytics/', AnalyticsPageView.as_view(), name='analytics_page'),
    path('projects-grants/', ProjectsGrantsDemoView.as_view(), name='projects_grants_demo'),
    path('search/', SearchPageView.as_view(), name='main_search'),
    path('search/suggestions/', SearchSuggestionsView.as_view(), name='search_suggestions'),
    path('publications/<int:pk>/', PublicationDetailView.as_view(), name='publication_detail'),
]
