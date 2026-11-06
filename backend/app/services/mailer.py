"""Доставка писем (этап 10, B).

Задача: ссылки подтверждения email и сброса пароля должны доходить до
пользователя. Полноценный SMTP-клиент здесь не эмулируется — есть два
режима, и оба честные:

* `console` (по умолчанию в dev/test) — письмо печатается в лог
  (с вычищенными секретами) и НЕ отправляется. Это позволяет пройти
  сценарий целиком без внешнего сервиса.
* `smtp` — реальная отправка через `smtplib` (STARTTLS/SSL) настройками
  `SMTP_*`. В production при `EMAIL_VERIFICATION_REQUIRED=true` режим
  `console` запрещён проверкой конфигурации: иначе пользователь никогда
  не подтвердит адрес, а система будет считать это «работает».

Тело письма не содержит токен в логах: в лог попадает факт отправки, а
ссылка — только в письмо (или в ответ API в dev-режиме).
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import metrics

logger = get_logger(__name__)


@dataclass
class Mailer:
    mode: str

    def send(self, *, to: str, subject: str, body: str) -> str:
        if self.mode == "smtp":
            return self._send_smtp(to=to, subject=subject, body=body)
        return self._send_console(to=to, subject=subject, body=body)

    def _send_console(self, *, to: str, subject: str, body: str) -> str:
        # Токен не печатаем: лог — не место для секретов. Тесты и dev-сценарии
        # получают токен через ответ API (`dev_token`).
        logger.info(
            "email_console_delivery",
            extra={"to": to, "subject": subject, "body_length": len(body)},
        )
        metrics.incr("email_sent", outcome="console")
        return "console"

    def _send_smtp(self, *, to: str, subject: str, body: str) -> str:
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        try:
            if settings.smtp_use_ssl:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(
                    settings.smtp_host, settings.smtp_port, context=context, timeout=10
                ) as client:
                    if settings.smtp_user:
                        client.login(settings.smtp_user, settings.smtp_password)
                    client.send_message(message)
            else:
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
                    if settings.smtp_starttls:
                        client.starttls(context=ssl.create_default_context())
                    if settings.smtp_user:
                        client.login(settings.smtp_user, settings.smtp_password)
                    client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            metrics.incr("email_sent", outcome="error")
            logger.warning("email_send_failed", extra={"error": type(exc).__name__})
            raise
        metrics.incr("email_sent", outcome="ok")
        return "email"


def get_mailer() -> Mailer:
    return Mailer(mode=settings.email_delivery)


def verification_message(*, display_name: str, token: str) -> tuple[str, str]:
    link = f"{settings.public_base_url.rstrip('/')}/verify-email?token={token}"
    subject = "Подтвердите адрес электронной почты — Agent Civilization"
    body = (
        f"Здравствуйте, {display_name}!\n\n"
        f"Подтвердите адрес электронной почты по ссылке (действует "
        f"{settings.email_verification_ttl_seconds // 3600} ч):\n{link}\n\n"
        "Если вы не регистрировались, просто проигнорируйте письмо.\n"
    )
    return subject, body


def password_reset_message(*, display_name: str, token: str) -> tuple[str, str]:
    link = f"{settings.public_base_url.rstrip('/')}/reset-password?token={token}"
    subject = "Восстановление доступа — Agent Civilization"
    body = (
        f"Здравствуйте, {display_name}!\n\n"
        f"Ссылка для сброса пароля (действует "
        f"{settings.password_reset_ttl_seconds // 60} мин, одноразовая):\n{link}\n\n"
        "Если вы не запрашивали сброс, проигнорируйте письмо: пароль не изменится.\n"
    )
    return subject, body
