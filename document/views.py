from django.shortcuts import render


def document_main_page(request):
    return render(request, "document/main.html")
