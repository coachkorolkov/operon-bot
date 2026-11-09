"""Приглашения участникам письмом — когда Google приглашать не даёт.

Жалоба: «бот не может отправлять письма с уведомлениями о встречах».
Причина в Google: сервисный аккаунт без делегирования в масштабе домена
приглашать участников не может, и событие с участником отклоняется целиком
(403 forbiddenForServiceAccounts). Делегирование есть только в Google
Workspace; у личного Gmail его нет.

Проверяется: письмо уходит от имени владельца календаря, содержит .ics с
правильным временем, ошибка одного адреса не отменяет остальные, без
настроек почты событие всё равно создаётся, а человеку честно говорят, что
приглашение не ушло.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app import config
from app.integrations import accounts, google_client, google_sa, mail
from app.tools import calendar as cal
from app.tools.base import registry

from tests.test_google_service_account import KIRILL, delegation, sa  # noqa: F401 — фикстура


class FakeSMTP:
    """Подменяет smtplib.SMTP: запоминает письма, умеет отказывать по адресу."""

    instances: list["FakeSMTP"] = []
    fail_recipients: set[str] = set()
    fail_on_connect: Exception | None = None

    def __init__(self, host, port, timeout=None):
        if FakeSMTP.fail_on_connect:
            raise FakeSMTP.fail_on_connect
        self.host, self.port = host, port
        self.messages: list = []
        self.logged_in = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        return (250, b"ok")

    def starttls(self, context=None):
        return (220, b"ok")

    def login(self, user, password):
        self.logged_in = True
        return (235, b"ok")

    def send_message(self, msg):
        to = msg["To"]
        if any(bad in to for bad in FakeSMTP.fail_recipients):
            raise smtplib_error()
        self.messages.append(msg)
        return {}


def smtplib_error():
    import smtplib

    return smtplib.SMTPRecipientsRefused({"x": (550, b"no such user")})


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_recipients = set()
    FakeSMTP.fail_on_connect = None
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    # Ссылку берём из самого модуля mail: соседний тест мог перезагрузить
    # app.config, и config.settings тогда — уже другой объект. Настройки
    # заморожены (frozen dataclass), поэтому подменяем объект целиком.
    monkeypatch.setattr(
        mail,
        "settings",
        replace(
            mail.settings,
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_user="kirill@gmail.com",
            smtp_password="app-password",
            smtp_from="",
            smtp_copy_owner=False,
        ),
    )
    yield


# --- .ics -----------------------------------------------------------------


def unfold(ics: str) -> str:
    """Развернуть переносы строк .ics (RFC 5545 велит резать длинные строки).

    Без этого проверка «есть ли адрес в файле» падает на длинном адресе,
    который разорван посередине, — хотя файл составлен правильно.
    """
    return ics.replace("\r\n ", "")


def test_ics_has_the_meeting_and_attendees() -> None:
    ics = unfold(
        mail.build_ics(
            title="Встреча с Михаилом",
            start="2026-11-20T15:00",
            end="2026-11-20T15:30",
            description="Обсуждение КП",
            location="Офис",
            organizer_email="kirill@gmail.com",
            attendees=["mikhail@example.ru"],
        )
    )
    assert "BEGIN:VCALENDAR" in ics and "END:VCALENDAR" in ics
    assert "SUMMARY:Встреча с Михаилом" in ics
    assert "LOCATION:Офис" in ics
    assert "mailto:mikhail@example.ru" in ics
    assert "ORGANIZER" in ics and "kirill@gmail.com" in ics


def test_ics_time_is_converted_to_utc() -> None:
    """15:00 по Москве — это 12:00 UTC. Со сдвигом встреча приедет не в тот час."""
    ics = mail.build_ics(
        title="Звонок", start="2026-11-20T15:00", end="2026-11-20T15:30",
        organizer_email="k@gmail.com",
    )
    assert "DTSTART:20261120T120000Z" in ics
    assert "DTEND:20261120T123000Z" in ics
    assert "TZID" not in ics, "без блока VTIMEZONE ссылка на пояс ломает Outlook"


def test_ics_all_day_uses_dates() -> None:
    ics = mail.build_ics(
        title="Отпуск", start="2026-11-20", end="2026-11-21",
        all_day=True, organizer_email="k@gmail.com",
    )
    assert "DTSTART;VALUE=DATE:20261120" in ics
    assert "DTEND;VALUE=DATE:20261121" in ics


def test_ics_escapes_special_characters() -> None:
    ics = mail.build_ics(
        title="Встреча, обсуждение; итоги",
        start="2026-11-20T10:00", end="2026-11-20T11:00",
        organizer_email="k@gmail.com",
    )
    assert "SUMMARY:Встреча\\, обсуждение\\; итоги" in ics


# --- отправка -------------------------------------------------------------


def test_letter_goes_from_the_calendar_owner() -> None:
    delivery = mail.send_invites(
        attendees=["mikhail@example.ru"],
        title="Совет",
        start="2026-11-20T10:00",
        end="2026-11-20T11:00",
        organizer_email="kirill@gmail.com",
    )
    assert delivery.ok and delivery.sent == ["mikhail@example.ru"]
    sent = FakeSMTP.instances[0].messages[0]
    assert sent["From"] == "kirill@gmail.com", "письмо должно идти от владельца календаря"
    assert sent["To"] == "mikhail@example.ru"
    assert "Приглашение" in sent["Subject"]


def test_letter_carries_ics_attachment() -> None:
    mail.send_invites(
        attendees=["m@example.ru"], title="Совет",
        start="2026-11-20T10:00", end="2026-11-20T11:00",
        organizer_email="kirill@gmail.com",
    )
    msg = FakeSMTP.instances[0].messages[0]
    parts = list(msg.iter_attachments())
    assert parts, "без файла .ics письмо не добавляет встречу в календарь"
    assert parts[0].get_filename() == "invite.ics"


def test_one_bad_address_does_not_stop_the_rest() -> None:
    FakeSMTP.fail_recipients = {"bad@example.ru"}
    delivery = mail.send_invites(
        attendees=["bad@example.ru", "good@example.ru"],
        title="Совет", start="2026-11-20T10:00", end="2026-11-20T11:00",
        organizer_email="kirill@gmail.com",
    )
    assert delivery.sent == ["good@example.ru"]
    assert [item["email"] for item in delivery.failed] == ["bad@example.ru"]
    assert "Не удалось" in delivery.summary()


def test_without_settings_nothing_is_sent_and_reason_is_named(monkeypatch) -> None:
    monkeypatch.setattr(mail, "settings", replace(mail.settings, smtp_host=""))
    delivery = mail.send_invites(
        attendees=["m@example.ru"], title="Совет",
        start="2026-11-20T10:00", end="2026-11-20T11:00",
    )
    assert not delivery.sent
    assert "не настроена" in delivery.skipped_reason
    assert not FakeSMTP.instances, "нечего и подключаться без настроек"


def test_unreachable_server_is_reported_not_hidden() -> None:
    FakeSMTP.fail_on_connect = OSError("connection refused")
    delivery = mail.send_invites(
        attendees=["m@example.ru"], title="Совет",
        start="2026-11-20T10:00", end="2026-11-20T11:00",
    )
    assert not delivery.sent
    assert "недоступен" in delivery.skipped_reason


def test_owner_gets_a_copy_when_asked() -> None:
    """Копия владельцу: он видит, что именно ушло участникам."""
    mail.send_invites(
        attendees=["m@example.ru"], title="Совет",
        start="2026-11-20T10:00", end="2026-11-20T11:00",
        organizer_email="kirill@gmail.com", copy_owner=True,
    )
    to = [msg["To"] for msg in FakeSMTP.instances[0].messages]
    assert "m@example.ru" in to and "kirill@gmail.com" in to


# --- что видит человек -----------------------------------------------------


def test_created_event_reports_who_got_the_letter(sa, monkeypatch) -> None:  # noqa: F811
    """Событие создаётся, а в ответе — кому ушло приглашение."""
    delegation(monkeypatch, works=False, access="writer")
    with accounts.use(KIRILL):
        google_sa.connect("kirill@gmail.com")
        assert google_client.can_invite() is False, "готовим именно случай без рассылки"

        created: dict = {}

        class FakeEvents:
            def insert(self, **kwargs):
                created.update(kwargs)
                return self

            def execute(self):
                return {"id": "ev1", "summary": "Совет",
                        "start": {"dateTime": "2026-11-20T10:00:00+03:00"},
                        "end": {"dateTime": "2026-11-20T11:00:00+03:00"}}

            def get(self, **kwargs):
                return self

        class FakeCal:
            def events(self):
                return FakeEvents()

        monkeypatch.setattr(cal, "_calendar", lambda: FakeCal())

        content, is_error = registry.execute("calendar_create_event", {
            "title": "Совет",
            "start": "2026-11-20T10:00",
            "end": "2026-11-20T11:00",
            "attendees": ["mikhail@example.ru"],
        })
        assert not is_error, content
        data = json.loads(content) if isinstance(content, str) else content
        assert data["invites"]["status"] == "sent", data
        assert data["invites"]["to"] == ["mikhail@example.ru"]
        assert "mikhail@example.ru" in data["note"]
        # Google получил событие без участников — иначе 403 на всё событие.
        assert "attendees" not in created["body"]
        assert created["sendUpdates"] == "none"


def test_event_is_created_even_if_the_letter_fails(sa, monkeypatch) -> None:  # noqa: F811
    """Встреча в календаре важнее письма: она остаётся, о письме говорят честно."""
    delegation(monkeypatch, works=False, access="writer")
    FakeSMTP.fail_on_connect = OSError("connection refused")
    with accounts.use(KIRILL):
        google_sa.connect("kirill@gmail.com")

        class FakeEvents:
            def insert(self, **kwargs):
                return self

            def execute(self):
                return {"id": "ev2", "summary": "Совет",
                        "start": {"dateTime": "2026-11-20T10:00:00+03:00"},
                        "end": {"dateTime": "2026-11-20T11:00:00+03:00"}}

        class FakeCal:
            def events(self):
                return FakeEvents()

        monkeypatch.setattr(cal, "_calendar", lambda: FakeCal())

        content, is_error = registry.execute("calendar_create_event", {
            "title": "Совет", "start": "2026-11-20T10:00",
            "end": "2026-11-20T11:00", "attendees": ["m@example.ru"],
        })
        assert not is_error, content
        data = json.loads(content) if isinstance(content, str) else content
        assert data["status"] == "created", "событие должно остаться"
        assert data["invites"]["status"] == "failed"
        assert "недоступен" in data["note"]


def test_no_attendees_means_no_letter(sa, monkeypatch) -> None:  # noqa: F811
    delegation(monkeypatch, works=False, access="writer")
    with accounts.use(KIRILL):
        google_sa.connect("kirill@gmail.com")

        class FakeEvents:
            def insert(self, **kwargs):
                return self

            def execute(self):
                return {"id": "ev3", "summary": "Совет",
                        "start": {"dateTime": "2026-11-20T10:00:00+03:00"},
                        "end": {"dateTime": "2026-11-20T11:00:00+03:00"}}

        class FakeCal:
            def events(self):
                return FakeEvents()

        monkeypatch.setattr(cal, "_calendar", lambda: FakeCal())

        content, _ = registry.execute("calendar_create_event", {
            "title": "Совет", "start": "2026-11-20T10:00", "end": "2026-11-20T11:00",
        })
        data = json.loads(content) if isinstance(content, str) else content
        assert "invites" not in data
        assert not FakeSMTP.instances


# --- /check -----------------------------------------------------------------


def test_selfcheck_warns_when_mail_is_not_configured(monkeypatch) -> None:
    """Участник письма не получит — человек должен узнать это заранее."""
    from app import selfcheck

    monkeypatch.setattr(mail, "settings", replace(mail.settings, smtp_host=""))
    monkeypatch.setattr(google_client, "can_invite", lambda: False)
    checks = selfcheck.check_mail()
    assert checks[0].status == selfcheck.SKIP
    assert "не настроена" in checks[0].detail
    assert any("SMTP" in hint or "OPERON_SMTP" in hint for hint in checks[0].hints)


def test_selfcheck_confirms_mail_when_configured(monkeypatch) -> None:
    from app import selfcheck

    monkeypatch.setattr(google_client, "can_invite", lambda: False)
    checks = selfcheck.check_mail()
    assert checks[0].status == selfcheck.OK
    assert "kirill@gmail.com" in checks[0].detail


def test_selfcheck_is_fine_when_google_invites_itself(monkeypatch) -> None:
    """Вход через Google умеет приглашать сам — своя почта не обязательна."""
    from app import selfcheck

    monkeypatch.setattr(google_client, "can_invite", lambda: True)
    monkeypatch.setattr(mail, "settings", replace(mail.settings, smtp_host=""))
    checks = selfcheck.check_mail()
    assert checks[0].status == selfcheck.OK
