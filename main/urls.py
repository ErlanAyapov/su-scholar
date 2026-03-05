from django.urls import path

from .views import main_page, publication_detail_page


urlpatterns = [
    path('', main_page, name='main'),
    path('publications/<int:pk>/', publication_detail_page, name='publication_detail'),
]
