from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm, UserCreationForm


User = get_user_model()


class RegisterForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True
        self.fields["last_name"].required = True
        self.fields["email"].required = True
        self.fields["email"].widget = forms.EmailInput()

        self.fields["username"].label = "Пайдаланушы аты"
        self.fields["first_name"].label = "Аты"
        self.fields["last_name"].label = "Тегі"
        # self.fields["email"].label = "Email"
        self.fields["password1"].label = "Құпиясөз"
        self.fields["password2"].label = "Құпиясөзді растау"

        for field_name, field in self.fields.items():
            field.widget.attrs.update({"class": "form-control"})
            field.widget.attrs.setdefault("placeholder", field.label)
            if field_name in {"password1", "password2"}:
                field.help_text = None

    def clean_email(self):
        email = str(self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            raise forms.ValidationError("Email міндетті.")

        existing_user = User.objects.filter(email__iexact=email).first()
        if not existing_user:
            return email

        if existing_user.is_user:
            raise forms.ValidationError("Бұл email бойынша аккаунт бар. Кіру бөлімін қолданыңыз.")

        raise forms.ValidationError("Бұл email қызметкер профиліне тиесілі. Аккаунтты белсендіруді таңдаңыз.")


class LoginForm(AuthenticationForm):
    username = forms.CharField(label="Пайдаланушы аты")
    password = forms.CharField(label="Құпиясөз", widget=forms.PasswordInput)

    error_messages = {
        "invalid_login": "Пайдаланушы аты немесе құпиясөз қате.",
        "inactive": "Бұл аккаунт белсенді емес.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"class": "form-control", "placeholder": "Пайдаланушы аты"})
        self.fields["password"].widget.attrs.update({"class": "form-control", "placeholder": "Құпиясөз"})


class ActivationSetPasswordForm(SetPasswordForm):
    error_messages = {
        "password_mismatch": "Құпиясөздер сәйкес келмейді.",
    }

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["new_password1"].label = "Жаңа құпиясөз"
        self.fields["new_password2"].label = "Жаңа құпиясөзді растау"

        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-control"})
            field.widget.attrs.setdefault("placeholder", field.label)


class ProfileEditForm(forms.ModelForm):
    class Meta:
        model = User
        fields = (
            "first_name",
            "last_name",
            "father_name",
            "email",
            "phone_number",
            "inn",
            "gender",
            "photo",
            "orc_id",
            "scopus_id",
            "wos_id",
            "google_scholar",
            "researchgate",
            "satbayev_profile_url",
        )
        labels = {
            "first_name": "Аты",
            "last_name": "Тегі",
            "father_name": "Әкесінің аты",
            "email": "Email",
            "phone_number": "Телефон",
            "inn": "ЖСН/БСН",
            "gender": "Жынысы",
            "photo": "Профиль суреті",
            "orc_id": "ORCID",
            "scopus_id": "Scopus ID",
            "wos_id": "Web of Science ID",
            "google_scholar": "Google Scholar",
            "researchgate": "ResearchGate",
            "satbayev_profile_url": "Satbayev profile URL",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "gender":
                field.widget.attrs.update({"class": "form-select"})
            else:
                field.widget.attrs.update({"class": "form-control"})
            field.widget.attrs.setdefault("placeholder", field.label)
