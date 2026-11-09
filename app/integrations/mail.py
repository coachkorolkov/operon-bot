"""Приглашения на встречи письмом — когда Google приглашать не даёт.

Google не разрешает сервисному аккаунту приглашать участников без
делегирования в масштабе домена: событие с участником отклоняется целиком
(403 ``forbiddenForServiceAccounts``). Делегирование доступно только в Google
Workspace, а у личного Gmail его нет — значит приглашение нужно отправить
самостоятельно.

Письмо уходит от имени владельца календаря (его почта и пароль приложения) и
содержит файл .ics: получатель открывает его, и встреча добавляется в его
календарь. Для участника разница с приглашением Google невелика — дата,
время, место и описание на месте.

Почему не «пригласить от бота»: адрес вида ``bot@…`` участники не знают и
воспримут как спам. Письмо от знакомого адреса читают.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Any

from ..config import settings

# Короткий таймаут: письмо не должно задерживать ответ бота. Человек ждёт
# ответа в чате, а не подтверждения от почтового сервера.
SMTP_TIMEOUT_SECONDS = 20


@dataclass
class Delivery:
    """Что получилось: кому ушло, кому нет и почему."""

    sent: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.sent) and not self.failed

    def summary(self) -> str:
        """Одна строка для модели и человека — без технических подробностей."""
        if self.skipped_reason:
            return self.skipped_reason
        if self.sent and not self.failed:
            return f"Приглашение отправлено: {', '.join(self.sent)}."
        if self.sent and self.failed:
            bad = ", ".join(item["email"] for item in self.failed)
            return f"Приглашение отправлено: {', '.join(self.sent)}. Не удалось: {bad}."
        bad = ", ".join(item["email"] for item in self.failed) or "адреса не указаны"
        return f"Приглашение отправить не удалось: {bad}."


def configured() -> bool:
    """Есть ли чем отправлять. Без настроек приглашения просто не уходят."""
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password)


def sender() -> str:
    return settings.smtp_from or settings.smtp_user


def problem() -> str:
    """Почему письма не уйдут — для /check и честного ответа человеку."""
    if not settings.smtp_host:
        return (
            "Почта для приглашений не настроена: задайте OPERON_SMTP_HOST, "
            "OPERON_SMTP_USER и OPERON_SMTP_PASSWORD в .env."
        )
    if not settings.smtp_user or not settings.smtp_password:
        return (
            "Почта для приглашений не настроена: нужны OPERON_SMTP_USER и "
            "OPERON_SMTP_PASSWORD (пароль приложения Gmail, не обычный пароль)."
        )
    return ""


def _ics_escape(text: str) -> str:
    """Правила RFC 5545: запятая, точка с запятой, обратный слэш и перевод строки."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _ics_fold(line: str) -> str:
    """Длинные строки в .ics переносятся по 75 байт — иначе часть клиентов молчит."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts: list[str] = []
    start = 0
    limit = 75
    while start < len(encoded):
        chunk = encoded[start:start + limit]
        # Не разрываем многобайтовый символ пополам.
        while chunk and (encoded[start + len(chunk) - 1] & 0xC0) == 0x80:
            chunk = chunk[:-1]
        parts.append(chunk.decode("utf-8", errors="ignore"))
        start += len(chunk)
        limit = 74  # у продолжения первый символ — пробел
    return "\r\n ".join(parts)


def build_ics(
    *,
    title: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    organizer_email: str = "",
    attendees: list[str] | None = None,
    all_day: bool = False,
    uid_seed: str = "",
) -> str:
    """Файл .ics с одной встречей.

    ``start``/``end`` — либо «2026-11-20T15:00» (местное время, как его назвал
    человек), либо «2026-11-20» для события на весь день. Часовой пояс
    подставляется из настроек, чтобы время не поехало.
    """
    zone = settings.timezone_name
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//OPERON//Assistant//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
    ]

    def stamp(value: str) -> str:
        if all_day:
            return value[:10].replace("-", "") + "T000000Z"
        cleaned = value.replace("-", "").replace(":", "")
        if "+" in cleaned:
            cleaned = cleaned.split("+")[0]
        if len(cleaned) == 13:  # 20261120T1500
            cleaned += "00"
        return f"{cleaned}Z"

    uid = uid_seed or f"{stamp(start)}-{abs(hash(title)) % 10**8}"
    import datetime as _dt

    def utc(value: str) -> str:
        """Местное время встречи → UTC.

        Иначе пришлось бы класть в файл блок VTIMEZONE: строку
        ``TZID=Europe/Moscow`` строгие клиенты (Outlook) без него игнорируют, и
        встреча приезжает со сдвигом. В UTC часовой пояс не нужен вовсе.
        """
        if all_day or len(value) == 10:
            return value[:10].replace("-", "") + "T000000Z"
        naive = _dt.datetime.fromisoformat(value.replace("Z", "")[:19])
        if naive.tzinfo is None:
            try:
                from zoneinfo import ZoneInfo

                naive = naive.replace(tzinfo=ZoneInfo(zone))
            except Exception:
                naive = naive.replace(tzinfo=_dt.timezone.utc)
        return naive.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines += [
        f"UID:{uid}@operon",
        f"DTSTAMP:{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
    ]
    if all_day:
        lines += [f"DTSTART;VALUE=DATE:{start[:10].replace('-', '')}",
                  f"DTEND;VALUE=DATE:{end[:10].replace('-', '')}"]
    else:
        lines += [f"DTSTART:{utc(start)}", f"DTEND:{utc(end)}"]
    lines.append(f"SUMMARY:{_ics_escape(title)}")
    if location:
        lines.append(f"LOCATION:{_ics_escape(location)}")
    if description:
        lines.append(f"DESCRIPTION:{_ics_escape(description)}")
    if organizer_email:
        lines += [f"ORGANIZER;CN={_ics_escape(settings.org_name)}:mailto:{organizer_email}"]
    for email in attendees or []:
        lines += [
            f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{email}"
        ]
    lines += ["STATUS:CONFIRMED", "SEQUENCE:0", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"


def _message(
    *,
    to: list[str],
    title: str,
    ics: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    organizer_email: str = "",
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"Приглашение: {title}"
    msg["From"] = sender()
    msg["To"] = ", ".join(to)
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = make_msgid(domain="operon")
    # Заголовок-приглашение: Gmail и Outlook показывают кнопку «Добавить».
    msg["Content-Class"] = "urn:content-classes:calendarmessage"

    body = [
        f"{settings.org_name}: приглашение на встречу",
        "",
        f"Тема: {title}",
        f"Начало: {start}",
        f"Окончание: {end}",
    ]
    if location:
        body.append(f"Место: {location}")
    if description:
        body += ["", description]
    body += [
        "",
        "Во вложении файл встречи (.ics) — откройте его, чтобы добавить "
        "событие в свой календарь.",
    ]
    msg.set_content("\n".join(body))

    msg.add_alternative(
        "\r\n".join(
            [
                '<div style="font-family:sans-serif;font-size:15px;line-height:1.5">',
                f"<p><b>{settings.org_name}</b> приглашает вас на встречу.</p>",
                f"<p><b>{title}</b><br>{start} — {end}"
                + (f"<br>Место: {location}" if location else "")
                + "</p>",
                (f"<p>{description}</p>" if description else ""),
                "<p>Во вложении файл встречи — откройте его, чтобы добавить "
                "событие в свой календарь.</p>",
                "</div>",
            ]
        ),
        subtype="html",
    )
    msg.add_attachment(
        ics.encode("utf-8"),
        maintype="text",
        subtype="calendar",
        filename="invite.ics",
        params={"method": "REQUEST"},
    )
    return msg


def send_invites(
    *,
    attendees: list[str],
    title: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    organizer_email: str = "",
    all_day: bool = False,
    copy_owner: bool | None = None,
) -> Delivery:
    """Разослать приглашение участникам. Возвращает, кому ушло, а кому нет.

    Ошибка отправки одному участнику не мешает остальным и не отменяет уже
    созданное событие: встреча в календаре остаётся, письмо можно повторить.
    """
    result = Delivery()
    if not attendees:
        result.skipped_reason = "Участники не указаны — приглашение не требуется."
        return result
    reason = problem()
    if reason:
        result.skipped_reason = reason
        return result

    ics = build_ics(
        title=title,
        start=start,
        end=end,
        description=description,
        location=location,
        organizer_email=organizer_email or sender(),
        attendees=attendees,
        all_day=all_day,
    )

    try:
        # 587 — STARTTLS; на 465 нужен SSL с первого байта.
        if settings.smtp_port == 465:
            server: Any = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port,
                timeout=SMTP_TIMEOUT_SECONDS, context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
            )
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        with server:
            server.login(settings.smtp_user, settings.smtp_password)
            for email in attendees:
                try:
                    server.send_message(
                        _message(
                            to=[email],
                            title=title,
                            ics=ics,
                            start=start,
                            end=end,
                            description=description,
                            location=location,
                            organizer_email=organizer_email,
                        )
                    )
                    result.sent.append(email)
                except Exception as exc:  # адрес не принят, ящик недоступен и т. п.
                    result.failed.append({"email": email, "reason": str(exc)[:200]})
            if copy_owner is None:
                copy_owner = settings.smtp_copy_owner
            if copy_owner and organizer_email and organizer_email not in attendees:
                try:
                    server.send_message(
                        _message(
                            to=[organizer_email],
                            title=title,
                            ics=ics,
                            start=start,
                            end=end,
                            description=description,
                            location=location,
                            organizer_email=organizer_email,
                        )
                    )
                except Exception:
                    # Копия владельцу — удобство, а не обещание: молчим.
                    pass
    except Exception as exc:
        result.skipped_reason = (
            "Почтовый сервер недоступен, приглашение не отправлено "
            f"({exc.__class__.__name__}: {str(exc)[:150]})."
        )
    return result
