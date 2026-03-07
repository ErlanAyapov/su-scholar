from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm


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

        self.fields["username"].label = "Пайдаланушы аты"
        self.fields["first_name"].label = "Аты"
        self.fields["last_name"].label = "Тегі"
        self.fields["email"].label = "Email"
        self.fields["password1"].label = "Құпиясөз"
        self.fields["password2"].label = "Құпиясөзді растау"

        for field_name, field in self.fields.items():
            field.widget.attrs.update({"class": "form-control"})
            field.widget.attrs.setdefault("placeholder", field.label)
            if field_name in {"password1", "password2"}:
                field.help_text = None


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
