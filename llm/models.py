import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class ChatSession(models.Model):
    DEFAULT_TITLE = "New dialog"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="llm_chat_sessions",
        verbose_name="User",
    )
    created = models.DateTimeField(auto_now_add=True, verbose_name="Created at")
    updated = models.DateTimeField(auto_now=True, verbose_name="Updated at")
    title = models.CharField(max_length=500, default=DEFAULT_TITLE, verbose_name="Title")
    project = models.ForeignKey(
        "main.Project",
        on_delete=models.SET_NULL,
        related_name="llm_chat_sessions",
        null=True,
        blank=True,
        verbose_name="project_chat_sessions",
    )

    class Meta:
        ordering = ("-updated", "-id")

    def __str__(self):
        return f"Chat #{self.pk}: {self.title}"

    @classmethod
    def title_from_prompt(cls, prompt: str) -> str:
        normalized = " ".join(str(prompt or "").strip().split())
        if not normalized:
            return cls.DEFAULT_TITLE
        return normalized[:120]


class Message(models.Model):
    class Sender(models.TextChoices):
        USER = "user", "User"
        BOT = "bot", "Assistant"

    chat = models.ForeignKey(
        ChatSession,
        on_delete=models.CASCADE,
        related_name="messages",
        verbose_name="Chat",
    )
    body = models.TextField(verbose_name="Text")
    created = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Created at")
    sended_from = models.CharField(
        max_length=16,
        choices=Sender.choices,
        default=Sender.USER,
        db_index=True,
        verbose_name="Sender",
    )

    class Meta:
        ordering = ("created", "id")

    def __str__(self):
        short_body = self.body[:50].replace("\n", " ")
        return f"{self.chat_id}:{self.sended_from}:{short_body}"

    @property
    def role(self) -> str:
        if self.sended_from == self.Sender.BOT:
            return "assistant"
        return "user"


class ChatShareLink(models.Model):
    TOKEN_BYTES = 32
    DEFAULT_EXPIRATION_DAYS = 30

    session = models.ForeignKey(
        ChatSession,
        on_delete=models.CASCADE,
        related_name="share_links",
        verbose_name="Shared chat session",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="llm_chat_share_links",
        verbose_name="Created by",
    )
    token = models.CharField(max_length=96, unique=True, db_index=True, verbose_name="Referral token")
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="Is active")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Created at")
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name="Expires at")
    last_opened_at = models.DateTimeField(null=True, blank=True, verbose_name="Last opened at")

    class Meta:
        ordering = ("-created_at", "-id")

    def __str__(self):
        return f"ShareLink#{self.id} session={self.session_id} active={self.is_active}"

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(ChatShareLink.TOKEN_BYTES)

    @classmethod
    def default_expiration(cls):
        return timezone.now() + timedelta(days=cls.DEFAULT_EXPIRATION_DAYS)

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        return self.expires_at <= timezone.now()

    @property
    def is_usable(self) -> bool:
        return self.is_active and not self.is_expired

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = self.generate_token()
        if self.expires_at is None:
            self.expires_at = self.default_expiration()
        super().save(*args, **kwargs)


class ChatShareImport(models.Model):
    share_link = models.ForeignKey(
        ChatShareLink,
        on_delete=models.CASCADE,
        related_name="imports",
        verbose_name="Share link",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="llm_chat_share_imports",
        verbose_name="Imported by",
    )
    session = models.ForeignKey(
        ChatSession,
        on_delete=models.CASCADE,
        related_name="share_imports",
        verbose_name="Imported session",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Created at")

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(fields=["share_link", "user"], name="unique_llm_share_import_per_user"),
        ]

    def __str__(self):
        return f"ShareImport#{self.id} link={self.share_link_id} user={self.user_id} session={self.session_id}"
