from django.db import models
from django.contrib.auth.models import AbstractUser


class University(models.Model):
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300, blank=True)
    country = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.name


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
    university = models.ForeignKey(
        University,
        on_delete=models.CASCADE,
        related_name="departments"
    )

    def __str__(self):
        return f"{self.name} ({self.university})"


class User(AbstractUser):
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

    banned = models.BooleanField(default=False)
    quiet_mode = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    orc_id = models.CharField(max_length=20, blank=True)
    scopus_id = models.CharField(max_length=20, blank=True)
    wos_id = models.CharField(max_length=20, blank=True)
    researchgate = models.URLField(blank=True)
    google_scholar = models.URLField(blank=True)

    def __str__(self):
        return f"{self.username}"