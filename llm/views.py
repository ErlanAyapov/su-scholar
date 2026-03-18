from django.shortcuts import render

# Create your views here.
def llm_page(request):
    return render(request, "llm/main.html")