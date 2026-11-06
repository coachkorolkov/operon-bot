"""Авторизация Google без доступа к серверу: ссылка → согласие → токен.

Обычный сценарий google-auth поднимает локальный веб-сервер и открывает
браузер. На Amvera так нельзя: браузера в контейнере нет, а токен, полученный
на рабочем ноутбуке, пришлось бы переносить файлами при каждой переавторизации.

Поддерживаются оба типа OAuth-клиента, и различие между ними существенное:

* **Web application** — код возвращается на публичный адрес приложения
  (``/oauth2/callback``). Пользователь просто нажимает «Разрешить» и всё;
  копировать ничего не нужно. Адрес обязан быть заранее зарегистрирован в
  Google Cloud Console, иначе Google ответит ``redirect_uri_mismatch``.
* **Desktop** — redirect_uri остаётся непрослушанным localhost: браузер
  показывает ошибку соединения, а код виден в адресной строке. Пользователь
  копирует адрес целиком и отдаёт боту.

Ответ Google привязывается к одноразовому ``state``: обменять можно только код,
пришедший на ранее выданную ссылку.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ..config import settings
from ..net import configure_requests_session, explain
from . import accounts, google_client, token_store

logger = logging.getLogger(__name__)

# Коды Google начинаются с «4/» и состоят из URL-safe символов. Длину берём
# с запасом вниз: настоящий код длиннее сотни знаков, но занижать порог
# безопаснее, чем отвергнуть годный код из-за смены формата.
CODE_RE = re.compile(r"^[0-9A-Za-z._~/\-]{12,}$")


class OAuthError(Exception):
    """Ошибка, текст которой можно показать пользователю целиком."""


@dataclass
class _Pending:
    """Выданная ссылка авторизации, ждущая ответа Google."""

    created_at: float
    label: str
    account: str = ""
    # Секрет PKCE: Google принимает код только вместе с ним. Библиотека кладёт
    # его в тот объект Flow, что выдал ссылку, а обмен идёт в новом — поэтому
    # храним здесь. Без него обмен падает с invalid_grant.
    code_verifier: str = ""
    done: bool = False
    result: dict[str, Any] | None = None
    error: str = ""


# state -> выданная ссылка. Обменять можно только код с известным state:
# случайный запрос на /oauth2/callback ничего не подключит.
_pending: dict[str, _Pending] = {}
_lock = threading.Lock()


def _forget_stale() -> None:
    limit = max(settings.oauth_wait_minutes, 1) * 60
    now = time.monotonic()
    for state, entry in list(_pending.items()):
        # Завершённые держим чуть дольше: бот должен успеть их забрать.
        age_limit = limit if not entry.done else limit + 300
        if now - entry.created_at > age_limit:
            _pending.pop(state, None)


def _read_client_file(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > 20_000:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    section = data.get("web") or data.get("installed")
    if isinstance(section, dict) and section.get("client_id") and section.get("client_secret"):
        return data
    return None


def client_file() -> tuple[Path, dict[str, Any]] | None:
    """Скачанный из Google файл OAuth-клиента (client_secret_….json).

    Достаточно положить его в /data как есть: в нём ID, секрет, тип клиента и
    разрешённые адреса возврата — переменные в панели хостинга не нужны.
    Файл сильнее переменных: загрузить новый файл — осознанное действие, а
    устаревшая переменная в панели могла остаться от прошлых попыток.
    """
    explicit = (os.getenv("OPERON_GOOGLE_CLIENT_SECRET") or "").strip()
    if explicit:
        data = _read_client_file(Path(explicit))
        return (Path(explicit), data) if data else None
    from ..config import PERSIST_DIR

    candidates = [settings.credentials_dir / "client_secret.json"]
    for folder in (settings.credentials_dir, PERSIST_DIR):
        try:
            candidates += sorted(folder.glob("*.json"))
        except OSError:
            continue
    for path in candidates:
        data = _read_client_file(path)
        if data:
            return path, data
    return None


def redirect_uri() -> str:
    """Куда Google вернёт код — из файла клиента, если он есть, иначе из настроек."""
    explicit = (os.getenv("OPERON_OAUTH_REDIRECT_URI") or "").strip()
    if explicit:
        return explicit
    found = client_file()
    if found is None:
        return settings.oauth_redirect_uri
    data = found[1]
    if "installed" in data:
        return "http://localhost:8765/"
    uris = [str(u) for u in (data["web"].get("redirect_uris") or [])]
    for uri in uris:
        if uri.rstrip("/").endswith(settings.oauth_callback_path):
            return uri
    for uri in uris:
        if uri.startswith(("http://localhost", "http://127.0.0.1")):
            return uri
    # Подходящего адреса в клиенте нет — возвращаем нужный нам; _client_config
    # скажет, какой именно адрес добавить в Google Cloud.
    if settings.public_url:
        return settings.public_url + settings.oauth_callback_path
    return settings.oauth_redirect_uri


def callback_enabled() -> bool:
    """Код придёт на сервер сам (Web-клиент с адресом сервера) — копировать ничего не нужно."""
    return redirect_uri().rstrip("/").endswith(settings.oauth_callback_path)


def _config_from_env() -> dict[str, Any] | None:
    """Собирает конфигурацию клиента из GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.

    На Amvera это удобнее файла: переменные задаются в панели, на постоянный
    диск ничего загружать не нужно. Секция выбирается по адресу возврата —
    «web» для клиента с зарегистрированным адресом, «installed» для Desktop.
    """
    client_id = settings.google_client_id
    client_secret = settings.google_client_secret_value
    if not client_id or not client_secret:
        return None

    section = "web" if callback_enabled() else "installed"
    return {
        section: {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": [redirect_uri()],
        }
    }


def _client_config() -> dict[str, Any]:
    found = client_file()
    if found is not None:
        config = found[1]
        if "web" in config and "installed" not in config:
            # Для Web-клиента адрес возврата должен быть прописан в Google Cloud.
            # Список из скачанного файла проверяем сами; Google проверит по-настоящему.
            registered = config["web"].get("redirect_uris") or []
            wanted = redirect_uri()
            if wanted not in registered:
                raise OAuthError(
                    "OAuth-клиент имеет тип «Web application», а его список Authorized "
                    f"redirect URIs не содержит {wanted}. Добавьте этот адрес в Google Cloud "
                    "(Google Auth Platform → Clients → ваш клиент → Authorized redirect URIs), "
                    "скачайте JSON клиента заново и замените файл на сервере."
                )
        return config
    from_env = _config_from_env()
    if from_env is not None:
        return from_env
    raise OAuthError(
        "OAuth-клиент Google не настроен. Проще всего: скачайте JSON клиента из Google Cloud "
        "(Google Auth Platform → Clients → ⬇) и положите файл в /data на сервере как есть. "
        "Или задайте переменные GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET."
    )


def _flow() -> Any:
    """Собирает поток заново на каждом шаге: состояние между шагами не нужно.

    ``fetch_token(code=...)`` проверяет только сам код, поэтому хранить объект
    потока между сообщениями (и переживать с ним перезапуск) не требуется.
    """
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise OAuthError(
            "Не установлены библиотеки Google. Выполните: pip install -r requirements.txt"
        ) from exc

    try:
        return Flow.from_client_config(
            _client_config(),
            scopes=list(settings.google_scopes),
            redirect_uri=redirect_uri(),
        )
    except ValueError as exc:
        raise OAuthError(f"Файл OAuth-клиента не подходит для авторизации: {exc}") from exc


def start(label: str = "") -> tuple[str, str]:
    """Выдаёт ссылку авторизации и одноразовый state. Возвращает (url, state)."""
    flow = _flow()
    state = secrets.token_urlsafe(24)
    url, _ = flow.authorization_url(
        # Без access_type=offline Google не выдаст refresh_token, и через час
        # агент потеряет доступ до следующей ручной авторизации.
        access_type="offline",
        # Повторная авторизация без prompt=consent возвращается без refresh_token.
        prompt="consent",
        include_granted_scopes="true",
        state=state,
    )
    with _lock:
        _forget_stale()
        # Чей это /auth: код с публичного адреса возврата сохраним в токен
        # именно этого пользователя, а не того, кто окажется «текущим».
        _pending[state] = _Pending(
            created_at=time.monotonic(),
            label=label,
            account=accounts.current(),
            code_verifier=getattr(flow, "code_verifier", None) or "",
        )
    return url, state


def authorization_url() -> str:
    """Ссылка без отслеживания state — для ручных сценариев и диагностики."""
    return start()[0]


def take_result(state: str) -> _Pending | None:
    """Забирает результат, пришедший на /oauth2/callback, — один раз."""
    with _lock:
        entry = _pending.get(state)
        if entry is None or not entry.done:
            return None
        return _pending.pop(state)


def forget(state: str) -> None:
    with _lock:
        _pending.pop(state, None)


def handle_callback(code: str, state: str) -> dict[str, Any]:
    """Обрабатывает ответ Google на /oauth2/callback.

    Обменять можно только код с известным state: адрес возврата публичен, и
    без этой проверки любой запрос к нему пытался бы что-то подключить.
    """
    with _lock:
        _forget_stale()
        entry = _pending.get(state or "")
    if entry is None:
        raise OAuthError(
            "Ссылка авторизации неизвестна или устарела. Запросите новую: /auth в боте."
        )
    if entry.done:
        raise OAuthError("Эта ссылка уже использована. Если нужно заново — /auth.")

    try:
        with accounts.use(entry.account):
            result = exchange_code(code, state=state)
    except OAuthError as exc:
        with _lock:
            entry.done = True
            entry.error = str(exc)
        raise

    with _lock:
        entry.done = True
        entry.result = result
    return result


DENIED_HINTS = {
    # Человек нажал «Отмена» — вредить нечем, просто повторить.
    "access_denied": (
        "Google вернул отказ в доступе.\n\n"
        "Если вы нажали «Отмена» — повторите /auth и нажмите «Разрешить».\n"
        "Если нажимали «Разрешить», а Google всё равно написал «Доступ заблокирован» — "
        "это настройка Google Cloud, а не ваша ошибка: приложение ещё не прошло проверку "
        "Google, и пускает только адреса из списка Test users.\n"
        "Откройте Google Cloud → Google Auth Platform → Audience (экран согласия): "
        "добавьте свою почту в Test users либо нажмите Publish app."
    ),
    # Администратор организации запретил приложению доступ к аккаунтам домена.
    "admin_policy_enforced": (
        "Доступ заблокирован правилом организации Google Workspace (admin_policy_enforced): "
        "администратор домена запретил сторонние приложения. Обратитесь к администратору "
        "или подключите личный аккаунт."
    ),
    # Экран согласия Internal пускает только аккаунты своей организации.
    "org_internal": (
        "Экран согласия настроен как Internal, а вы вошли аккаунтом другой организации "
        "(org_internal). Либо войдите аккаунтом этой организации, либо смените тип экрана "
        "согласия на External."
    ),
    # Google не пускает встроенные браузеры Telegram и прочих приложений.
    "disallowed_useragent": (
        "Google не пускает вход из встроенного браузера (disallowed_useragent). "
        "Откройте ссылку в обычном браузере: скопируйте её и вставьте в Chrome или Safari."
    ),
}


def _describe_denied(code: str) -> str:
    """Почему Google не выдал код — по коду ошибки из адреса возврата."""
    hint = DENIED_HINTS.get((code or "").strip().lower())
    if hint:
        return hint
    return (
        f"Google вернул отказ в доступе ({code}). Повторите /auth и нажмите «Разрешить»; "
        "если повторится — посмотрите /check."
    )


def looks_like_code(text: str) -> bool:
    """Отличает вставленный код (или адрес возврата) от обычного вопроса."""
    raw = (text or "").strip()
    if not raw or len(raw.split()) > 1:
        return False
    if "code=" in raw:
        return True
    # Адрес возврата с отказом тоже ответ на наш запрос: Google не выдал код,
    # но объяснил причину (error=access_denied). Без этой ветки человек,
    # скопировавший адрес со страницы ошибки, получал «это не похоже на код» —
    # то есть совет искать код там, где Google его заведомо не выдал.
    if "error=" in raw:
        return True
    return bool(CODE_RE.match(raw))


def extract_code(text: str) -> str:
    """Достаёт код из адреса возврата или принимает его в чистом виде."""
    raw = (text or "").strip().strip("<>\"'")
    if not raw:
        raise OAuthError("Пустое сообщение — пришлите код или адрес из строки браузера.")

    if "code=" in raw:
        query = urlparse(raw).query or raw.split("?", 1)[-1]
        values = parse_qs(query).get("code") or []
        if not values or not values[0]:
            raise OAuthError(
                "В присланном адресе нет параметра code. Скопируйте адрес целиком — "
                "он выглядит так: http://localhost:8765/?code=4/0A…&scope=…"
            )
        return values[0]

    if "error=" in raw:
        # Причина отказа уже в адресе: Google различает «нажал Отмена»
        # (access_denied) и «аккаунт не пустили на экран согласия»
        # (admin_policy_enforced, org_internal, disallowed_useragent).
        # Общий совет «нажмите Разрешить» в последнем случае бесполезен:
        # человек нажимал, а его не пустили до экрана.
        query = urlparse(raw).query or raw.split("?", 1)[-1]
        code = (parse_qs(query).get("error") or ["access_denied"])[0]
        raise OAuthError(_describe_denied(code))

    # Скопированный из адресной строки код может остаться percent-encoded.
    code = unquote(raw)
    if not CODE_RE.match(code):
        raise OAuthError(
            "Это не похоже на код авторизации. Нужен либо код целиком (начинается с «4/»), "
            "либо весь адрес из строки браузера после подтверждения доступа."
        )
    return code


def _state_from(text: str) -> str:
    raw = (text or "").strip().strip("<>\"'")
    if "state=" not in raw:
        return ""
    query = urlparse(raw).query or raw.split("?", 1)[-1]
    return (parse_qs(query).get("state") or [""])[0]


def code_verifier_for(state: str) -> str:
    with _lock:
        entry = _pending.get(state or "")
        return entry.code_verifier if entry else ""


def exchange_code(text: str, state: str = "") -> dict[str, Any]:
    """Обменивает код на токен и сохраняет его. Возвращает описание результата.

    state — чья это ссылка: по нему находим секрет PKCE. Если пользователь
    прислал адрес целиком, state берём из него.
    """
    code = extract_code(text)
    verifier = code_verifier_for(_state_from(text) or state)
    flow = _flow()
    if verifier:
        flow.code_verifier = verifier
    # Обмен идёт через requests: те же сертификаты и прокси, что у всего бота.
    # Иначе OPERON_PROXY=none здесь не действовал бы, и /auth падал бы на
    # нестабильном системном прокси при работающем боте.
    configure_requests_session(flow.oauth2session)

    # Google возвращает scope'ы в своём порядке и добавляет openid — oauthlib
    # считает это подменой прав и падает. Расхождение здесь безопасно:
    # фактически выданные права мы проверяем ниже сами.
    previous = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 — библиотека бросает разные типы
        raise OAuthError(_describe_exchange_error(exc)) from exc
    finally:
        if previous is None:
            os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
        else:
            os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous

    creds = flow.credentials
    if not getattr(creds, "refresh_token", None):
        raise OAuthError(
            "Google выдал токен без refresh_token — через час доступ пропадёт. "
            "Отзовите доступ приложению на myaccount.google.com/permissions "
            "и пройдите /auth заново."
        )

    granted = set(getattr(creds, "scopes", None) or [])
    missing = [scope for scope in settings.google_scopes if scope not in granted]

    path = token_store.save_token(creds.to_json())
    google_client.reset_cache()
    logger.info("Токен Google сохранён (%s), выдано %s разрешений", path, len(granted))

    return {
        "path": str(path),
        "encrypted": token_store.encryption_enabled(),
        "scopes": sorted(granted),
        "missing_scopes": missing,
        "account": _account_email(),
    }


def _account_email() -> str:
    """Под какой учётной записью выдан доступ — важно перепроверить глазами."""
    try:
        return google_client.status().get("account_hint", "") or ""
    except Exception:  # noqa: BLE001 — необязательная информация
        return ""


def _describe_exchange_error(exc: Exception) -> str:
    message = str(exc)
    lowered = message.lower()
    if "invalid_grant" in lowered:
        return (
            "Google отклонил код (invalid_grant). Обычно это значит, что код уже "
            "использован или истёк — он живёт несколько минут. Начните заново: /auth"
        )
    if "redirect_uri_mismatch" in lowered:
        return (
            "Google отклонил адрес возврата (redirect_uri_mismatch). Ожидался "
            f"{redirect_uri()}. Убедитесь, что OAuth-клиент имеет тип "
            "«Desktop app», либо задайте OPERON_OAUTH_REDIRECT_URI под свой клиент."
        )
    if "invalid_client" in lowered:
        return (
            "Google не признал OAuth-клиент (invalid_client). GOOGLE_CLIENT_ID и "
            "GOOGLE_CLIENT_SECRET должны быть от одного и того же клиента, а секрет — "
            "действующим (после «Reset secret» старый перестаёт работать)."
        )
    if "access_denied" in lowered:
        return "Доступ не выдан: на экране согласия нажата «Отмена». Повторите /auth."
    # Сеть: прокси, таймаут, сертификат — объясняем словами и с советом.
    return f"Не удалось обменять код на токен: {explain(exc)}"


# Ошибки, которые Google показывает в браузере, до бота они не доходят.
TROUBLESHOOTING = (
    "\n\nЕсли Google показал ошибку:\n"
    "• redirect_uri_mismatch — проще всего создать в Google Cloud клиент типа «Desktop app» "
    "(Credentials → Create credentials → OAuth client ID) и поставить его ID и секрет в "
    "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET: ему адреса возврата не нужны. Либо добавьте адрес "
    "ниже в Authorized redirect URIs того клиента, чей ID указан ниже.\n"
    "• «Доступ заблокирован» / org_internal — вход не тем аккаунтом: экран согласия "
    "Internal пускает только аккаунты вашей организации Workspace.\n"
    "• access_denied / «приложение не проверено, тестирование» — экран согласия в режиме "
    "Testing: добавьте свой адрес в Test users или нажмите Publish app.\n"
    "• «API не включён» — включите Drive, Calendar и Sheets API в том же проекте."
)


def _client_line() -> str:
    """Какой клиент и какой адрес возврата уходят в Google — сверить с консолью."""
    try:
        config = _client_config()
    except OAuthError:
        return ""
    section = config.get("installed") or config.get("web") or {}
    client_id = str(section.get("client_id", ""))
    short = client_id.split(".apps.googleusercontent.com")[0]
    return (
        f"\n\nДля сверки в Google Cloud: клиент {short}, "
        f"адрес возврата {redirect_uri()}"
    )


def instructions() -> str:
    """Текст, который бот показывает вместе со ссылкой (без разметки)."""
    if callback_enabled():
        return (
            "1. Откройте ссылку и разрешите доступ — под той учётной записью, "
            "с документами которой должен работать ассистент.\n"
            "2. Всё. Код придёт на сервер сам, копировать ничего не нужно — "
            "я напишу, когда токен сохранится.\n\n"
            f"Жду ответа {settings.oauth_wait_minutes} мин."
            + TROUBLESHOOTING
            + _client_line()
        )
    return (
        "1. Откройте ссылку и разрешите доступ — под той учётной записью, "
        "с документами которой должен работать ассистент.\n"
        "2. Браузер попытается открыть "
        f"{redirect_uri()} и покажет ошибку соединения. Это нормально: "
        "слушать этот адрес некому.\n"
        "3. Скопируйте адрес из строки браузера целиком и пришлите его сюда "
        "следующим сообщением.\n\n"
        f"Ссылка действует ограниченное время, жду код {settings.oauth_wait_minutes} мин."
        + TROUBLESHOOTING
        + _client_line()
    )


def describe_client() -> dict[str, Any]:
    """Как настроен OAuth-клиент — для /status и диагностики."""
    source = ""
    found = client_file()
    if found is not None:
        kind = "Web" if "web" in found[1] else "Desktop"
        source = f"файл {found[0].name} (клиент {kind})"
    elif _config_from_env() is not None:
        source = "переменные окружения"
    return {
        "configured": bool(source),
        "source": source or "не настроен",
        "redirect_uri": redirect_uri(),
        "callback_mode": callback_enabled(),
    }


# --- проверка до клика ---------------------------------------------------------

# Что Google отвечает на ссылку входа, видно без входа в аккаунт: неверный
# адрес возврата или клиент — перенаправление на страницу ошибки с кодом
# внутри. Проверяем ссылку сами, чтобы пользователь никогда не получал ссылку,
# которая закончится «Доступ заблокирован».
PREFLIGHT_HINTS = {
    "redirect_uri_mismatch": (
        "Google не принимает адрес возврата {redirect} для клиента {client}. "
        "Google Cloud → Google Auth Platform → Clients → этот клиент → Authorized redirect URIs → "
        "добавьте {redirect} точно как написано, Save, подождите 5 минут."
    ),
    "invalid_client": (
        "Google не знает клиента {client}: в GOOGLE_CLIENT_ID опечатка или клиент удалён. "
        "Скопируйте Client ID заново из Google Cloud → Google Auth Platform → Clients."
    ),
    "deleted_client": "Клиент {client} удалён в Google Cloud — создайте новый и обновите GOOGLE_CLIENT_ID / SECRET.",
    "disabled_client": "Клиент {client} отключён в Google Cloud — включите его или создайте новый.",
}


def _decode_auth_error(location: str) -> str:
    """Код ошибки из адреса страницы ошибки Google (authError — base64 protobuf)."""
    import base64

    query = parse_qs(urlparse(location).query)
    raw = (query.get("authError") or [""])[0]
    if not raw:
        return "unknown_error"
    try:
        data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError):
        return "unknown_error"
    # Поле 1 — строка с кодом: 0x0A, длина, байты.
    if len(data) > 2 and data[0] == 0x0A and data[1] < len(data):
        return data[2:2 + data[1]].decode("utf-8", "replace")
    return "unknown_error"


def preflight() -> tuple[bool, str]:
    """(True, "") — Google покажет вход; (False, объяснение) — ссылка закончится ошибкой.

    Сеть недоступна — (True, ""): не мешаем входу из-за проверки.
    """
    import httpx

    from ..net import http_options

    try:
        config = _client_config()
    except OAuthError as exc:
        return False, str(exc)
    section = config.get("installed") or config.get("web") or {}
    client_id = str(section.get("client_id", ""))
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(settings.google_scopes),
        "access_type": "offline",
    }
    try:
        response = httpx.get(
            "https://accounts.google.com/o/oauth2/v2/auth", params=params,
            follow_redirects=False, timeout=15, **http_options(),
        )
    except httpx.HTTPError:
        return True, ""
    location = response.headers.get("location", "")
    if "/signin/oauth/error" not in location:
        return True, ""
    code = _decode_auth_error(location)
    short = client_id.split(".apps.googleusercontent.com")[0]
    template = PREFLIGHT_HINTS.get(code, "Google отклоняет вход: {code}. Клиент {client}, адрес возврата {redirect}.")
    return False, template.format(code=code, client=short, redirect=redirect_uri())


def configured() -> bool:
    return describe_client()["configured"]
