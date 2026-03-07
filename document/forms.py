from django import forms

from document.models import DocumentGenerator


FILE_TYPE_CHOICES = [
    ("docx", "DOCX"),
    ("txt", "TXT"),
]


class DocumentGeneratorCreateForm(forms.ModelForm):
    file_type = forms.ChoiceField(choices=FILE_TYPE_CHOICES, label="Құжат түрі")
    content = forms.CharField(widget=forms.HiddenInput(), required=False)

    class Meta:
        model = DocumentGenerator
        fields = ("title", "file_type", "access_to_all", "content")
        labels = {
            "title": "Атауы",
            "access_to_all": "Барлық пайдаланушыларға қолжетімді",
        }
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control", "placeholder": "Құжат атауын енгізіңіз"}),
            "access_to_all": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["file_type"].widget.attrs.update({"class": "form-select"})
