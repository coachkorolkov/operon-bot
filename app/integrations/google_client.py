"""Подключение к Google API от имени пользователя.

Агент никогда не использует сервисный аккаунт с расширенными правами: он
работает по OAuth-токену конкретного пользователя, поэтому видит ровно те
документы и календари, к которым у пользователя есть доступ.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any

from ..config import settings
from ..errors import IntegrationUnavailable
from ..net import configure_requests_session, httplib2_http
from . import accounts, google_sa, token_store

try:  # Google-библиотеки опциональны: без них агент запускается, но интеграции выключены.
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials
    import google_auth_httplib2
    import requests
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    GOOGLE_LIBS_AVAILABLE = True
    GOOGLE_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover — зависит от окружения
    GOOGLE_LIBS_AVAILABLE = False
    GOOGLE_IMPORT_ERROR = str(exc)
    AuthorizedSession = Request = Credentials = build = None  # type: ignore[assignment]

    class HttpError(Exception):  # type: ignore[no-redef]
        """Заглушка, чтобы except-блоки в инструментах оставались валидными."""

        resp: Any = None


_lock = threading.Lock()
# Кэш учётных данных — у каждого пользователя свой (integrations/accounts.py).
_cached: dict[str, Any] = {}
# Адрес учётной записи — тоже по пользователю. Без кэша каждый ответ бота и
# каждая минутная проверка встреч стоили бы лишнего запроса к Диску.
_emails: dict[str, str] = {}

SETUP_HINT = (
    "Google для этого пользователя не подключён. Отправьте боту /auth и разрешите "
    "доступ своей учётной записью Google — у каждого пользователя свой доступ, "
    "чужой не подставляется."
)


def _load_credentials() -> Any:
    key = accounts.resolve()

    if not GOOGLE_LIBS_AVAILABLE:
        raise IntegrationUnavailable(
            "Библиотеки Google API не установлены "
            f"({GOOGLE_IMPORT_ERROR}). Установите зависимости: pip install -r requirements.txt"
        )

    # Свой вход человека (OAuth) важнее общего доступа через сервисный аккаунт.
    if google_sa.enabled() and not token_store.token_exists():
        return _service_account_credentials(key)

    with _lock:
        cached = _cached.get(key)
        if cached is not None and cached.valid:
            return cached

        try:
            payload = token_store.load_token()
        except token_store.TokenDecryptionError as exc:
            raise IntegrationUnavailable(str(exc)) from exc
        if payload is None:
            raise IntegrationUnavailable(SETUP_HINT)

        try:
            info = json.loads(payload)
            # Токен обновляется с теми правами, что были выданы при входе. Попросить
            # при обновлении право, которого в токене нет (набор расширили позже), —
            # получить invalid_scope и потерять доступ; новое право даст новый /auth.
            granted = info.get("scopes") if isinstance(info, dict) else None
            creds = Credentials.from_authorized_user_info(
                info, list(granted) if granted else list(settings.google_scopes)
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise IntegrationUnavailable(
                f"Файл токена Google повреждён ({exc}). Отправьте боту /auth и разрешите "
                "доступ заново."
            ) from exc

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request(session=configure_requests_session(requests.Session())))
                    token_store.save_token(creds.to_json())
                except Exception as exc:  # noqa: BLE001
                    raise IntegrationUnavailable(refresh_error_message(exc)) from exc
            else:
                raise IntegrationUnavailable(
                    "Токен Google недействителен и не может быть обновлён. "
                    "Отправьте боту /auth и разрешите доступ заново."
                )

        _cached[key] = creds
        return creds


SA_HINT = (
    "Google для этого пользователя не подключён. Отправьте боту /auth — он подскажет, "
    "как открыть доступ (личный Gmail) или подключит аккаунт организации сам."
)


def _service_account_credentials(key: str) -> Any:
    link = google_sa.current_link()
    if link is None:
        raise IntegrationUnavailable(SA_HINT)
    cache_key = f"sa:{key}:{link['email']}:{link.get('mode')}"
    with _lock:
        cached = _cached.get(cache_key)
        if cached is None:
            # Токен сервисного аккаунта обновляется сам при первом запросе.
            cached = google_sa.credentials(link)
            _cached[cache_key] = cached
        return cached


def mode() -> str:
    """oauth — свой токен; delegated / shared — сервисный аккаунт; пусто — не подключено."""
    if token_store.token_exists():
        return "oauth"
    if google_sa.enabled():
        link = google_sa.current_link()
        return str(link.get("mode")) if link else ""
    return ""


def default_calendar() -> str:
    """Чей календарь по умолчанию. У сервисного аккаунта «primary» — его собственный, пустой."""
    if mode() == "shared":
        link = google_sa.current_link() or {}
        return link.get("email") or "primary"
    return "primary"


def can_invite() -> bool:
    """Рассылать приглашения участникам. Сервисный аккаунт без делегирования не может."""
    return mode() != "shared"


def calendar_owner() -> str:
    """Почта владельца календаря — от чьего имени подписывать письмо участникам.

    Приглашение должно прийти от человека, которого участники знают, а не от
    адреса бота. Пусто — если почта владельца неизвестна.
    """
    if mode() == "shared":
        link = google_sa.current_link() or {}
        return str(link.get("email") or "")
    if mode() == "oauth":
        try:
            payload = token_store.load_token()
            info = json.loads(payload) if payload else {}
            return str(info.get("email") or info.get("account") or "")
        except Exception:
            return ""
    return ""


def refresh_error_message(exc: Exception) -> str:
    """Почему не обновился токен — и что делать. Сеть и отозванный доступ лечатся по-разному."""
    text = str(exc)
    if "invalid_grant" in text or "expired or revoked" in text:
        return (
            "Google отозвал доступ бота (invalid_grant): токен истёк или отозван. Отправьте "
            "боту /auth и разрешите доступ заново. Если это повторяется каждую неделю — "
            "приложение в Google Cloud в режиме «Testing»: там токены живут 7 дней. "
            "Переведите его в «In production» (Google Auth Platform → Audience → Publish app)."
        )
    if "invalid_client" in text or "unauthorized_client" in text:
        return (
            "Google не принял OAuth-клиент (invalid_client): в .env другие GOOGLE_CLIENT_ID / "
            "GOOGLE_CLIENT_SECRET, чем при авторизации. Верните прежние или пройдите /auth заново."
        )
    return (
        f"Не удалось связаться с Google для обновления доступа ({exc.__class__.__name__}: {text[:200]}). "
        "Это сеть, а не права: повторите чуть позже; проверьте интернет и OPERON_PROXY."
    )


def reset_cache() -> None:
    with _lock:
        _cached.clear()
        _emails.clear()


def get_service(api: str, version: str) -> Any:
    creds = _load_credentials()
    # Свой httplib2: сертификаты хранилища системы и настройка OPERON_PROXY,
    # как у остальных запросов бота.
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2_http())
    return build(api, version, http=http, cache_discovery=False)


def authorized_session() -> Any:
    return configure_requests_session(AuthorizedSession(_load_credentials()))


def describe_http_error(exc: Any, context: str) -> str:
    """Переводит ошибку Google API в понятное объяснение для модели и пользователя."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    detail = ""
    content = getattr(exc, "content", None)
    if content:
        try:
            payload = json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
            detail = payload.get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            detail = ""
    disabled = api_disabled_message(detail)
    if disabled:
        return f"{context}: {disabled}"
    if status == 401:
        return f"{context}: Google отклонил токен (401). Требуется повторная авторизация. {detail}"
    if status == 403:
        return (
            f"{context}: доступ запрещён (403). У текущей учётной записи нет прав на этот "
            f"объект, либо не выданы нужные OAuth-разрешения. {detail}"
        )
    if status == 404:
        return f"{context}: объект не найден (404) или недоступен этой учётной записи. {detail}"
    return f"{context}: ошибка Google API {status or ''}. {detail or exc}".strip()


_ACTIVATION_URL = re.compile(r"https://console\.(?:developers|cloud)\.google\.com/\S+")
_API_NAME = re.compile(r"([A-Za-z ]+ API) has not been used|([a-z]+)\.googleapis\.com")


def api_disabled_message(detail: str) -> str:
    """Отказ «API не включён в проекте» — самая частая ошибка первого запуска.

    Google отвечает на него 403, и без этой проверки человек видит «нет прав»
    и идёт перевыдавать доступ, хотя нужно нажать «Enable» в консоли.
    """
    text = detail or ""
    if "has not been used in project" not in text and "it is disabled" not in text:
        return ""
    name_match = _API_NAME.search(text)
    name = ""
    if name_match:
        name = name_match.group(1) or f"{name_match.group(2)}.googleapis.com"
    url_match = _ACTIVATION_URL.search(text)
    url = url_match.group(0).rstrip(".,)") if url_match else ""
    return (
        f"в проекте Google Cloud не включён {name or 'нужный API'}. "
        "Это настройка проекта, а не прав пользователя: включите API в консоли"
        + (f" ({url})" if url else " (APIs & Services → Library)")
        + ", подождите пару минут и повторите. Повторная авторизация не нужна."
    )


def status() -> dict[str, Any]:
    """Состояние интеграции — показывается в интерфейсе."""
    if not GOOGLE_LIBS_AVAILABLE:
        return {
            "connected": False,
            "reason": "Библиотеки Google API не установлены",
            "hint": "pip install -r requirements.txt",
        }
    if google_sa.enabled() and not token_store.token_exists():
        link = google_sa.current_link()
        if link is None:
            return {"connected": False, "reason": "почта Google не подключена (сервисный аккаунт)",
                    "hint": SA_HINT, "service_account": google_sa.email()}
        return {
            "connected": True,
            "mode": link.get("mode"),
            "scopes": list(google_sa.SCOPES),
            "account_hint": link["email"],
            "service_account": google_sa.email(),
        }
    if not token_store.token_exists():
        return {
            "connected": False,
            "reason": "Нет OAuth-токена пользователя",
            "hint": SETUP_HINT,
            "client_secret_present": settings.google_client_secret_path.exists(),
        }
    try:
        creds = _load_credentials()
    except IntegrationUnavailable as exc:
        return {"connected": False, "reason": str(exc), "hint": SETUP_HINT}
    return {
        "connected": True,
        "scopes": list(getattr(creds, "scopes", []) or settings.google_scopes),
        "account_hint": _account_email(),
        "token": token_store.describe(),
    }


def _account_email() -> str:
    # Адрес берём у Диска: userinfo требует отдельного разрешения (email),
    # которого в наборе нет, и раньше этот запрос молча возвращал пустоту.
    key = accounts.resolve()
    with _lock:
        if key in _emails:
            return _emails[key]
    try:
        about = get_service("drive", "v3").about().get(fields="user(emailAddress)").execute()
        email = (about.get("user") or {}).get("emailAddress", "")
    except Exception:  # noqa: BLE001 — необязательная информация
        return ""
    if email:
        with _lock:
            _emails[key] = email
    return email
