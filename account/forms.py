import io
from pathlib import Path

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm, UserCreationForm
from django.core.files.uploadedfile import InMemoryUploadedFile
from PIL import Image, ImageOps, UnidentifiedImageError


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


class InactiveUserCreateForm(forms.ModelForm):
    class Meta:
        model = User
        fields = (
            "username",
            "first_name",
            "last_name",
            "father_name",
            "email",
            "gender",
            "department",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].required = True
        self.fields["first_name"].required = True
        self.fields["last_name"].required = True
        self.fields["email"].required = True
        self.fields["email"].widget = forms.EmailInput()

        self.fields["username"].label = "Username"
        self.fields["first_name"].label = "First name"
        self.fields["last_name"].label = "Last name"
        self.fields["father_name"].label = "Middle name"
        self.fields["email"].label = "Email"
        self.fields["gender"].label = "Gender"
        self.fields["department"].label = "Department"

        for name, field in self.fields.items():
            if name == "gender":
                field.widget.attrs.update({"class": "form-select"})
            else:
                field.widget.attrs.update({"class": "form-control"})
            field.widget.attrs.setdefault("placeholder", field.label)

    def clean_username(self):
        username = str(self.cleaned_data.get("username") or "").strip()
        if not username:
            raise forms.ValidationError("Username is required.")

        existing = User.objects.filter(username__iexact=username).first()
        if existing:
            raise forms.ValidationError("A user with this username already exists.")
        return username

    def clean_email(self):
        email = str(self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            raise forms.ValidationError("Email is required.")
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("A user with this email already exists.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_user = False
        user.is_active = False
        user.is_staff = False
        user.is_superuser = False
        user.set_unusable_password()
        if commit:
            user.save()
            self.save_m2m()
        return user


class LoginForm(AuthenticationForm):
    username = forms.CharField(label="Пайдаланушы аты")
    password = forms.CharField(label="Құпиясөз", widget=forms.PasswordInput)

    error_messages = {
        "invalid_login": "Пайдаланушы аты, email немесе құпиясөз қате.",
        "inactive": "Бұл аккаунт белсенді емес.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"class": "form-control", "placeholder": "Пайдаланушы аты немесе Email"})
        self.fields["password"].widget.attrs.update({"class": "form-control", "placeholder": "Құпиясөз"})

    def clean(self):
        login_value = str(self.cleaned_data.get("username") or "").strip()
        if "@" in login_value:
            user_by_email = User.objects.filter(email__iexact=login_value).order_by("id").first()
            if user_by_email:
                self.cleaned_data["username"] = user_by_email.get_username()
        return super().clean()


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
    MAX_PHOTO_UPLOAD_BYTES = 15 * 1024 * 1024
    PHOTO_MAX_SIDE = 1280
    PHOTO_QUALITY = 82

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
            if name == "photo":
                field.widget.attrs.update({"accept": "image/*"})

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo in (None, False):
            return photo

        if photo.size > self.MAX_PHOTO_UPLOAD_BYTES:
            raise forms.ValidationError("Фото тым үлкен. Файл өлшемі 15 MB аспауы керек.")

        try:
            return self._compress_photo(photo)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise forms.ValidationError("Суретті өңдеу мүмкін болмады. Басқа файл жүктеп көріңіз.") from exc

    @classmethod
    def _compress_photo(cls, photo):
        photo.seek(0)
        with Image.open(photo) as image:
            image = ImageOps.exif_transpose(image)

            if image.mode in ("RGBA", "LA", "P"):
                rgba_image = image.convert("RGBA")
                canvas = Image.new("RGB", rgba_image.size, (255, 255, 255))
                canvas.paste(rgba_image, mask=rgba_image.split()[-1])
                image = canvas
            else:
                image = image.convert("RGB")

            if max(image.size) > cls.PHOTO_MAX_SIDE:
                image.thumbnail((cls.PHOTO_MAX_SIDE, cls.PHOTO_MAX_SIDE), Image.Resampling.LANCZOS)

            output = io.BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=cls.PHOTO_QUALITY,
                optimize=True,
                progressive=True,
            )
            output.seek(0)

        stem = Path(photo.name).stem.strip() or "profile-photo"
        file_name = f"{stem}.jpg"
        return InMemoryUploadedFile(
            file=output,
            field_name="photo",
            name=file_name,
            content_type="image/jpeg",
            size=output.getbuffer().nbytes,
            charset=None,
        )


class ProfileSyncFieldsForm(forms.ModelForm):
    class Meta:
        model = User
        fields = (
            "scopus_id",
            "wos_id",
            "google_scholar",
            "researchgate",
            "satbayev_profile_url",
        )
        labels = {
            "scopus_id": "Scopus ID",
            "wos_id": "Web of Science ID",
            "google_scholar": "Google Scholar",
            "researchgate": "ResearchGate",
            "satbayev_profile_url": "Satbayev profile URL",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = False
            field.widget.attrs.update({"class": "form-control"})
            field.widget.attrs.setdefault("placeholder", field.label)

    def clean(self):
        cleaned_data = super().clean()
        for field_name in self.fields:
            value = cleaned_data.get(field_name)
            if isinstance(value, str):
                cleaned_data[field_name] = value.strip()
        return cleaned_data
