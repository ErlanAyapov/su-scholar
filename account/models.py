from django.db import models
from django.contrib.auth.models import AbstractUser


class University(models.Model):
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300, blank=True)
    country = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.name


class Institute(models.Model):
    """
    Institute inside a university.
    """

    name = models.CharField(max_length=200)
    university = models.ForeignKey(
        University,
        on_delete=models.CASCADE,
        related_name="institutes",
    )

    class Meta:
        unique_together = ("name", "university")

    def __str__(self):
        return f"{self.name} ({self.university})"


class Role(models.Model):
    """
    Роль пользователя в системе.
    Чем выше level — тем меньше прав.
    """
    name = models.CharField(max_length=100)
    level = models.IntegerField(unique=True)

    def __str__(self):
        return f"{self.name} (Level {self.level})"


class Department(models.Model):
    """
    Кафедра
    """
    name = models.CharField(max_length=200)
    institute = models.ForeignKey(
        Institute,
        on_delete=models.CASCADE,
        related_name="departments"
    )

    @property
    def university(self):
        if not self.institute_id:
            return None
        return self.institute.university

    def __str__(self):
        return f"{self.name} ({self.institute})"


class User(AbstractUser):
    GENDER_CHOICES = [
        ('male', 'Male'),
        ('female', 'Female')
    ]

    father_name = models.CharField(max_length=100, blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    inn = models.CharField(max_length=20, blank=True)

    photo = models.ImageField(upload_to="user_photos/", blank=True, null=True)

    universities = models.ManyToManyField(
        University,
        blank=True,
        related_name="users"
    )

    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    roles = models.ManyToManyField(
        Role,
        blank=True
    )
    is_user = models.BooleanField(default=True)

    banned = models.BooleanField(default=False)
    quiet_mode = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    orc_id = models.CharField(max_length=20, blank=True)
    scopus_id = models.CharField(max_length=20, blank=True)
    wos_id = models.CharField(max_length=20, blank=True)
    researchgate = models.URLField(blank=True)
    google_scholar = models.URLField(blank=True)
    satbayev_profile_url = models.URLField(blank=True)
    journal_links = models.JSONField(default=list, blank=True)
    journal_ids = models.JSONField(default=list, blank=True)
    gender = models.CharField(max_length=20, blank=True, choices=GENDER_CHOICES)

    def __str__(self):
        return f"{self.username}"
