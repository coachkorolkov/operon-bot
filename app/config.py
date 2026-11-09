"""Конфигурация агента OPERON. Все значения читаются из окружения/.env."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from datetime import timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Постоянный диск Amvera (persistenceMount). Единственное место в контейнере,
# которое переживает передеплой, — и единственное, куда пользователь может
# положить файл через панель.
PERSIST_DIR = Path(os.getenv("OPERON_PERSIST_DIR", "/data"))


def _load_env_files() -> list[Path]:
    """Читает .env из всех мест, где он может лежать. Возвращает найденные.

    Порядок важен: побеждает то, что прочитано раньше, а переменные, уже
    заданные в окружении (панель Amvera), не перекрываются ничем — так
    настройка через панель всегда сильнее файла.

    Файл на постоянном диске нужен затем, что в панели Amvera переменные
    задаются по одной, а загрузить один файл — быстрее и труднее ошибиться.
    """
    candidates = []
    explicit = os.getenv("OPERON_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(PERSIST_DIR / ".env")
    candidates.append(BASE_DIR / ".env")

    loaded = []
    for path in candidates:
        try:
            if path.is_file():
                load_dotenv(path)
                loaded.append(path)
        except OSError:  # pragma: no cover — недоступный путь не должен ронять старт
            continue
    return loaded


# Что задано в окружении (панель Amvera) ДО чтения .env-файлов — чтобы
# самопроверка могла сказать, откуда взялось значение.
_ENV_BEFORE_FILES = frozenset(os.environ)
ENV_FILES_LOADED = _load_env_files()


def env_source(name: str) -> str:
    """Откуда взята переменная: панель (окружение) или конкретный .env-файл."""
    if name in _ENV_BEFORE_FILES:
        return "переменные окружения (панель Amvera)"
    for path in ENV_FILES_LOADED:
        try:
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if line.strip().startswith(name + "="):
                    return f"файл {path}"
        except OSError:
            continue
    return "не задано"


def parse_telegram_ids(raw: str) -> tuple[frozenset[int], list[str]]:
    """ID через запятую, точку с запятой, пробел или перенос строки.

    Возвращает (ID, отброшенные куски) — отброшенное показывает /check, чтобы
    опечатка в списке не превращалась в «бот молча не отвечает».
    """
    ids: set[int] = set()
    rejected: list[str] = []
    for chunk in re.split(r"[,;\n]+", raw or ""):
        chunk = chunk.strip().strip("\"'").strip()
        if not chunk:
            continue
        pieces = chunk.split()
        # «198704816 159893732» — два ID через пробел; «159 893 732» — один,
        # набранный с пробелами. Различаем по длине кусков: ID короче 6 цифр нет.
        if len(pieces) > 1 and all(p.lstrip("-").isdigit() and len(p.lstrip("-")) >= 6 for p in pieces):
            candidates = pieces
        else:
            candidates = ["".join(pieces)]
        for part in candidates:
            if part.lstrip("-").isdigit():
                ids.add(int(part))
            else:
                rejected.append(chunk)
                break
    return frozenset(ids), rejected

# Провайдеры доступа к модели. Anthropic-совместимый протокол поддерживают оба,
# поэтому код агента одинаков — различаются адрес, ключ и набор возможностей.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# RouterAI — российский шлюз, совместим с API OpenAI.
ROUTERAI_BASE_URL = "https://routerai.ru/api/v1"


def _str(name: str, default: str = "") -> str:
    """Строка из окружения без окружающих пробелов и кавычек.

    Файл .env правят руками, и пробел на конце строки — обычное дело. Он не
    виден глазом, но уходит прямо в адрес запроса: токен Telegram с хвостовым
    пробелом даёт «404 Not Found», и человек начинает искать причину в токене.
    Кавычки снимаем по той же причине: их часто ставят по привычке.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().strip("\"'").strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _detect_provider() -> str:
    explicit = (os.getenv("OPERON_LLM_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    if os.getenv("ROUTERAI_API_KEY"):
        return "routerai"
    if os.getenv("OPENROUTER_API_KEY"):
        return "openrouter"
    # Свой адрес шлюза + свой ключ = сторонний провайдер («AI-роутер»).
    if os.getenv("OPERON_LLM_BASE_URL") and os.getenv("OPERON_LLM_API_KEY"):
        return "custom"
    return "anthropic"


def _default_model(provider: str) -> str:
    if provider == "openrouter":
        # В OpenRouter идентификаторы моделей включают вендора.
        return "anthropic/claude-opus-4.1"
    if provider == "routerai":
        # Проверена на RouterAI с вызовом инструментов. Без значения по
        # умолчанию свежий сервер без OPERON_MODEL не ответил бы ни на что.
        # Сменить — OPERON_MODEL или выбор в Mini App.
        return "openai/gpt-4.1-mini"
    if provider == "custom":
        # У своего шлюза свой каталог — имя модели обязан задать пользователь.
        # Посмотреть доступные: python -m app.probe
        return ""
    return "claude-opus-5"


@dataclass(frozen=True)
class Settings:
    # --- Провайдер модели ---
    provider: str = _detect_provider()
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = _int("OPERON_MAX_TOKENS", 32000)
    effort: str = _str("OPERON_EFFORT", "high")  # low|medium|high|xhigh|max
    max_tool_iterations: int = _int("OPERON_MAX_TOOL_ITERATIONS", 24)

    # --- Организация ---
    org_name: str = _str("OPERON_ORG_NAME", "OPERON")
    timezone_name: str = _str("OPERON_TIMEZONE", "Europe/Moscow")

    # --- Почта (приглашения на встречи) --------------------------------------
    # Сервисный аккаунт Google без делегирования приглашать участников не
    # может (403 forbiddenForServiceAccounts), и всё событие с участником
    # Google отклоняет целиком. Поэтому приглашение отправляем сами, обычной
    # почтой от имени владельца календаря: письмо с файлом .ics даёт тот же
    # эффект — встреча добавляется в календарь одним нажатием.
    smtp_host: str = _str("OPERON_SMTP_HOST")
    smtp_port: int = _int("OPERON_SMTP_PORT", 587)
    smtp_user: str = _str("OPERON_SMTP_USER")
    smtp_password: str = _str("OPERON_SMTP_PASSWORD")
    smtp_from: str = _str("OPERON_SMTP_FROM")
    # Копия владельцу: он видит, что именно ушло участникам, и может
    # переслать письмо, если адрес окажется неверным.
    smtp_copy_owner: bool = _bool("OPERON_SMTP_COPY_OWNER", True)

    # --- Данные ---
    kb_dir: Path = Path(os.getenv("OPERON_KB_DIR", str(BASE_DIR / "knowledge_base")))
    data_dir: Path = Path(os.getenv("OPERON_DATA_DIR", str(BASE_DIR / "data")))
    credentials_dir: Path = Path(
        os.getenv("OPERON_CREDENTIALS_DIR", str(BASE_DIR / "credentials"))
    )

    # --- Интернет ---
    web_search_max_uses: int = _int("OPERON_WEB_SEARCH_MAX_USES", 8)

    # --- Сервер ---
    host: str = _str("OPERON_HOST", "127.0.0.1")
    port: int = _int("PORT", _int("OPERON_PORT", 8000))
    public_url: str = _str("OPERON_PUBLIC_URL").rstrip("/")

    # --- Доступ ---
    access_password: str = _str("OPERON_ACCESS_PASSWORD")
    session_secret: str = _str("OPERON_SESSION_SECRET")
    auth_ttl_hours: int = _int("OPERON_AUTH_TTL_HOURS", 168)

    # --- Telegram ---
    telegram_token: str = _str("TELEGRAM_BOT_TOKEN")
    telegram_poll_timeout: int = _int("TELEGRAM_POLL_TIMEOUT", 30)

    # --- Напоминания ---
    # Бот пишет сам: без этого пункт ТЗ про напоминания закрыть нечем —
    # инструмент срабатывает только в ответ на реплику пользователя.
    digest_hour: int = _int("OPERON_DIGEST_HOUR", 9)
    remind_before_days: int = _int("OPERON_REMIND_BEFORE_DAYS", 2)
    # За сколько минут до встречи из календаря бот пишет в Telegram. 0 — не писать.
    meeting_remind_minutes: int = _int("OPERON_MEETING_REMIND_MINUTES", 30)

    # --- Ограничения ---
    max_history_messages: int = _int("OPERON_MAX_HISTORY", 200)
    session_ttl_minutes: int = _int("OPERON_SESSION_TTL_MINUTES", 720)
    # Сколько ждать ответа на карточку подтверждения. Молчание = отказ, но
    # без срока «отказ» никогда не наступает и диалог висит вечно. 0 — не ждать
    # ограниченно (карточка живёт, пока жива сессия).
    confirmation_ttl_minutes: int = _int("OPERON_CONFIRMATION_TTL_MINUTES", 30)
    # Сколько бот ждёт код авторизации Google после /auth. Сам код Google
    # живёт около 10 минут, дольше ждать нет смысла.
    oauth_wait_minutes: int = _int("OPERON_OAUTH_WAIT_MINUTES", 15)

    google_scopes: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            s
            for s in os.getenv(
                "OPERON_GOOGLE_SCOPES",
                # Минимальный набор: каждый лишний scope — лишний риск.
                # drive.readonly   — чтение файлов, доступных пользователю;
                # drive.file       — создание файлов и доступ ТОЛЬКО к тем,
                #                    которые создал сам агент (протоколы, КП);
                #                    чужие файлы этим scope'ом не изменить;
                # calendar.events  — чтение и запись событий;
                # calendar.calendarlist.readonly — список календарей аккаунта
                #                    (рабочие, общие), чтобы читать встречи из всех.
                # Любая запись (Диск и календарь) выполняется только после
                # подтверждения пользователя — это проверяет рантайм, не промпт.
                # Менять набор — значит проходить OAuth заново: старый токен
                # выдан под старые scope'ы и новых прав не получит.
                "https://www.googleapis.com/auth/drive.readonly "
                "https://www.googleapis.com/auth/drive.file "
                "https://www.googleapis.com/auth/calendar.events "
                "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
            ).split()
            if s
        )
    )

    def __post_init__(self) -> None:
        # dataclass заморожен, поэтому вычисляемые поля выставляем через object.
        # Значение, переданное явно, имеет приоритет над окружением — иначе
        # Settings(base_url=...) молча игнорировался бы (важно для тестов
        # и для программной сборки конфигурации).
        provider = self.provider or "anthropic"
        object.__setattr__(self, "provider", provider)

        if not self.model:
            object.__setattr__(
                self, "model", _str("OPERON_MODEL") or _default_model(provider)
            )
        if not self.api_key:
            object.__setattr__(
                self,
                "api_key",
                _str("OPERON_LLM_API_KEY")
                or _str("ROUTERAI_API_KEY")
                or _str("OPENROUTER_API_KEY")
                or _str("ANTHROPIC_API_KEY"),
            )
        if not self.base_url:
            default_base = {
                "openrouter": OPENROUTER_BASE_URL,
                "routerai": ROUTERAI_BASE_URL,
            }.get(provider, "")
            # ANTHROPIC_BASE_URL учитываем только для самой Anthropic: иначе
            # оставшаяся в окружении переменная увела бы запросы стороннего
            # шлюза на чужой адрес.
            anthropic_base = _str("ANTHROPIC_BASE_URL") if provider == "anthropic" else ""
            object.__setattr__(
                self,
                "base_url",
                (
                    _str("OPERON_LLM_BASE_URL") or default_base or anthropic_base
                ).rstrip("/"),
            )
        else:
            object.__setattr__(self, "base_url", self.base_url.rstrip("/"))

    # --- возможности провайдера -------------------------------------------
    # Серверные инструменты поиска, параметр effort и кэширование промпта —
    # расширения Anthropic. Через сторонний шлюз они могут быть не приняты,
    # поэтому по умолчанию включаются только на прямом доступе к Anthropic.

    @property
    def is_anthropic_direct(self) -> bool:
        return self.provider == "anthropic"

    @property
    def llm_protocol(self) -> str:
        """Формат запросов: «anthropic» (/v1/messages) или «openai» (/chat/completions).

        Явно задаётся через OPERON_LLM_PROTOCOL. По умолчанию: Anthropic — для
        прямого доступа и OpenRouter (у него есть Anthropic-совместимый режим),
        OpenAI-совместимый — для прочих шлюзов, где это де-факто стандарт.
        """
        explicit = (os.getenv("OPERON_LLM_PROTOCOL") or "").strip().lower()
        if explicit in {"anthropic", "openai"}:
            return explicit
        return "anthropic" if self.provider in {"anthropic", "openrouter"} else "openai"

    @property
    def models_url(self) -> str:
        """Адрес каталога моделей провайдера (для диагностики)."""
        if not self.base_url:
            return ""
        return self.base_url.rstrip("/") + "/models"

    @property
    def web_search_enabled(self) -> bool:
        return _bool("OPERON_WEB_SEARCH", self.is_anthropic_direct)

    @property
    def effort_enabled(self) -> bool:
        return _bool("OPERON_USE_EFFORT", self.is_anthropic_direct)

    @property
    def prompt_cache_enabled(self) -> bool:
        return _bool("OPERON_USE_PROMPT_CACHE", self.is_anthropic_direct)

    @property
    def extra_headers(self) -> dict[str, str]:
        """OpenRouter просит указывать источник трафика — влияет на лимиты."""
        if self.provider != "openrouter":
            return {}  # у остальных шлюзов обязательных заголовков нет
        return {
            "HTTP-Referer": self.public_url or "https://amvera.ru",
            "X-Title": f"Assistant {self.org_name}",
        }

    @property
    def auth_required(self) -> bool:
        return bool(self.access_password)

    @property
    def telegram_allowed_users(self) -> frozenset[int]:
        """Белый список Telegram ID. Единственная граница доступа к боту.

        Бот работает с Google-аккаунтом владельца, поэтому любое сообщение
        не из этого списка игнорируется молча — без списка бот не стартует.
        """
        return parse_telegram_ids(os.getenv("TELEGRAM_ALLOWED_USERS", ""))[0]

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_allowed_users)

    @property
    def tz(self) -> tzinfo:
        """Часовой пояс организации. Никогда не бросает исключение.

        Прежний запасной вариант возвращал ZoneInfo("UTC") — и падал сам:
        на Windows нет системной базы IANA, а без пакета tzdata недоступен и
        ключ «UTC». Исключение вылетало из свойства и роняло любой вызов
        datetime.now(...) — то есть создание поручения и сохранение протокола.

        Отсюда правило: запасной вариант не должен зависеть от того же, из-за
        чего отказал основной. timezone.utc — часть стандартной библиотеки и
        доступен всегда.
        """
        try:
            return ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return timezone.utc

    @property
    def google_token_path(self) -> Path:
        return self.credentials_dir / "google_token.json"

    # --- OAuth-клиент Google -------------------------------------------------
    # Клиент можно задать двумя способами: файлом client_secret.json на диске
    # или парой переменных окружения. Второй способ удобнее на Amvera —
    # ничего не нужно загружать на постоянный диск.

    @property
    def google_client_id(self) -> str:
        """Client ID без лишнего: его часто копируют как ссылку.

        «https://…apps.googleusercontent.com/» Google не узнаёт и отвечает
        invalid_client уже после нажатия ссылки авторизации — причина
        неочевидна. Схему и хвостовую косую черту отрезаем сами.
        """
        value = _str("GOOGLE_CLIENT_ID")
        for prefix in ("https://", "http://"):
            if value.lower().startswith(prefix):
                value = value[len(prefix):]
        return value.strip().rstrip("/")

    @property
    def google_client_secret_value(self) -> str:
        return _str("GOOGLE_CLIENT_SECRET")

    @property
    def google_client_type(self) -> str:
        """Тип OAuth-клиента: «desktop», «web» или пусто (определить по адресу).

        Это не косметика: у Desktop-клиента адрес возврата — непрослушанный
        localhost, у Web — заранее зарегистрированный публичный адрес. Перепутать
        их значит получить redirect_uri_mismatch уже после нажатия ссылки, когда
        причина совсем не очевидна.
        """
        return _str("OPERON_GOOGLE_CLIENT_TYPE").lower()

    @property
    def oauth_callback_path(self) -> str:
        return "/oauth2/callback"

    @property
    def oauth_redirect_uri(self) -> str:
        """Куда Google вернёт код.

        Клиент типа **Web application** обязан возвращать код на заранее
        зарегистрированный адрес, и localhost для него не подходит — поэтому
        по умолчанию берём публичный адрес приложения: код приходит прямо на
        сервер, копировать ничего не нужно.

        Клиенту типа **Desktop** годится любой localhost. Слушать его некому,
        поэтому пользователь копирует адрес из строки браузера и отдаёт боту.
        Этот режим включается, когда публичный адрес не задан.
        """
        explicit = _str("OPERON_OAUTH_REDIRECT_URI")
        if explicit:
            return explicit
        # Тип клиента важнее наличия публичного адреса: Desktop-клиент
        # зарегистрированный https-адрес попросту не примет.
        if self.google_client_type == "desktop":
            return "http://localhost:8765/"
        if self.public_url:
            return self.public_url + self.oauth_callback_path
        return "http://localhost:8765/"

    @property
    def oauth_callback_enabled(self) -> bool:
        """Код придёт на сервер сам, без копирования из адресной строки."""
        return self.oauth_redirect_uri.rstrip("/").endswith(self.oauth_callback_path)

    @property
    def google_client_secret_path(self) -> Path:
        env_path = os.getenv("OPERON_GOOGLE_CLIENT_SECRET")
        if env_path:
            return Path(env_path)
        return self.credentials_dir / "client_secret.json"

    @property
    def reminders_enabled(self) -> bool:
        return _bool("OPERON_REMINDERS", True)

    @property
    def digest_weekdays(self) -> frozenset[int]:
        """Дни недели для сводки, ISO: 1 — понедельник, 7 — воскресенье.

        По умолчанию рабочая неделя: сводка о просроченных поручениях в
        воскресенье утром никому не нужна.
        """
        raw = (os.getenv("OPERON_DIGEST_WEEKDAYS") or "1-5").strip()
        days: set[int] = set()
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, _, end = part.partition("-")
                if start.strip().isdigit() and end.strip().isdigit():
                    days.update(range(int(start), int(end) + 1))
            elif part.isdigit():
                days.add(int(part))
        return frozenset(d for d in days if 1 <= d <= 7) or frozenset({1, 2, 3, 4, 5})

    @property
    def quiet_hours(self) -> tuple[int, int]:
        """Часы тишины «начало-конец», например 22-8. Равные значения — тишины нет."""
        raw = (os.getenv("OPERON_QUIET_HOURS") or "22-8").strip()
        start, _, end = raw.partition("-")
        try:
            first, second = int(start), int(end)
        except ValueError:
            return (22, 8)
        if not (0 <= first <= 23 and 0 <= second <= 23):
            return (22, 8)
        return (first, second)

    @property
    def tasks_path(self) -> Path:
        return self.data_dir / "tasks.json"

    @property
    def reminders_path(self) -> Path:
        return self.data_dir / "reminders.json"

    @property
    def personal_reminders_path(self) -> Path:
        return self.data_dir / "personal_reminders.json"

    @property
    def kpi_path(self) -> Path:
        return self.data_dir / "kpi.json"

    @property
    def protocols_path(self) -> Path:
        return self.data_dir / "protocols.json"

    @property
    def sessions_path(self) -> Path:
        return self.data_dir / "sessions.json"


settings = Settings()

settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.credentials_dir.mkdir(parents=True, exist_ok=True)
