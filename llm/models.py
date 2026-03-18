from django.conf import settings
from django.db import models


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
