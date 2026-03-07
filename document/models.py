from django.db import models


class Document(models.Model):
    choice = (
        ('pdf', 'PDF'),
        ('docx', 'DOCX'),
        ('txt', 'TXT'),
        ('excel', 'Excel'),
        ('pptx', 'PowerPoint'),
        ('other', 'Other'),

    )

    title = models.CharField(max_length=255)
    content = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    file = models.FileField(upload_to='documents/')
    file_type = models.CharField(max_length=50, choices=choice, default='docx')
    user = models.ForeignKey('account.User', on_delete=models.CASCADE, related_name='documents')
    version = models.PositiveIntegerField(default=1)
    is_deleted = models.BooleanField(default=False)
    generated_by = models.ForeignKey('DocumentGenerator', on_delete=models.SET_NULL, blank=True, null=True, related_name='generated_documents')

    class Meta:
        ordering = ('-updated_at', '-id')

    def __str__(self):
        return self.title


class DocumentPermission(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='permissions')
    user = models.ForeignKey('account.User', on_delete=models.CASCADE, related_name='document_permissions')
    can_view = models.BooleanField(default=True)
    can_edit = models.BooleanField(default=False)
    can_comment = models.BooleanField(default=False)
    can_review = models.BooleanField(default=False)
    can_download = models.BooleanField(default=True)
    can_print = models.BooleanField(default=True)

    class Meta:
        unique_together = ('document', 'user')


    def __str__(self):
        return f'{self.document_id} - {self.user_id}'


class DocumentGenerator(models.Model):
    title = models.CharField(max_length=255)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    file = models.FileField(upload_to='synthetic_documents/')
    file_type = models.CharField(max_length=50)
    user = models.ForeignKey('account.User', on_delete=models.CASCADE, related_name='synthetic_documents')
    access_to_all = models.BooleanField(default=False)

    def last_ten_documents(self):
        return self.generated_documents.order_by('-created_at')[:10]

    def __str__(self):
        return self.title

class Synonym(models.Model):
    code = models.CharField(max_length=255)
    value = models.CharField(max_length=255)
    example = models.TextField(blank=True, null=True)
    example_image = models.ImageField(upload_to='synonym_examples/', blank=True, null=True)

    def __str__(self):
        return f'{self.code} - {self.value}'
