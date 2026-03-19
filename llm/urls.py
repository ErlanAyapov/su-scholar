from django.urls import path

from .views import llm_page, llm_share_create, llm_shared_chat

urlpatterns = [
    path('', llm_page, name='llm_page'),
    path('share/create/', llm_share_create, name='llm_share_create'),
    path('share/<str:token>/', llm_shared_chat, name='llm_shared_chat'),
]
