"""Проверка всей системы одной командой.

    python -m app.selfcheck

Отвечает на единственный вопрос: заработает ли ассистент прямо сейчас — и
если нет, что именно чинить. Проверяются настройки, шлюз модели, Telegram,
Google, интернет-поиск и база знаний.

Зачем отдельно от тестов: тесты доказывают, что код делает задуманное, но
ничего не говорят о том, приняли ли ключ, дошли ли до сети и выдал ли Google
нужные разрешения. Всё это выясняется только живым обращением.

Ничего не меняет: только читает. Запускать можно и локально, и на сервере.
"""

from __future__ import annotations

import os

from dataclasses import dataclass, field
from typing import Any

import httpx

from . import console
from .search_keys import GOOGLE_CSE_SHUTDOWN, SEARCH_KEY_VARIABLES  # noqa: F401
from .search_keys import damaged_keys as _damaged_keys
from .config import settings
from .model_choice import current_model
from .net import http_options

TIMEOUT = 25.0

OK = "OK"
WARN = "ВНИМАНИЕ"
FAIL = "СБОЙ"
SKIP = "пропуск"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hints: list[str] = field(default_factory=list)
    # Без этого ассистент не работает вообще, а не «работает хуже».
    blocking: bool = False


def _http(url: str, **kwargs: Any) -> httpx.Response:
    return httpx.get(url, timeout=TIMEOUT, **http_options(), **kwargs)


# --- настройки --------------------------------------------------------------


def check_settings() -> list[Check]:
    checks: list[Check] = []

    if settings.api_key:
        checks.append(Check("Ключ доступа к модели", OK, f"провайдер {settings.provider}"))
    else:
        checks.append(
            Check(
                "Ключ доступа к модели",
                FAIL,
                "не задан",
                ["Задайте ROUTERAI_API_KEY (или OPERON_LLM_API_KEY)."],
                blocking=True,
            )
        )

    from .net import describe_proxy, proxy_setting

    via = describe_proxy()
    if via:
        checks.append(Check(
            "Прокси", OK, via,
            ["Все запросы бота идут через этот прокси. Если ниже таймауты "
             "(«handshake operation timed out») — прокси или VPN нестабилен: "
             "проверьте его или пустите бота напрямую строкой OPERON_PROXY=none в .env."],
        ))
    elif proxy_setting().lower() in {"none", "off", "direct", "нет"}:
        checks.append(Check("Прокси", OK, "не используется (OPERON_PROXY=none)"))

    if current_model(settings):
        chosen = current_model(settings)
        note = "" if chosen == settings.model else " (выбрана в Mini App)"
        checks.append(Check("Имя модели", OK, chosen + note))
    else:
        checks.append(
            Check(
                "Имя модели",
                FAIL,
                "не задано",
                ["Задайте OPERON_MODEL. Список моделей покажет: python -m app.probe"],
                blocking=True,
            )
        )

    if settings.auth_required:
        checks.append(Check("Пароль на веб-чат", OK, "задан"))
    else:
        checks.append(
            Check(
                "Пароль на веб-чат",
                WARN,
                "не задан",
                [
                    "Локально это допустимо. На сервере приложение откажется "
                    "слушать внешний адрес: OPERON_ACCESS_PASSWORD обязателен."
                ],
            )
        )

    # Подмена часового пояса на UTC происходит молча, а стоит трёх часов
    # в сроках поручений и в часе утренней сводки.
    requested = settings.timezone_name
    actual = str(settings.tz)
    if requested and actual != requested:
        checks.append(
            Check(
                "Часовой пояс",
                FAIL,
                f"запрошен {requested}, используется {actual}",
                [
                    "Нет базы часовых поясов IANA — на Windows её не бывает "
                    "в системе.",
                    "Установите: pip install tzdata (она уже в requirements.txt).",
                    "Иначе сроки, «сегодня» и час сводки сместятся.",
                ],
            )
        )
    else:
        checks.append(Check("Часовой пояс", OK, actual))

    from .integrations import token_store

    if token_store.encryption_enabled():
        checks.append(Check("Шифрование токена Google", OK, "OPERON_TOKEN_KEY задан"))
    else:
        checks.append(
            Check(
                "Шифрование токена Google",
                WARN,
                "OPERON_TOKEN_KEY не задан",
                ["На сервере задайте: доступ к диску иначе равен доступу к Google."],
            )
        )
    return checks


# --- модель -----------------------------------------------------------------


def check_model() -> list[Check]:
    if not (settings.api_key and current_model(settings)):
        return [Check("Шлюз модели", SKIP, "нет ключа или имени модели")]

    from .probe import _try_anthropic, _try_openai

    protocol = settings.llm_protocol
    base = settings.base_url or "https://api.anthropic.com/v1"
    attempt = _try_openai if protocol == "openai" else _try_anthropic

    try:
        reachable, tools_work, note = attempt(base, settings.api_key, current_model(settings))
    except Exception as exc:  # noqa: BLE001 — сюда попадают и сетевые сбои
        return [
            Check(
                "Шлюз модели",
                FAIL,
                f"{exc.__class__.__name__}: {exc}",
                [f"Проверьте доступность {base} с этой машины."],
                blocking=True,
            )
        ]

    checks = []
    if reachable:
        checks.append(Check("Шлюз модели отвечает", OK, f"{base} ({protocol})"))
    else:
        return [
            Check(
                "Шлюз модели отвечает",
                FAIL,
                note,
                [
                    "Проверьте ключ и имя модели.",
                    "Каталог моделей покажет: python -m app.probe",
                ],
                blocking=True,
            )
        ]

    if tools_work:
        checks.append(Check("Модель вызывает инструменты", OK, "проверено живым вызовом"))
    else:
        checks.append(
            Check(
                "Модель вызывает инструменты",
                FAIL,
                note or "модель не вызвала инструмент",
                [
                    "Без вызова инструментов ассистент бесполезен: он не сможет "
                    "ни искать в базе знаний, ни спрашивать подтверждение.",
                    "Выберите другую модель из каталога провайдера.",
                ],
                blocking=True,
            )
        )
    return checks


# --- Telegram ---------------------------------------------------------------


def _webhook_check() -> list[Check]:
    """Webhook от прежней платформы забирает сообщения себе — бот молчит."""
    try:
        info = _http(f"https://api.telegram.org/bot{settings.telegram_token}/getWebhookInfo").json()
    except Exception:  # noqa: BLE001 — проверка вспомогательная
        return []
    result = info.get("result") or {}
    url = result.get("url") or ""
    if not url:
        return [Check("Telegram: webhook", OK, "не установлен — сообщения получает этот бот")]
    detail = f"установлен: {url}; ждут доставки: {result.get('pending_update_count', 0)}"
    if result.get("last_error_message"):
        detail += f"; последняя ошибка: {result['last_error_message']}"
    return [Check(
        "Telegram: webhook", WARN, detail,
        ["Пока он стоит, сообщения уходят на этот адрес, а не нашему боту. "
         "Бот снимет его сам при запуске (python run.py или start-windows.ps1)."],
    )]


def check_telegram() -> list[Check]:
    if not settings.telegram_token:
        return [Check("Telegram", SKIP, "TELEGRAM_BOT_TOKEN не задан — работает только веб")]

    checks: list[Check] = []
    try:
        response = _http(f"https://api.telegram.org/bot{settings.telegram_token}/getMe")
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return [
            Check(
                "Telegram: токен",
                FAIL,
                f"{exc.__class__.__name__}: {exc}",
                ["Проверьте доступность api.telegram.org с этой машины."],
            )
        ]

    if payload.get("ok"):
        bot = payload.get("result", {})
        checks.append(Check("Telegram: токен", OK, f"@{bot.get('username', '?')}"))
        checks.extend(_webhook_check())
    else:
        return [
            Check(
                "Telegram: токен",
                FAIL,
                payload.get("description", "отклонён"),
                ["Перевыпустите токен у @BotFather: /mybots → бот → API Token."],
            )
        ]

    from .config import env_source, parse_telegram_ids

    allowed = settings.telegram_allowed_users
    _ids, rejected = parse_telegram_ids(os.getenv("TELEGRAM_ALLOWED_USERS", ""))
    if allowed:
        source = env_source("TELEGRAM_ALLOWED_USERS")
        hints = [f"Взято из: {source}."]
        if source.startswith("файл"):
            hints.append(
                "В панели Amvera переменной TELEGRAM_ALLOWED_USERS нет — правка в панели не "
                "дойдёт, пока её там не создать (панель сильнее файла)."
            )
        from .telegram import access

        added = sorted(access.extra())
        detail = f"{len(allowed)} чел.: {sorted(allowed)}"
        if added:
            detail += f" + добавлены командой /adduser: {added}"
        checks.append(Check("Telegram: белый список", OK, detail, hints))
        if rejected:
            checks.append(Check(
                "Telegram: белый список", WARN,
                "пропущено, не похоже на ID: " + ", ".join(f"«{r}»" for r in rejected[:5]),
                ["ID — только цифры, через запятую. Узнать ID — @userinfobot."],
            ))
    else:
        checks.append(
            Check(
                "Telegram: белый список",
                FAIL,
                "TELEGRAM_ALLOWED_USERS не задан",
                [
                    "Без белого списка бот НЕ ЗАПУСТИТСЯ — это намеренно: он "
                    "работает с вашим Google-аккаунтом.",
                    "Свой ID узнайте у @userinfobot.",
                ],
            )
        )
    return checks


# --- Google -----------------------------------------------------------------


def check_google() -> list[Check]:
    from .integrations import google_client, google_oauth, google_sa

    from .integrations import token_store

    prefix: list[Check] = []
    if google_oauth.configured():
        ok, problem = google_oauth.preflight()
        prefix.append(Check(
            "Google: вход через Google", OK if ok else FAIL,
            "Google примет вход — кнопка «Войти через Google» в /auth" if ok else problem,
        ))
    if google_sa.problem():
        return prefix + [Check("Google: сервисный аккаунт", FAIL, google_sa.problem())]
    if google_sa.enabled() and not token_store.token_exists():
        return prefix + _check_google_service_account()

    checks: list[Check] = list(prefix)
    client = google_oauth.describe_client()

    if client["configured"]:
        client_id = settings.google_client_id
        which = f" · клиент {client_id.split('-')[0]}-…" if client_id else ""
        checks.append(Check("Google: OAuth-клиент", OK, client["source"] + which))

        # Для Desktop-клиента петлевой адрес — это норма, а не недоработка.
        # Советовать здесь «переключитесь на публичный адрес» значило бы
        # отправить человека чинить то, что уже верно.
        declared = settings.google_client_type
        if client["callback_mode"]:
            detail = client["redirect_uri"] + " (код придёт на сервер сам)"
            checks.append(Check("Google: адрес возврата", OK, detail))
            if declared == "desktop":
                checks.append(
                    Check(
                        "Google: тип клиента",
                        FAIL,
                        "заявлен desktop, но адрес возврата публичный",
                        ["Desktop-клиент такой адрес не примет. Уберите "
                         "OPERON_OAUTH_REDIRECT_URI либо смените тип клиента."],
                    )
                )
        elif declared == "desktop":
            checks.append(
                Check(
                    "Google: адрес возврата",
                    OK,
                    client["redirect_uri"] + " (заявлен клиент Desktop)",
                    # Тип клиента Google отсюда не видно — только то, что заявлено.
                    ["Если Google ответит redirect_uri_mismatch — клиент на самом деле «Web application»: "
                     f"добавьте {client['redirect_uri']} в его Authorized redirect URIs "
                     "(Google Cloud → APIs & Services → Credentials)."],
                )
            )
        else:
            checks.append(
                Check(
                    "Google: адрес возврата",
                    WARN,
                    client["redirect_uri"],
                    [
                        "Код придётся копировать из строки браузера вручную.",
                        "Если клиент типа Desktop — так и задумано, укажите "
                        "OPERON_GOOGLE_CLIENT_TYPE=desktop, и это перестанет "
                        "быть замечанием.",
                        "Если клиент типа Web — задайте OPERON_PUBLIC_URL, "
                        "код придёт на сервер сам.",
                    ],
                )
            )
    else:
        checks.append(
            Check(
                "Google: OAuth-клиент",
                WARN,
                "не настроен",
                [
                    "Задайте GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET, либо "
                    "положите client_secret.json.",
                    "Без этого Диск и календарь недоступны; остальное работает.",
                ],
            )
        )
        return checks

    status = google_client.status()
    if not status.get("connected"):
        checks.append(
            Check(
                "Google: доступ выдан",
                WARN,
                str(status.get("reason", "токен не найден")),
                ["Пройдите авторизацию: команда /auth в боте."],
            )
        )
        return checks

    checks.append(
        Check("Google: доступ выдан", OK, status.get("account_hint") or "учётная запись определена")
    )
    checks.append(_invites_check())

    checks.extend(_google_live_checks())
    return checks


def _invites_check() -> Check:
    """Уйдут ли участникам приглашения — заранее, а не после создания встречи.

    Сервисный аккаунт без делегирования приглашения рассылать не может: для
    Google он не тот, кто организует встречу. Человек узнавал об этом уже
    постфактум («письма не пришли»), поэтому говорим заранее и в /check.
    """
    from .integrations import google_client

    if google_client.can_invite():
        return Check("Google: приглашения участникам", OK, "уходят от вашего имени")
    return Check(
        "Google: приглашения участникам",
        WARN,
        "НЕ отправляются (календарь подключён через общий доступ)",
        [
            "Google не даёт сервисному аккаунту рассылать приглашения: он не организатор "
            "встречи. Участники записываются в описание события.",
            "Чтобы письма уходили: подключите Google через /auth (вход своим аккаунтом) "
            "или настройте делегирование в Google Workspace.",
        ],
    )


SCOPE_PURPOSE = {
    "https://www.googleapis.com/auth/drive.readonly": "чтение Диска (документы, таблицы, презентации)",
    "https://www.googleapis.com/auth/drive.file": "создание файлов на Диске (протоколы, КП)",
    "https://www.googleapis.com/auth/calendar.events": "календарь: чтение и создание событий",
    "https://www.googleapis.com/auth/calendar.readonly": "календарь: только чтение",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly": "список календарей (рабочие, общие)",
}


def _check_google_service_account() -> list[Check]:
    """Вход через сервисный аккаунт: ключ, подключённая почта этого человека, живые запросы."""
    from .integrations import google_client, google_sa

    checks = [Check("Google: сервисный аккаунт", OK, google_sa.email())]
    link = google_sa.current_link()
    if link is None:
        checks.append(Check(
            "Google: ваша почта", WARN, "не подключена",
            ["Отправьте боту /auth ваша@почта — бот проверит доступ и подключит."],
        ))
        return checks
    checks.append(Check("Google: ваша почта", OK, google_sa.describe_link(link)))
    if link.get("mode") == "shared" and link.get("access") == "reader":
        checks.append(Check(
            "Google: права на календарь", WARN, "только чтение — встречи создавать нельзя",
            ["Календарь → Настройки и общий доступ → право «Вносить изменения в мероприятия»."],
        ))
    google_client.reset_cache()
    checks.append(_invites_check())
    checks.extend(_google_live_checks())
    return checks


def _granted_scopes() -> set[str] | None:
    """Разрешения, которые Google выдал на самом деле, — по tokeninfo.

    Список в самом токене показывает, что запрашивали, а не что выдали: на
    экране согласия галочку можно снять, и тогда запись молча не работает.
    """
    from .integrations import google_client

    try:
        creds = google_client._load_credentials()
        response = httpx.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"access_token": creds.token},
            timeout=15,
            **http_options(),
        )
        if response.status_code != 200:
            return None
        return set((response.json().get("scope") or "").split())
    except Exception:  # noqa: BLE001 — проверка вспомогательная
        return None


def _google_live_checks() -> list[Check]:
    """Живые запросы: выданный токен ещё не значит, что права те, что нужны."""
    from .integrations import google_client
    from .tools import registry

    checks: list[Check] = []

    granted = _granted_scopes()
    if granted is not None:
        missing = [scope for scope in settings.google_scopes if scope not in granted]
        if missing:
            checks.append(
                Check(
                    "Google: разрешения",
                    FAIL,
                    "не выданы: " + "; ".join(SCOPE_PURPOSE.get(m, m) for m in missing),
                    [
                        "На экране согласия Google сняли галочку, либо токен выдан "
                        "под старый набор разрешений.",
                        "Отправьте боту /auth и на экране согласия отметьте ВСЕ пункты.",
                    ],
                )
            )
        else:
            checks.append(
                Check(
                    "Google: разрешения",
                    OK,
                    "; ".join(SCOPE_PURPOSE.get(s, s) for s in settings.google_scopes),
                )
            )

    for name, tool, tool_input, api in (
        ("Google Drive: чтение", "drive_search", {"query": "", "max_results": 1}, "Google Drive API"),
        ("Google Calendar: чтение", "calendar_list_events", {"max_results": 1}, "Google Calendar API"),
    ):
        try:
            content, is_error = registry.execute(tool, tool_input)
        except Exception as exc:  # noqa: BLE001
            content, is_error = f"{exc.__class__.__name__}: {exc}", True
        if not is_error:
            checks.append(Check(name, OK, "запрос прошёл"))
            continue
        hints = []
        if "не включён" in content:
            hints.append(
                f"Google Cloud Console → APIs & Services → Library → {api} → Enable."
            )
        checks.append(Check(name, FAIL, content[:300], hints))

    # Sheets API: несуществующая таблица даёт 404, если API включён, и 403
    # «has not been used», если нет. Так проверяем, не зная ни одного файла.
    try:
        from googleapiclient.errors import HttpError

        try:
            google_client.get_service("sheets", "v4").spreadsheets().get(
                spreadsheetId="operon-selfcheck-missing", fields="spreadsheetId"
            ).execute()
            checks.append(Check("Google Sheets API", OK, "включён"))
        except HttpError as exc:
            status_code = getattr(getattr(exc, "resp", None), "status", None)
            text = google_client.describe_http_error(exc, "Sheets API")
            if status_code in (400, 404):
                checks.append(Check("Google Sheets API", OK, "включён"))
            elif "не включён" in text:
                checks.append(
                    Check(
                        "Google Sheets API",
                        WARN,
                        "не включён — таблицы читаются запасным путём, целиком",
                        [
                            "Google Cloud Console → APIs & Services → Library → "
                            "Google Sheets API → Enable. Тогда можно читать "
                            "отдельный лист или диапазон.",
                        ],
                    )
                )
            else:
                checks.append(Check("Google Sheets API", WARN, text[:200]))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("Google Sheets API", WARN, f"{exc.__class__.__name__}: {exc}"[:200]))

    return checks


# --- баланс шлюза -------------------------------------------------------------


def check_billing() -> list[Check]:
    """То, что покажет вкладка «Настройки» в Mini App: модель, баланс, подключения."""
    from . import routerai

    if not routerai.available():
        return []
    data = routerai.billing()
    unit = data.get("currency", "")
    checks: list[Check] = []
    errors = data.get("errors") or []
    if "balance" in data:
        balance = data["balance"]
        source = f" ({data['balance_source']})" if data.get("balance_source") else ""
        detail = f"{balance:.2f} {unit}{source}"
        hints = [f"Не ответило: {e}" for e in errors]
        if balance <= 0:
            # Ноль или минус — следующий запрос к модели шлюз, скорее всего,
            # отклонит (обычно 402). Это не замечание, а причина будущего отказа.
            checks.append(
                Check(
                    "Баланс",
                    FAIL,
                    detail + (" — лимит ключа исчерпан" if data.get("balance_source") else " — средства исчерпаны"),
                    [
                        "Поднимите или снимите лимит расхода у ключа в кабинете RouterAI."
                        if data.get("balance_source")
                        else "Пополните баланс в кабинете RouterAI.",
                        *hints,
                    ],
                )
            )
        else:
            checks.append(Check("Баланс", OK, detail, hints))
    else:
        spent = data.get("key_usage")
        usage = f"; расход по ключу: {spent:.2f} {unit}" if spent is not None else ""
        checks.append(
            Check(
                "Баланс",
                WARN if errors else OK,
                "шлюз не сообщил остаток" + usage,
                [f"Не ответило: {e}" for e in errors]
                + (["Сырые ответы шлюза: команда /routerai в боте (или python -m app.routerai)."] if errors else []),
            )
        )
    try:
        models = routerai.list_models()
        with_tools = sum(1 for m in models if m["tools"] is not False)
        checks.append(Check("Каталог моделей", OK, f"{len(models)} моделей, с инструментами: {with_tools}"))
    except routerai.BillingError as exc:
        checks.append(Check("Каталог моделей", WARN, str(exc)))
    return checks


# --- интернет ---------------------------------------------------------------




PROVIDER_LABEL = {
    "free": "бесплатный поиск",
    "tavily": "Tavily",
    "brave": "Brave",
    "serper": "Serper",
    "google": "Google CSE",
    "": "никакой (интернет выключен)",
}


def check_search() -> list[Check]:
    """Живой поиск плюс предупреждение о лишних испорченных ключах.

    Испорченная строка, оставшаяся от старой настройки, поиск не ломает:
    её поставщик просто не выбирается. Поэтому это замечание, а не сбой.
    """
    checks = _search_live_check()
    damaged = _damaged_keys()
    already_named = any("испорчен" in c.detail or "пробел" in c.detail for c in checks)
    if damaged and not already_named:
        from .tools.web import _search_provider

        active = PROVIDER_LABEL.get(_search_provider(), _search_provider())
        hints = [
            f"Эти строки не используются — поиск идёт через: {active}. "
            "Удалите их из .env или вставьте ключ заново (перевыпускать не нужно: "
            "значение испортилось при вставке).",
            "Проверить строку: Select-String -Path .env -Pattern KEY",
        ]
        if any(name.startswith("GOOGLE_CSE") for name, _ in damaged):
            hints.append(GOOGLE_CSE_SHUTDOWN)
        checks.append(
            Check("Ключи поиска", WARN, "; ".join(f"{name}: {why}" for name, why in damaged), hints)
        )
    return checks


def _search_live_check() -> list[Check]:
    from .tools import registry
    import json as _json

    if "internet_search" not in registry.names():
        return [
            Check(
                "Интернет-поиск",
                OK,
                "используется серверный поиск Anthropic",
            )
        ]

    content, is_error = registry.execute("internet_search", {"query": "ставка ЦБ РФ"})
    if is_error:
        hints = []
        if "Бесплатный интернет-поиск" in content:
            hints.append("Сеть сервера не достучалась ни до одного бесплатного источника. "
                         "Повторите /check позже; при постоянном сбое можно задать TAVILY_API_KEY "
                         "(бесплатный тариф).")
        if "испорчен" in content or "пробел" in content:
            hints.append("Значение испорчено при вставке, а не сервисом. "
                         "Перевыпускать ключ не нужно — вставьте его заново.")
        if "сервис google" in content or "GOOGLE_CSE" in content:
            hints.append(GOOGLE_CSE_SHUTDOWN)
        return [Check("Интернет-поиск", FAIL, content[:200], hints)]

    payload = _json.loads(content)
    status = payload.get("status")
    if status == "not_configured":
        return [
            Check(
                "Интернет-поиск",
                WARN,
                "выключен (OPERON_SEARCH_PROVIDER=none)",
                [
                    "Ассистент честно скажет «интернет недоступен» и ответит "
                    "только по внутренним данным — но пункт ТЗ про внешние "
                    "источники работать не будет.",
                    "Уберите OPERON_SEARCH_PROVIDER — включится бесплатный поиск "
                    "без ключей (DuckDuckGo, Bing, Google News, Википедия).",
                ],
            )
        ]
    if status == "ok":
        provider = payload.get("provider", "?")
        found = f"{provider}: найдено {payload.get('results_count', len(payload.get('results', [])))}"
        if provider == "google":
            return [Check("Интернет-поиск", WARN, found, [GOOGLE_CSE_SHUTDOWN])]
        if provider == "free":
            engines = sorted({r.get("engine", "?") for r in payload.get("results", [])})
            failed = payload.get("sources_failed") or []
            detail = f"бесплатный, без ключей: найдено {len(payload.get('results', []))} ({', '.join(engines)})"
            if failed:
                return [Check("Интернет-поиск", OK, detail, ["Не ответили (есть замена): " + "; ".join(failed)])]
            return [Check("Интернет-поиск", OK, detail)]
        return [Check("Интернет-поиск", OK, found)]
    return [Check("Интернет-поиск", WARN, str(payload.get("hint", status))[:150])]


# --- база знаний ------------------------------------------------------------


def check_knowledge_base() -> list[Check]:
    from .kb import knowledge_base

    stats = knowledge_base.stats
    count = stats.get("documents", 0)
    if count:
        return [
            Check(
                "База знаний",
                OK,
                f"{count} документов в {stats.get('root')}"
                + (f", разделы: {', '.join(stats['categories'][:5])}" if stats.get("categories") else ""),
            )
        ]
    from . import quant

    if quant.config().enabled:
        return [Check(
            "База знаний",
            OK,
            "документов, загруженных прямо в бота, нет — основная база в Кванте (ниже)",
        )]
    return [
        Check(
            "База знаний",
            WARN,
            f"пусто ({stats.get('root')})",
            [
                "Это не поломка: ассистент честно ответит «данных нет».",
                "Положите документы — читаются .md, .csv, .json, .yaml, "
                "а также .docx, .pdf, .xlsx, .pptx.",
            ],
        )
    ]


# --- вывод ------------------------------------------------------------------

MARK = {OK: "  OK  ", WARN: " ВНИМ ", FAIL: " СБОЙ ", SKIP: "  --  "}


def check_quant() -> list[Check]:
    from . import quant

    conf = quant.config()
    if not conf.enabled:
        return [Check("Квант", SKIP, "QDRANT_URL не задан")]
    checks: list[Check] = []
    try:
        quant.reset_cache()
        names = quant.collection_names(conf)
    except quant.QuantError as exc:
        return [Check("Квант: подключение", FAIL, str(exc), ["Проверьте QDRANT_URL и QDRANT_API_KEY."])]
    if not names:
        return [Check("Квант: коллекции", WARN, "подключено, но коллекций нет")]
    checks.append(Check("Квант: подключение", OK, f"{conf.url}, коллекции: {', '.join(names)}"))
    for name in names:
        try:
            info = quant.collection_info(name, conf)
        except quant.QuantError as exc:
            checks.append(Check(f"Квант: {name}", FAIL, str(exc)))
            continue
        model = conf.embedding_model or quant.KNOWN_SIZES.get(info.size, "")
        detail = f"записей {info.points}, вектор {info.size or '?'}"
        if not model:
            checks.append(Check(
                f"Квант: {name}", WARN, detail + "; поиск только по словам",
                ["Укажите QDRANT_EMBEDDING_MODEL — модель, которой наполняли базу: поиск станет по смыслу."],
            ))
            continue
        try:
            vector = quant.embed("проверка", conf, model)
        except quant.QuantError as exc:
            checks.append(Check(f"Квант: {name}", WARN, f"{detail}; {exc} — пока поиск по словам"))
            continue
        if len(vector) != info.size:
            checks.append(Check(
                f"Квант: {name}", FAIL,
                f"{detail}; модель {model} даёт {len(vector)} — не та модель",
                ["Укажите в QDRANT_EMBEDDING_MODEL модель, которой наполняли базу."],
            ))
        elif info.points != 0 and (match := _quant_match(quant, info, conf, model)) is not None \
                and match < quant.SAME_MODEL_COSINE and (detected := _quant_detect(quant, info, conf)):
            checks.append(Check(
                f"Квант: {name}", OK,
                f"{detail}; поиск по векторам Кванта ({detected}) — модель базы определена "
                f"автоматически, указанная {model} не подходит",
                [f"Чтобы не сверять при запуске: QDRANT_EMBEDDING_MODEL={detected}"],
            ))
        elif info.points != 0 and match is not None and match < quant.SAME_MODEL_COSINE:
            checks.append(Check(
                f"Квант: {name}", WARN,
                f"{detail}; база наполнена ДРУГОЙ моделью, не {model} (совпадение {match} из 1.0) — "
                f"бот ищет по смыслу по своему индексу текста ({model}), в Квант не пишет",
                ["Чтобы искать прямо по векторам Кванта, узнайте у загрузчика модель и задайте "
                 "QDRANT_EMBEDDING_MODEL. Проверить: python -m app.quant --models модель1,модель2"],
            ))
        elif info.points == 0:
            checks.append(Check(
                f"Квант: {name}", WARN, f"{detail}; поиск по смыслу ({model}) готов, но коллекция пуста",
                ["Документы в Квант ещё не загружены — искать боту нечего."],
            ))
        else:
            checks.append(Check(f"Квант: {name}", OK, f"{detail}; поиск по смыслу ({model})"))
    return checks


def check_quant_rights() -> list[Check]:
    """Права к документам Кванта: роль с сайта базы знаний, роли по умолчанию или без фильтра."""
    from . import quant, site_db
    from .telegram import access

    conf = quant.config()
    if not conf.enabled:
        return []
    if not site_db.enabled():
        if conf.roles:
            return [Check("Права в Кванте", OK, f"всем — роли из QDRANT_ROLES: {', '.join(conf.roles)}")]
        many = len(access.allowed()) > 1
        return [Check(
            "Права в Кванте", WARN if many else OK,
            "без фильтра по ролям: все пользователи бота видят все документы Кванта",
            ["Чтобы права назначались на сайте базы знаний, задайте OPERON_MONGODB_URI — адрес его "
             "MongoDB (на сервере Timeweb: mongodb://mongo:27017/operon)."],
        )]
    ok, detail = site_db.check()
    if not ok:
        return [Check(
            "Права в Кванте", FAIL, detail,
            ["Проверьте OPERON_MONGODB_URI. Пока база недоступна, документы Кванта показываются "
             "только по ролям из QDRANT_ROLES (если заданы)."],
        )]
    checks = [Check("База сайта (MongoDB)", OK, detail)]
    found = site_db.access()
    user = site_db.telegram_id()
    if found.state == "role":
        checks.append(Check("Права в Кванте", OK, f"ваша роль: {found.role_name or found.role_id}"))
    elif conf.roles and found.state != "blocked":
        checks.append(Check(
            "Права в Кванте", WARN, f"у вас (ID {user}) нет роли на сайте — роли по умолчанию из QDRANT_ROLES",
        ))
    else:
        checks.append(Check("Права в Кванте", WARN, site_db.explain(found, user)))
    return checks


def check_voice() -> list[Check]:
    """Распознавание голосовых: пробуем на секунде тишины — без ffmpeg и без записи."""
    from . import stt

    if not stt.available():
        return [Check("Распознавание голоса", SKIP, "нет ключа шлюза")]
    stt.reset()
    try:
        _text, model = stt.transcribe(stt.silent_wav(), "check.wav", "audio/wav")
    except stt.STTError as exc:
        return [Check("Распознавание голоса", WARN, str(exc)[:400],
                      ["Текст и документы работают. Голосовые заработают, когда шлюз примет модель распознавания."])]
    import shutil

    hints = [] if shutil.which("ffmpeg") or "whisper" in model or "transcribe" in model else [
        "Для голосовых Telegram (OGG) этим способом нужен ffmpeg — в Docker-образе он есть."
    ]
    return [Check("Распознавание голоса", OK, f"работает ({model})", hints)]


def _quant_detect(quant: Any, info: Any, conf: Any) -> str:
    try:
        return quant.matching_model(info, conf) or ""
    except quant.QuantError:
        return ""


def _quant_match(quant: Any, info: Any, conf: Any, model: str) -> float | None:
    try:
        return quant.model_match(info, conf, model)
    except quant.QuantError:
        return None


def run_groups() -> list[tuple[str, list[Check]]]:
    """Все проверки по группам. Упавшая проверка не лишает отчёта об остальных."""
    groups = [
        ("Настройки", check_settings),
        ("Модель", check_model),
        ("Баланс шлюза", check_billing),
        ("Telegram", check_telegram),
        ("Google", check_google),
        ("Интернет", check_search),
        ("База знаний", check_knowledge_base),
        ("Квант (Qdrant)", check_quant),
        ("Права в Кванте", check_quant_rights),
        ("Голосовые", check_voice),
    ]
    results = []
    for title, runner in groups:
        try:
            checks = runner()
        except Exception as exc:  # noqa: BLE001 — проверка не должна падать сама
            checks = [Check(title, FAIL, f"проверка сорвалась: {exc.__class__.__name__}: {exc}")]
        results.append((title, checks))
    return results


TELEGRAM_MARK = {OK: "✅", WARN: "⚠️", FAIL: "❌", SKIP: "➖"}


def render_telegram(results: list[tuple[str, list[Check]]]) -> str:
    """Отчёт для команды /check в боте: те же проверки, без компьютера."""
    from .telegram.format import escape

    lines = ["<b>Проверка ассистента</b>"]
    for title, checks in results:
        if not checks:
            continue
        lines.append(f"\n<b>{escape(title)}</b>")
        for check in checks:
            lines.append(f"{TELEGRAM_MARK.get(check.status, '•')} {escape(check.name)}: {escape(check.detail)}")
            for hint in check.hints:
                lines.append(f"    → {escape(hint)}")
    failures = [c for _, checks in results for c in checks if c.status == FAIL]
    lines.append("")
    lines.append("Сбоев нет." if not failures else f"Сбоев: {len(failures)} — см. строки с ❌.")
    return "\n".join(lines)


def main() -> int:
    # Русская консоль Windows не примет «—» и оборвёт вывод на первой же строке.
    marks = console.setup()
    rule = marks["rule"]
    arrow = marks["arrow"]

    print("Проверка ассистента OPERON\n")
    all_checks: list[Check] = []

    for title, checks in run_groups():
        print(f"{rule} {title} " + "-" * (58 - len(title)))
        for check in checks:
            print(f"[{MARK[check.status]}] {check.name}: {check.detail}")
            for hint in check.hints:
                print(f"           {arrow} {hint}")
        all_checks.extend(checks)
        print()

    blocking = [c for c in all_checks if c.status == FAIL and c.blocking]
    failures = [c for c in all_checks if c.status == FAIL]
    warnings = [c for c in all_checks if c.status == WARN]

    print("=" * 62)
    if blocking:
        print("НЕ ЗАПУСТИТСЯ. Сначала почините:")
        for check in blocking:
            print(f"  • {check.name}: {check.detail}")
        return 2
    if failures:
        print("Запустится, но часть функций не работает:")
        for check in failures:
            print(f"  • {check.name}: {check.detail}")
        return 1
    if warnings:
        print("Готов к работе. Замечания (не мешают запуску):")
        for check in warnings:
            print(f"  • {check.name}: {check.detail}")
        return 0
    print("Всё проверено, замечаний нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
