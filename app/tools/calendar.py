"""Инструменты Google Calendar: встречи, сроки, напоминания."""

from __future__ import annotations

import logging
import threading
import time as _clock
from datetime import date, datetime, time, timedelta
from typing import Any

from ..config import settings
from ..integrations import accounts, google_client, google_sa
from ..integrations.google_client import HttpError, describe_http_error, get_service
from .base import Preview, ToolError, ToolSpec, registry


logger = logging.getLogger(__name__)


def _calendar():
    return get_service("calendar", "v3")


# --- какие календари читать ----------------------------------------------------
#
# Раньше бот читал только основной календарь, и встречи из рабочего календаря,
# который аккаунт видит (общий календарь компании, календарь коллеги), не
# попадали ни в ответы, ни в сводку, ни в напоминания. Теперь — все.

CALENDARS_TTL = 300
# Праздники, дни рождения контактов, номера недель — не встречи.
_SKIP = ("#holiday@", "#contacts@", "#weeknum@", "addressbook#")
_calendars_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_calendars_lock = threading.Lock()
# Почему список календарей не удалось получить (по аккаунту). Нужно, чтобы
# сказать человеку правду «вижу только основной», а не молча показывать
# половину встреч: со стороны это выглядит как «бот не видит рабочий календарь».
_calendars_problem: dict[str, str] = {}


def readable_calendars() -> list[dict[str, Any]]:
    """Календари подключённого аккаунта: [{id, name, primary}], основной первым.

    Вход через Google и делегирование — список берётся у Google (все, что
    видит аккаунт). Открытый боту календарь (сервисный аккаунт) — основной
    плюс добавленные командой /calendars: свои списки календарей человека
    сервисному аккаунту не видны.
    """
    default = google_client.default_calendar()
    if google_client.mode() == "shared":
        link = google_sa.current_link() or {}
        extra = [c for c in link.get("calendars") or [] if c and c != default]
        return [{"id": default, "name": "основной", "primary": True}] + [
            {"id": c, "name": c, "primary": False} for c in extra
        ]

    key = accounts.resolve()
    now = _clock.monotonic()
    with _calendars_lock:
        cached = _calendars_cache.get(key)
    if cached and now - cached[0] < CALENDARS_TTL:
        return cached[1]

    found: list[dict[str, Any]] = []
    try:
        request = _calendar().calendarList().list(minAccessRole="reader", showHidden=False)
        while request is not None:
            page = request.execute()
            for item in page.get("items", []):
                cid = item.get("id") or ""
                if not cid or any(mark in cid for mark in _SKIP):
                    continue
                found.append({
                    "id": cid,
                    "name": item.get("summaryOverride") or item.get("summary") or cid,
                    "primary": bool(item.get("primary")),
                })
            request = _calendar().calendarList().list_next(request, page)
    except Exception as exc:  # noqa: BLE001 — нет права на список (старый токен): только основной
        logger.info("Список календарей недоступен, читаю только основной: %s", exc)
        found = []
        with _calendars_lock:
            _calendars_problem[key] = str(exc)[:300]
    else:
        with _calendars_lock:
            _calendars_problem.pop(key, None)
    if not any(c["primary"] for c in found):
        found.insert(0, {"id": default, "name": "основной", "primary": True})
    found.sort(key=lambda c: not c["primary"])
    with _calendars_lock:
        _calendars_cache[key] = (now, found)
    return found


def calendars_problem() -> str:
    """Почему виден только основной календарь. Пусто — список получен целиком.

    Старый токен выдан без права на список календарей: встречи из рабочего
    календаря в него не попадают, и человеку об этом нужно сказать прямо —
    сам он этого не заметит, ответы выглядят правдоподобно.
    """
    if google_client.mode() == "shared":
        return ""
    key = accounts.resolve()
    with _calendars_lock:
        problem = _calendars_problem.get(key, "")
    if not problem and not _calendars_cache.get(key):
        return ""  # ещё не спрашивали — узнаем при первом чтении
    return problem


def _events_from(calendars: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    """События из нескольких календарей одним списком, по времени, без повторов.

    Сбой основного календаря — ошибка; сбой дополнительного — пропуск с записью
    в журнал: один недоступный календарь не должен прятать остальные.
    """
    views: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for cal in calendars:
        try:
            items = _calendar().events().list(calendarId=cal["id"], **params).execute().get("items", [])
        except Exception as exc:  # noqa: BLE001
            if cal.get("primary"):
                raise
            logger.warning("Календарь %s не прочитан: %s", cal["id"], exc)
            continue
        for event in items:
            view = _event_view(event, cal["id"])
            if len(calendars) > 1:
                view["calendar"] = cal.get("name") or cal["id"]
            mark = (event.get("iCalUID") or event.get("id") or "", str(view.get("start")))
            if mark in seen:
                continue  # одна встреча в двух календарях (приглашение) — один раз
            seen.add(mark)
            views.append(view)
    views.sort(key=lambda v: str(v.get("start") or ""))
    return views


def _to_rfc3339(value: str, *, end_of_day: bool = False) -> str:
    """Принимает «2026-08-20», «2026-08-20T15:00» или полный RFC3339."""
    raw = (value or "").strip()
    if not raw:
        raise ToolError("Пустая дата/время.")
    try:
        if len(raw) == 10:
            day = date.fromisoformat(raw)
            moment = datetime.combine(day, time.max if end_of_day else time.min)
        else:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(
            f"Не удалось разобрать дату «{value}». Используйте YYYY-MM-DD или YYYY-MM-DDTHH:MM."
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=settings.tz)
    return moment.isoformat()


def _local(value: str | None) -> str | None:
    """Время события — в часовом поясе бота.

    Google отдаёт время в поясе календаря: у сервисного аккаунта это UTC
    («12:00Z»). Модель передала бы его как есть — и встреча в 15:00 по Москве
    превратилась бы в ответе в «12:00».
    """
    if not value or len(value) == 10:
        return value
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is None:
        return value
    return moment.astimezone(settings.tz).isoformat()


def _event_view(event: dict[str, Any], calendar_id: str) -> dict[str, Any]:
    start = event.get("start", {})
    end = event.get("end", {})
    attendees = [
        {
            "email": a.get("email"),
            "name": a.get("displayName", ""),
            "response": a.get("responseStatus"),
        }
        for a in event.get("attendees", [])
    ]
    return {
        "event_id": event.get("id"),
        "calendar_id": calendar_id,
        "title": event.get("summary", "(без названия)"),
        "start": _local(start.get("dateTime")) or start.get("date"),
        "end": _local(end.get("dateTime")) or end.get("date"),
        "all_day": "date" in start,
        "location": event.get("location", ""),
        "description": (event.get("description") or "")[:2000],
        "organizer": (event.get("organizer") or {}).get("email", ""),
        "attendees": attendees,
        "status": event.get("status"),
        "link": event.get("htmlLink"),
    }


def list_events_between(day_from: date, day_to: date, calendar_id: str = "") -> list[dict[str, Any]]:
    """События за период — для утренней сводки, без участия модели."""
    start = datetime.combine(day_from, time.min).replace(tzinfo=settings.tz)
    end = datetime.combine(day_to, time.max).replace(tzinfo=settings.tz)
    calendars = [{"id": calendar_id, "primary": True}] if calendar_id else readable_calendars()
    return _events_from(calendars, {
        "timeMin": start.isoformat(), "timeMax": end.isoformat(),
        "singleEvents": True, "orderBy": "startTime", "maxResults": 50,
    })


def list_events_window(start: datetime, end: datetime, calendar_id: str = "") -> list[dict[str, Any]]:
    """События, начинающиеся в интервале, — для напоминаний о встречах."""
    calendars = [{"id": calendar_id, "primary": True}] if calendar_id else readable_calendars()
    return _events_from(calendars, {
        "timeMin": start.isoformat(), "timeMax": end.isoformat(),
        "singleEvents": True, "orderBy": "startTime", "maxResults": 20,
    })


def _send_updates() -> str:
    """Уведомлять участников. Сервисному аккаунту без делегирования Google это запрещает."""
    return "all" if google_client.can_invite() else "none"


def _calendar_list_events(tool_input: dict[str, Any]) -> Any:
    explicit = (tool_input.get("calendar_id") or "").strip()
    now = datetime.now(settings.tz)

    time_min = tool_input.get("time_min")
    time_max = tool_input.get("time_max")
    start = _to_rfc3339(time_min) if time_min else now.isoformat()
    end = (
        _to_rfc3339(time_max, end_of_day=True)
        if time_max
        else (now + timedelta(days=14)).isoformat()
    )

    limit = min(max(int(tool_input.get("max_results") or 50), 1), 250)
    params: dict[str, Any] = {
        "timeMin": start,
        "timeMax": end,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": limit,
    }
    if tool_input.get("query"):
        params["q"] = tool_input["query"]

    calendars = [{"id": explicit, "primary": True}] if explicit else readable_calendars()
    try:
        events = _events_from(calendars, params)[:limit]
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Чтение календаря")) from exc

    names = [c.get("name") or c["id"] for c in calendars]
    # Токен без права на список календарей видит только основной. Молчать об
    # этом нельзя: человек спросит про встречу в рабочем календаре, получит
    # «встреч нет» и решит, что бот врёт или сломан.
    narrowed = "" if explicit else calendars_problem()
    partial = (
        "ВНИМАНИЕ: доступен только основной календарь — список календарей аккаунта "
        "получить не удалось (скорее всего вход выполнен старым токеном без нужных прав). "
        "Встречи из рабочих и общих календарей в ответ НЕ попали. Обязательно скажи об этом "
        "пользователю и предложи отправить /auth, чтобы войти заново с новыми правами."
        if narrowed
        else ""
    )
    if not events:
        result: dict[str, Any] = {
            "status": "empty",
            "calendars": names,
            "period": {"from": start, "to": end},
            "hint": "В указанный период встреч нет ни в одном из этих календарей. Не придумывай события.",
        }
        if partial:
            result["warning"] = partial
            result["hint"] = (
                "В этих календарях встреч нет — но учти предупреждение: часть календарей "
                "недоступна, поэтому «встреч нет» может быть неполной картиной."
            )
        return result
    result = {
        "status": "ok",
        "calendars": names,
        "period": {"from": start, "to": end},
        "events_count": len(events),
        "events": events,
        "hint": "Чтобы изменить или удалить событие, передай его calendar_id из этого списка.",
    }
    if partial:
        result["warning"] = partial
    return result


def _build_event_body(tool_input: dict[str, Any]) -> dict[str, Any]:
    title = (tool_input.get("title") or "").strip()
    start = (tool_input.get("start") or "").strip()
    end = (tool_input.get("end") or "").strip()
    if not title:
        raise ToolError("Не указано название события (title).")
    if not start:
        raise ToolError("Не указано время начала (start).")

    all_day = len(start) == 10
    if not end:
        if all_day:
            end = start
        else:
            begin = datetime.fromisoformat(_to_rfc3339(start))
            end = (begin + timedelta(minutes=int(tool_input.get("duration_minutes") or 60))).isoformat()

    body: dict[str, Any] = {"summary": title}
    if all_day:
        finish = date.fromisoformat(end[:10]) + timedelta(days=1)
        body["start"] = {"date": start[:10]}
        body["end"] = {"date": finish.isoformat()}
    else:
        body["start"] = {"dateTime": _to_rfc3339(start), "timeZone": settings.timezone_name}
        body["end"] = {"dateTime": _to_rfc3339(end), "timeZone": settings.timezone_name}

    if tool_input.get("description"):
        body["description"] = tool_input["description"]
    if tool_input.get("location"):
        body["location"] = tool_input["location"]
    attendees = [email for email in (tool_input.get("attendees") or []) if email]
    if attendees and google_client.can_invite():
        body["attendees"] = [{"email": email} for email in attendees]
    elif attendees:
        # Сервисный аккаунт без делегирования приглашать не может — Google
        # отклонил бы всё событие. Участников сохраняем в описании.
        body["description"] = (body.get("description", "") + "\n\nУчастники: " + ", ".join(attendees)).strip()
    reminder_minutes = tool_input.get("reminder_minutes")
    if reminder_minutes is not None:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": int(reminder_minutes)}],
        }
    return body


def _calendar_create_event(tool_input: dict[str, Any]) -> Any:
    calendar_id = (tool_input.get("calendar_id") or google_client.default_calendar()).strip()
    body = _build_event_body(tool_input)
    try:
        created = (
            _calendar()
            .events()
            .insert(
                calendarId=calendar_id,
                body=body,
                sendUpdates="all" if body.get("attendees") else "none",
            )
            .execute()
        )
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Создание события")) from exc
    result: dict[str, Any] = {"status": "created", **_event_view(created, calendar_id)}
    if tool_input.get("attendees") and not google_client.can_invite():
        result["note"] = (
            "Приглашения участникам НЕ отправлены (календарь подключён через общий доступ): "
            "участники записаны в описание встречи. Скажи об этом пользователю одной строкой."
        )
    return result


def _calendar_update_event(tool_input: dict[str, Any]) -> Any:
    calendar_id = (tool_input.get("calendar_id") or google_client.default_calendar()).strip()
    event_id = (tool_input.get("event_id") or "").strip()
    if not event_id:
        raise ToolError("Не указан event_id.")

    try:
        current = _calendar().events().get(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Чтение события перед изменением")) from exc

    patch: dict[str, Any] = {}
    if tool_input.get("title"):
        patch["summary"] = tool_input["title"]
    if tool_input.get("description") is not None:
        patch["description"] = tool_input["description"]
    if tool_input.get("location") is not None:
        patch["location"] = tool_input["location"]
    if tool_input.get("start"):
        start = tool_input["start"]
        patch["start"] = (
            {"date": start[:10]}
            if len(start) == 10
            else {"dateTime": _to_rfc3339(start), "timeZone": settings.timezone_name}
        )
    if tool_input.get("end"):
        end = tool_input["end"]
        patch["end"] = (
            {"date": end[:10]}
            if len(end) == 10
            else {"dateTime": _to_rfc3339(end), "timeZone": settings.timezone_name}
        )
    elif "start" in patch:
        # Перенесли начало без конца — сохраняем длительность. Иначе встреча
        # «с 15 до 11» и Google отказывает, или она растягивается на полдня.
        kept = _shifted_end(current, patch["start"])
        if kept:
            patch["end"] = kept
    if tool_input.get("reminder_minutes") is not None:
        patch["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": int(tool_input["reminder_minutes"])}],
        }
    new_attendees = [e for e in (tool_input.get("add_attendees") or []) if e]
    if new_attendees and not google_client.can_invite():
        base = patch.get("description", current.get("description") or "")
        patch["description"] = (base + "\n\nУчастники: " + ", ".join(new_attendees)).strip()
        new_attendees = []
    if new_attendees:
        existing = current.get("attendees", [])
        known = {(a.get("email") or "").lower() for a in existing}
        patch["attendees"] = existing + [
            {"email": e} for e in new_attendees if e.lower() not in known
        ]
    if not patch:
        raise ToolError("Не переданы поля для изменения.")

    try:
        updated = (
            _calendar()
            .events()
            .patch(calendarId=calendar_id, eventId=event_id, body=patch, sendUpdates=_send_updates())
            .execute()
        )
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Изменение события")) from exc
    return {
        "status": "updated",
        "previous": _event_view(current, calendar_id),
        "current": _event_view(updated, calendar_id),
    }


def _shifted_end(current: dict[str, Any], new_start: dict[str, Any]) -> dict[str, Any] | None:
    old_start, old_end = current.get("start", {}), current.get("end", {})
    try:
        if "dateTime" in new_start and old_start.get("dateTime") and old_end.get("dateTime"):
            length = datetime.fromisoformat(old_end["dateTime"].replace("Z", "+00:00")) - datetime.fromisoformat(
                old_start["dateTime"].replace("Z", "+00:00")
            )
            begin = datetime.fromisoformat(new_start["dateTime"])
            return {"dateTime": (begin + length).isoformat(), "timeZone": settings.timezone_name}
        if "date" in new_start and old_start.get("date") and old_end.get("date"):
            length = date.fromisoformat(old_end["date"]) - date.fromisoformat(old_start["date"])
            return {"date": (date.fromisoformat(new_start["date"]) + length).isoformat()}
    except ValueError:
        return None
    if "date" in new_start:
        return {"date": (date.fromisoformat(new_start["date"]) + timedelta(days=1)).isoformat()}
    return None


def _calendar_delete_event(tool_input: dict[str, Any]) -> Any:
    calendar_id = (tool_input.get("calendar_id") or google_client.default_calendar()).strip()
    event_id = (tool_input.get("event_id") or "").strip()
    if not event_id:
        raise ToolError("Не указан event_id.")
    try:
        current = _calendar().events().get(calendarId=calendar_id, eventId=event_id).execute()
        _calendar().events().delete(
            calendarId=calendar_id, eventId=event_id, sendUpdates=_send_updates()
        ).execute()
    except HttpError as exc:
        raise ToolError(describe_http_error(exc, "Удаление события")) from exc
    return {"status": "deleted", "deleted_event": _event_view(current, calendar_id)}


# --- карточки подтверждения ------------------------------------------------


def _preview_create(tool_input: dict[str, Any]) -> Preview:
    attendees = tool_input.get("attendees") or []
    invites = google_client.can_invite()
    return Preview(
        title="Создать событие в календаре",
        summary=f"«{tool_input.get('title', 'без названия')}», начало {tool_input.get('start', '?')}"
        + (
            (f", участников: {len(attendees)} (им уйдут приглашения)" if invites
             else f", участников: {len(attendees)} — приглашения НЕ рассылаются, участники будут в описании")
            if attendees else ""
        ),
        details={
            "Календарь": tool_input.get("calendar_id") or "primary (основной)",
            "Название": tool_input.get("title", ""),
            "Начало": tool_input.get("start", ""),
            "Окончание": tool_input.get("end") or f"+{tool_input.get('duration_minutes', 60)} мин",
            "Место": tool_input.get("location") or "—",
            "Участники": ", ".join(attendees) if attendees else "—",
            "Напоминание": (
                f"за {tool_input['reminder_minutes']} мин"
                if tool_input.get("reminder_minutes") is not None
                else "по умолчанию"
            ),
            "Описание": (tool_input.get("description") or "—")[:800],
        },
    )


def _describe_event(tool_input: dict[str, Any]) -> str:
    """«Название, начало» — чтобы в карточке было видно, какую встречу трогаем.

    Карточка строится до подтверждения; чтение события ничего не меняет.
    Не прочиталось — показываем идентификатор, как раньше.
    """
    event_id = (tool_input.get("event_id") or "").strip()
    try:
        event = _calendar().events().get(
            calendarId=(tool_input.get("calendar_id") or google_client.default_calendar()).strip(), eventId=event_id
        ).execute()
    except Exception:  # noqa: BLE001 — карточка должна показаться в любом случае
        return f"Событие {event_id or '?'}"
    start = event.get("start", {})
    when = (_local(start.get("dateTime")) or start.get("date") or "").replace("T", " ")[:16]
    return f"«{event.get('summary', '(без названия)')}» ({when})"


_UPDATE_LABELS = {
    "title": "Новое название",
    "start": "Новое начало",
    "end": "Новое окончание",
    "description": "Описание",
    "location": "Место",
    "reminder_minutes": "Напоминание, мин",
    "add_attendees": "Добавить участников",
}


def _preview_update(tool_input: dict[str, Any]) -> Preview:
    changes = {
        _UPDATE_LABELS.get(key, key): (", ".join(value) if isinstance(value, list) else value)
        for key, value in tool_input.items()
        if key not in {"event_id", "calendar_id"} and value not in (None, "", [])
    }
    event = _describe_event(tool_input)
    return Preview(
        title="Изменить событие в календаре",
        summary=f"{event} будет изменено"
        + ("; участники получат уведомление." if google_client.can_invite()
           else "; уведомления участникам не рассылаются, новые участники — в описание."),
        details={"Событие": event, "Календарь": tool_input.get("calendar_id") or "основной", **changes},
    )


def _preview_delete(tool_input: dict[str, Any]) -> Preview:
    event = _describe_event(tool_input)
    return Preview(
        title="Удалить событие из календаря",
        summary=f"{event} будет удалено безвозвратно"
        + (", участникам уйдёт отмена." if google_client.can_invite() else ", участники уведомлены не будут."),
        details={
            "Событие": event,
            "Календарь": tool_input.get("calendar_id") or "основной",
            "Идентификатор события": tool_input.get("event_id", ""),
        },
    )


# --- регистрация -----------------------------------------------------------

registry.register(
    ToolSpec(
        name="calendar_list_events",
        description=(
            "Показывает встречи и сроки из Google Calendar за период. По умолчанию — ближайшие "
            "14 дней. Используй для вопросов о расписании, дедлайнах, загрузке и при подготовке "
            "к встречам. Если событий нет — так и скажи, не придумывай."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "Начало периода: YYYY-MM-DD или YYYY-MM-DDTHH:MM."},
                "time_max": {"type": "string", "description": "Конец периода: YYYY-MM-DD или YYYY-MM-DDTHH:MM."},
                "calendar_id": {
                    "type": "string",
                    "description": (
                        "Не указывай — тогда события берутся из ВСЕХ календарей аккаунта "
                        "(основного и рабочих). Указывай, только если пользователь просит один "
                        "конкретный календарь."
                    ),
                },
                "query": {"type": "string", "description": "Фильтр по тексту события."},
                "max_results": {"type": "integer", "description": "Максимум событий (1–250), по умолчанию 50."},
            },
        },
        handler=_calendar_list_events,
        activity="Смотрю календарь",
    )
)

registry.register(
    ToolSpec(
        name="calendar_create_event",
        description=(
            "Создаёт событие или напоминание в Google Calendar. Применяй для встреч, контрольных "
            "точек, сроков отчётности и напоминаний о поручениях. ВАЖНО: событие создаётся только "
            "после подтверждения пользователя. Если в событии есть участники, им будут отправлены "
            "приглашения — обязательно предупреди об этом."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название события."},
                "start": {
                    "type": "string",
                    "description": "Начало: YYYY-MM-DDTHH:MM или YYYY-MM-DD для события на весь день.",
                },
                "end": {"type": "string", "description": "Окончание в том же формате. Можно не указывать."},
                "duration_minutes": {
                    "type": "integer",
                    "description": "Длительность, если не указано окончание. По умолчанию 60.",
                },
                "description": {"type": "string", "description": "Описание, повестка, ссылки."},
                "location": {"type": "string", "description": "Место или ссылка на видеовстречу."},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "E-mail участников. Им будут отправлены приглашения.",
                },
                "reminder_minutes": {
                    "type": "integer",
                    "description": "За сколько минут напомнить.",
                },
                "calendar_id": {
                    "type": "string",
                    "description": (
                        "Не указывай без необходимости — новое событие создаётся в основном "
                        "календаре. Другой — только если пользователь просит конкретный календарь "
                        "(его calendar_id есть в ответе calendar_list_events)."
                    ),
                },
            },
            "required": ["title", "start"],
        },
        handler=_calendar_create_event,
        requires_confirmation=True,
        preview=_preview_create,
        activity="Создаю событие в календаре",
    )
)

registry.register(
    ToolSpec(
        name="calendar_update_event",
        description=(
            "Изменяет существующее событие календаря: перенос (достаточно нового start — "
            "длительность сохранится), название, место, описание, напоминание, новые участники. "
            "event_id бери из calendar_list_events (ищи по названию через query). "
            "Выполняется только после подтверждения пользователя; участники получат уведомление."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Идентификатор события из calendar_list_events."},
                "calendar_id": {
                    "type": "string",
                    "description": (
                        "calendar_id события из ответа calendar_list_events. Обязательно передай "
                        "его, если событие не из основного календаря, — иначе Google его не найдёт."
                    ),
                },
                "title": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "reminder_minutes": {"type": "integer", "description": "За сколько минут напомнить (уведомление Google)."},
                "add_attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "E-mail новых участников — им уйдут приглашения.",
                },
            },
            "required": ["event_id"],
        },
        handler=_calendar_update_event,
        requires_confirmation=True,
        preview=_preview_update,
        activity="Изменяю событие в календаре",
    )
)

registry.register(
    ToolSpec(
        name="calendar_delete_event",
        description=(
            "Удаляет (отменяет) событие из календаря. event_id бери из calendar_list_events. "
            "Необратимое действие, выполняется только после подтверждения пользователя."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Идентификатор события."},
                "calendar_id": {
                    "type": "string",
                    "description": (
                        "calendar_id события из ответа calendar_list_events. Обязательно передай "
                        "его, если событие не из основного календаря, — иначе Google его не найдёт."
                    ),
                },
            },
            "required": ["event_id"],
        },
        handler=_calendar_delete_event,
        requires_confirmation=True,
        preview=_preview_delete,
        activity="Удаляю событие из календаря",
    )
)
