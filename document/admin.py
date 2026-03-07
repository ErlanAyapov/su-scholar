from django.contrib import admin
from .models import Document, DocumentGenerator, DocumentPermission, Synonym

admin.site.register(Document)
admin.site.register(DocumentGenerator)
admin.site.register(DocumentPermission)
admin.site.register(Synonym)
# Register your models here.
