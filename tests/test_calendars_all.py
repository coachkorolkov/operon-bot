"""Встречи из всех календарей аккаунта, а не только из основного.

Жалоба: бот видел события личного календаря, но не рабочего, к которому у
аккаунта есть доступ. Проверяется: список календарей берётся у Google (вход
через Google, делегирование) или из добавленных командой (открытый календарь),
события сводятся в один список без повторов, сбой дополнительного календаря
не прячет основной, токен обновляется с выданными при входе правами.
"""

from __future__ import annotations

import json

import pytest

from app.integrations import accounts, google_client, google_sa
from app.tools import calendar as cal
from app.tools.base import registry

from tests.test_google_service_account import KIRILL, delegation, sa  # noqa: F401 — фикстура


def _event(eid, title, start, uid=None):
    return {"id": eid, "iCalUID": uid or eid, "summary": title,
            "start": {"dateTime": start}, "end": {"dateTime": start}}


class FakeCalendar:
    """calendarList + events.list по календарям; broken — календари, отвечающие ошибкой."""

    def __init__(self, calendars, events, broken=(), list_error=None):
        self.calendars, self.events_by_cal, self.broken = calendars, events, set(broken)
        self.list_error, self.asked = list_error, []
        self._op = None

    def calendarList(self):
        self._op = "list"
        return self

    def list(self, **kwargs):
        if self._op == "events":
            self.asked.append(kwargs["calendarId"])
            self._cal = kwargs["calendarId"]
        return self

    def list_next(self, request, page):
        return None

    def events(self):
        self._op = "events"
        return self

    def execute(self):
        if self._op == "list":
            if self.list_error:
                raise self.list_error
            return {"items": self.calendars}
        if self._cal in self.broken:
            raise RuntimeError("403 нет доступа")
        return {"items": self.events_by_cal.get(self._cal, [])}


@pytest.fixture(autouse=True)
def _fresh():
    cal._calendars_cache.clear()
    cal._calendars_problem.clear()
    yield
    cal._calendars_cache.clear()
    cal._calendars_problem.clear()


def oauth(monkeypatch, fake):
    monkeypatch.setattr(google_client, "mode", lambda: "oauth")
    monkeypatch.setattr(cal, "_calendar", lambda: fake)


CALS = [
    {"id": "anna@gmail.com", "summary": "anna@gmail.com", "primary": True},
    {"id": "work@group.calendar.google.com", "summary": "Рабочий"},
    {"id": "ru.russian#holiday@group.v.calendar.google.com", "summary": "Праздники"},
]


def test_events_come_from_every_calendar(monkeypatch) -> None:
    fake = FakeCalendar(CALS, {
        "anna@gmail.com": [_event("p1", "Личное", "2026-10-30T12:00:00+03:00"),
                           _event("p2", "Планёрка", "2026-10-30T09:00:00+03:00", uid="same")],
        "work@group.calendar.google.com": [_event("w1", "Совет директоров", "2026-10-30T10:00:00+03:00"),
                                           _event("w2", "Планёрка", "2026-10-30T09:00:00+03:00", uid="same")],
    })
    oauth(monkeypatch, fake)
    content, is_error = registry.execute("calendar_list_events", {"time_min": "2026-10-30", "time_max": "2026-10-30"})
    assert not is_error, content
    data = json.loads(content) if isinstance(content, str) else content
    titles = [e["title"] for e in data["events"]]
    assert titles == ["Планёрка", "Совет директоров", "Личное"], "по времени, без повтора приглашения"
    assert data["calendars"] == ["anna@gmail.com", "Рабочий"], "праздники — не встречи"
    work = next(e for e in data["events"] if e["title"] == "Совет директоров")
    assert work["calendar_id"] == "work@group.calendar.google.com" and work["calendar"] == "Рабочий"


def test_broken_extra_calendar_does_not_hide_the_rest(monkeypatch) -> None:
    fake = FakeCalendar(CALS, {"anna@gmail.com": [_event("p1", "Личное", "2026-10-30T12:00:00+03:00")]},
                        broken={"work@group.calendar.google.com"})
    oauth(monkeypatch, fake)
    from datetime import date
    assert [e["title"] for e in cal.list_events_between(date(2026, 10, 30), date(2026, 10, 30))] == ["Личное"]


def test_old_token_without_list_right_reads_primary(monkeypatch) -> None:
    fake = FakeCalendar([], {"primary": [_event("p1", "Личное", "2026-10-30T12:00:00+03:00")]},
                        list_error=RuntimeError("insufficientPermissions"))
    oauth(monkeypatch, fake)
    assert cal.readable_calendars() == [{"id": "primary", "name": "основной", "primary": True}]


def test_old_token_is_reported_not_silent(monkeypatch) -> None:
    """«Вижу только основной» — сказать вслух, а не показывать половину встреч.

    Со стороны это неотличимо от «в рабочем календаре ничего нет»: человек
    получает правдоподобный, но неполный ответ и не знает, что делать.
    """
    fake = FakeCalendar([], {"primary": [_event("p1", "Личное", "2026-10-30T12:00:00+03:00")]},
                        list_error=RuntimeError("insufficientPermissions"))
    oauth(monkeypatch, fake)
    cal.readable_calendars()
    assert cal.calendars_problem(), "причина должна быть известна"

    content, is_error = registry.execute(
        "calendar_list_events", {"time_min": "2026-10-30", "time_max": "2026-10-30"}
    )
    assert not is_error, content
    data = json.loads(content) if isinstance(content, str) else content
    assert "warning" in data, "бот обязан предупредить о неполной картине"
    assert "/auth" in data["warning"]


def test_full_list_has_no_warning(monkeypatch) -> None:
    """Когда список получен целиком, пугать нечем."""
    fake = FakeCalendar(CALS, {"anna@gmail.com": [_event("p1", "Личное", "2026-10-30T12:00:00+03:00")]})
    oauth(monkeypatch, fake)
    assert cal.calendars_problem() == ""
    content, _ = registry.execute(
        "calendar_list_events", {"time_min": "2026-10-30", "time_max": "2026-10-30"}
    )
    data = json.loads(content) if isinstance(content, str) else content
    assert "warning" not in data


def test_explicit_calendar_reads_only_it(monkeypatch) -> None:
    fake = FakeCalendar(CALS, {"work@group.calendar.google.com": [_event("w1", "Совет", "2026-10-30T10:00:00+03:00")]})
    oauth(monkeypatch, fake)
    registry.execute("calendar_list_events", {"calendar_id": "work@group.calendar.google.com"})
    assert fake.asked == ["work@group.calendar.google.com"]


def test_shared_mode_reads_added_calendars(sa, monkeypatch) -> None:  # noqa: F811
    delegation(monkeypatch, works=False, access="reader")
    with accounts.use(KIRILL):
        google_sa.connect("kirill@gmail.com")
        assert [c["id"] for c in cal.readable_calendars()] == ["kirill@gmail.com"]
        google_sa.add_calendar("work@group.calendar.google.com")
        google_sa.add_calendar("work@group.calendar.google.com")  # повтор не дублирует
        assert [c["id"] for c in cal.readable_calendars()] == ["kirill@gmail.com", "work@group.calendar.google.com"]
        assert google_sa.remove_calendar("work@group.calendar.google.com")
        assert [c["id"] for c in cal.readable_calendars()] == ["kirill@gmail.com"]


def test_shared_mode_refuses_calendar_not_opened(sa, monkeypatch) -> None:  # noqa: F811
    delegation(monkeypatch, works=False, access="writer")
    with accounts.use(KIRILL):
        google_sa.connect("kirill@gmail.com")
        monkeypatch.setattr(google_sa, "_shared_access", lambda mail: "")
        with pytest.raises(google_sa.LinkError) as exc:
            google_sa.add_calendar("work@group.calendar.google.com")
    assert google_sa.email() in str(exc.value)


def test_token_refreshes_with_its_own_scopes(monkeypatch) -> None:
    """Расширенный набор прав не должен ломать уже выданный токен (invalid_scope)."""
    from app.integrations import token_store

    token = {"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
             "token_uri": "https://oauth2.googleapis.com/token",
             "scopes": ["https://www.googleapis.com/auth/calendar.events"],
             "expiry": "2999-01-01T00:00:00Z"}
    monkeypatch.setattr(token_store, "load_token", lambda: json.dumps(token))
    google_client.reset_cache()
    creds = google_client._load_credentials()
    assert list(creds.scopes) == ["https://www.googleapis.com/auth/calendar.events"]
    google_client.reset_cache()


def test_calendars_command_lists_and_adds(sa, monkeypatch) -> None:  # noqa: F811
    from tests.test_telegram_bot import FakeAgent, make_bot, message

    delegation(monkeypatch, works=False, access="writer")
    bot, api = make_bot(FakeAgent([{"type": "done", "stop": "end_turn"}]), monkeypatch)
    with accounts.use(KIRILL):
        google_sa.connect("kirill@gmail.com")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"100500,{KIRILL}")
    bot._allowed = frozenset({100500, int(KIRILL)})
    bot._handle_update(message("/calendars add work@group.calendar.google.com", user_id=int(KIRILL)))
    assert "добавлен" in api.texts()[-1]
    bot._handle_update(message("/calendars", user_id=int(KIRILL)))
    text = api.texts()[-1]
    assert "kirill@gmail.com" not in text or "основной" in text
    assert "work@group.calendar.google.com" in text and google_sa.email() in text


def test_preset_connects_mail_and_work_calendar_without_commands(sa, monkeypatch) -> None:  # noqa: F811
    """GOOGLE_ACCOUNTS: почта и рабочий календарь — сразу, без /auth и /calendars."""
    delegation(monkeypatch, works=False, access="writer")
    monkeypatch.setenv("GOOGLE_ACCOUNTS", f"{KIRILL}=7673022@gmail.com|work@group.calendar.google.com")
    with accounts.use(KIRILL):
        assert google_client.mode() == "shared"
        assert google_client.default_calendar() == "7673022@gmail.com"
        assert [c["id"] for c in cal.readable_calendars()] == ["7673022@gmail.com", "work@group.calendar.google.com"]


def test_preset_calendars_join_an_existing_link(sa, monkeypatch) -> None:  # noqa: F811
    delegation(monkeypatch, works=False, access="writer")
    with accounts.use(KIRILL):
        google_sa.connect("7673022@gmail.com")  # раньше подключили командой /auth
        monkeypatch.setenv("GOOGLE_ACCOUNTS", f"{KIRILL}=7673022@gmail.com|work@group.calendar.google.com")
        assert [c["id"] for c in cal.readable_calendars()][-1] == "work@group.calendar.google.com"
