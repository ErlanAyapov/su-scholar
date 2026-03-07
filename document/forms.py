from django import forms

from document.models import DocumentGenerator


FILE_TYPE_CHOICES = [
    ("docx", "DOCX"),
    ("txt", "TXT"),
]


class DocumentGeneratorCreateForm(forms.ModelForm):
    file_type = forms.ChoiceField(choices=FILE_TYPE_CHOICES, label="File type")
    file = forms.FileField(required=False, label="Template file")
    content = forms.CharField(
        required=False,
        label="Template text",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 8,
                "placeholder": "Use synonyms and template tags here",
            }
        ),
    )

    class Meta:
        model = DocumentGenerator
        fields = ("title", "file_type", "file", "access_to_all", "content")
        labels = {
            "title": "Title",
            "access_to_all": "Accessible for all users",
        }
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Template title",
                }
            ),
            "access_to_all": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["file_type"].widget.attrs.update({"class": "form-select"})
        self.fields["file"].widget.attrs.update({"class": "form-control"})

    def clean(self):
        cleaned = super().clean()
        file_type = (cleaned.get("file_type") or "").strip().lower()
        uploaded_file = cleaned.get("file")
        content = (cleaned.get("content") or "").strip()
        has_existing_file = bool(self.instance and self.instance.pk and self.instance.file)

        if file_type == "docx":
            if uploaded_file and not uploaded_file.name.lower().endswith(".docx"):
                self.add_error("file", "DOCX template must have .docx extension.")
            if not uploaded_file and not has_existing_file:
                self.add_error("file", "Upload DOCX template file.")

        if file_type == "txt":
            if not uploaded_file and not has_existing_file and not content:
                self.add_error("content", "Provide template text or upload TXT file.")

        return cleaned

