"""Бот Telegram: интерфейс агента с кнопками подтверждения.

Единственная граница доступа — белый список Telegram ID. Бот работает с
Google-аккаунтом и бюджетом владельца, поэтому сообщения от посторонних
игнорируются молча: отвечать «вам сюда нельзя» — значит подтверждать, что
бот существует.

Запуск отдельным процессом:  python -m app.telegram.bot
Вместе с веб-интерфейсом бот стартует фоновым потоком (см. app/server.py).
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..agent import agent as default_agent
from ..config import settings
from ..model_choice import current_model
from ..integrations import google_sa, accounts, google_client, google_oauth
from ..kb import intake, knowledge_base
from ..sessions import store
from .. import reminders, stt
from . import access
from .api import TelegramAPI, TelegramError
from .format import escape, split_message, to_telegram_html
from ..tools import outbox
from . import menu
from .webapp import miniapp_url

logger = logging.getLogger(__name__)

GREETING = (
    "Деловой ассистент {org}.\n\n"
    "Отвечаю по базе знаний «Квант», Google Диску и календарю, ищу в интернете — "
    "всегда со ссылкой на источник. Чего нет в источниках — так и скажу, не придумываю.\n"
    "Ничего не создаю и не меняю без вашего подтверждения.\n"
    "Пишите, присылайте голосовые и документы. Подключить Google — /auth.\n\n"
    "Команды:\n"
    "/new — начать диалог заново\n"
    "/status — что подключено\n"
    "/auth — подключить Google (или переподключить)\n"
    "/menu — меню по разделам\n"
    "/app — приложение: выбор модели, баланс, чат\n"
    "/check — проверить всё: модель, баланс, Google, поиск\n"
    "/users — кто имеет доступ (владельцам: /adduser ID)\n"
    "/help — подсказка"
)

WELCOME = (
    "Я — {org}, ваш деловой ассистент.\n\n"
    "📅 Встречи и напоминания в Google Календаре\n"
    "✅ Поручения и план на день\n"
    "📚 Ответы по базе знаний «Квант» и Google Диску — со ссылками на источники\n"
    "📄 Протоколы, КП и расчёты файлами Word и Excel\n"
    "🌐 Поиск в интернете\n\n"
    "Ничего не создаю и не меняю без вашего подтверждения.\n\n"
    "Разделы — на кнопках под полем ввода. Можно просто написать вопрос "
    "или отправить голосовое. Все команды — /help."
)


def _hello(first_name: str = "") -> str:
    """«Добрый день, Кирилл!» — по времени организации."""
    from datetime import datetime

    hour = datetime.now(settings.tz).hour
    if 5 <= hour < 12:
        hello = "Доброе утро"
    elif 12 <= hour < 18:
        hello = "Добрый день"
    elif 18 <= hour < 23:
        hello = "Добрый вечер"
    else:
        hello = "Доброй ночи"
    name = first_name.strip()
    return f"{hello}, {name}! 👋" if name else f"{hello}! 👋"


THINKING = "⏳ Думаю…"
EXPIRED_MARK = "⌛ Время вышло — действие отменено"


@dataclass
class PendingCard:
    """Карточка подтверждения, ждущая нажатия кнопки."""

    chat_id: int
    message_id: int
    tool_use_id: str
    title: str


@dataclass
class ChatState:
    """Незакрытые подтверждения одного чата."""

    cards: dict[str, PendingCard] = field(default_factory=dict)
    decisions: dict[str, str] = field(default_factory=dict)
    comments: dict[str, str] = field(default_factory=dict)
    awaiting_comment_for: str | None = None
    # Момент выдачи ссылки /auth: следующее сообщение считаем кодом.
    oauth_started_at: float | None = None
    # Одноразовый state выданной ссылки — по нему забираем результат,
    # если код пришёл на сервер сам (клиент типа Web).
    oauth_state: str = ""
    # Присланный документ, ожидающий решения: класть его в базу знаний или нет.
    pending_file: Any = None

    @property
    def oauth_expired(self) -> bool:
        if self.oauth_started_at is None:
            return False
        limit = settings.oauth_wait_minutes
        return limit > 0 and time.monotonic() - self.oauth_started_at > limit * 60


MAX_VOICE_SECONDS = 600
STRANGER_REPEAT_SECONDS = 24 * 3600
STRANGER_REPORTS_PER_HOUR = 10
CONFLICT_REPEAT_SECONDS = 6 * 3600


class TelegramBot:
    def __init__(self, api: TelegramAPI | None = None, agent: Any = None) -> None:
        self._api = api or TelegramAPI(settings.telegram_token)
        self._agent = agent or default_agent
        self._allowed = settings.telegram_allowed_users
        # Кому меню (команды, кнопка Mini App) уже поставлено.
        self._menu_ready: set[int] = set()
        self._states: dict[int, ChatState] = {}
        self._offset: int | None = None
        self._stop = threading.Event()
        # Посторонние, о которых уже сообщили (ID → когда), и последний отчёт о 409.
        self._strangers: dict[int, float] = {}
        self._conflict_reported: float | None = None

        if not self._allowed:
            raise TelegramError(
                "Не задан TELEGRAM_ALLOWED_USERS. Без белого списка бот не запускается: "
                "иначе доступ к вашему Google-аккаунту получит любой, кто найдёт бота."
            )

    # --- жизненный цикл ---

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        """Читает обновления, пока не позовут stop().

        Ошибку запуска наружу не гасим: перезапуском занимается надзор
        (app/telegram/supervisor.py), а молча вернувшийся поток выглядел бы
        как работающий бот.
        """
        me = self._api.get_me()
        logger.info(
            "Telegram-бот @%s запущен, разрешено пользователей: %s",
            me.get("username", "?"),
            len(self._allowed),
        )

        self._take_over_from_webhook()
        self._install_menu_button()

        backoff = 1.0
        while not self._stop.is_set():
            try:
                updates = self._api.get_updates(self._offset, settings.telegram_poll_timeout)
                backoff = 1.0
            except TelegramError as exc:
                logger.error("Ошибка получения обновлений: %s", exc)
                self._report_conflict(exc)
                # 409 означает второй экземпляр — ждём дольше, чтобы не спорить с ним.
                self._stop.wait(min(backoff, 60))
                backoff = min(backoff * 2, 60)
                continue

            for update in updates:
                self._offset = update["update_id"] + 1
                try:
                    self._handle_update(update)
                except Exception:  # noqa: BLE001 — один сбой не должен ронять бота
                    logger.exception("Сбой обработки обновления %s", update.get("update_id"))
                    self._report_failure(update)

            # Сроки идут своим ходом, даже когда пользователь молчит: длинный
            # опрос возвращается не реже раза в telegram_poll_timeout секунд.
            try:
                self.sweep_expired()
            except Exception:  # noqa: BLE001
                logger.exception("Сбой при снятии просроченных подтверждений")

            try:
                self.send_reminders()
            except Exception:  # noqa: BLE001 — напоминание не должно ронять бота
                logger.exception("Сбой при отправке напоминаний")

        logger.info("Telegram-бот остановлен")

    def _take_over_from_webhook(self) -> None:
        """Снимает webhook, если бот раньше работал на другой платформе.

        Пока webhook включён, Telegram отдаёт сообщения только на его адрес,
        а getUpdates отвечает 409 — бот молчал бы, хотя запущен и исправен.
        Перенос бота сюда и есть решение забрать его, поэтому снимаем сами
        и пишем в журнал, откуда забрали.
        """
        try:
            info = self._api.get_webhook_info()
            url = info.get("url") if isinstance(info, dict) else ""
            if url:
                logger.warning(
                    "На боте был webhook %s (ожидало %s сообщений) — снимаю: бот работает здесь",
                    url, info.get("pending_update_count", 0),
                )
                self._api.delete_webhook()
        except Exception as exc:  # noqa: BLE001 — без этого бот всё равно попробует читать
            logger.warning("Не удалось проверить webhook: %s", exc)

    # --- маршрутизация ---

    def _allowed_user(self, update: dict[str, Any]) -> int | None:
        source = update.get("message") or update.get("callback_query") or {}
        user = source.get("from") or {}
        user_id = user.get("id")
        if user_id in self._allowed or user_id in access.extra():
            return user_id
        # WARNING, а не INFO: «бот молчит» чаще всего значит, что ID в белом
        # списке записан не тот, — эта строка в журнале называет настоящий.
        logger.warning(
            "Проигнорировано сообщение от id=%s (@%s): его нет в TELEGRAM_ALLOWED_USERS",
            user_id,
            user.get("username", ""),
        )
        return None

    def _handle_update(self, update: dict[str, Any]) -> None:
        user_id = self._allowed_user(update)
        if user_id is None:
            self._report_stranger(update)
            return
        # Всё, что бот делает по этому сообщению, — от имени этого человека:
        # его Google, его /auth, его /check (integrations/accounts.py).
        self._ensure_menu(user_id)
        self._current_user = user_id
        with accounts.use(user_id):
            if "callback_query" in update:
                self._handle_callback(update["callback_query"])
            elif "message" in update:
                self._handle_message(update["message"])

    def _report_stranger(self, update: dict[str, Any]) -> None:
        """Пишет человек не из списка: ему — его ID, владельцам — кнопка «Дать доступ».

        Раньше такие сообщения пропадали молча, и «бот не отвечает» нельзя было
        отличить от поломки: чаще всего в список записан не тот ID (например,
        ID другого аккаунта или бота). Теперь настоящий ID виден сразу.
        Не чаще раза в сутки на человека — чтобы не превращать бота в спамера.
        """
        message = update.get("message")
        user = (message or {}).get("from") or {}
        user_id = user.get("id")
        if not message or not isinstance(user_id, int) or user.get("is_bot"):
            return
        if message.get("chat", {}).get("type", "private") != "private":
            return
        now = time.monotonic()
        last = self._strangers.get(user_id)
        if last is not None and now - last < STRANGER_REPEAT_SECONDS:
            return
        # Общий предел: если бота найдут случайные люди, владельцев не завалит.
        recent = sum(1 for t in self._strangers.values() if now - t < 3600)
        if recent >= STRANGER_REPORTS_PER_HOUR:
            return
        self._strangers[user_id] = now

        try:
            self._api.send_message(
                message["chat"]["id"],
                f"Доступа к боту пока нет. Ваш Telegram ID: <code>{user_id}</code>\n"
                "Владельцу уже пришёл запрос — как только он нажмёт «Дать доступ», "
                "напишите сюда ещё раз.",
            )
        except Exception as exc:  # noqa: BLE001 — ответ постороннему необязателен
            logger.warning("Не удалось ответить %s: %s", user_id, exc)

        name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p) or "без имени"
        username = f" @{user['username']}" if user.get("username") else ""
        text = (
            f"🔒 Боту пишет человек не из списка доступа: <b>{escape(name)}</b>{escape(username)}, "
            f"ID <code>{user_id}</code>.\n"
            f"Сообщение: <i>{escape((message.get('text') or '[не текст]')[:200])}</i>\n\n"
            "Если это ваш сотрудник или тестировщик — дайте доступ кнопкой. "
            "Если в списке у вас записан другой ID этого человека — значит, записан неверный."
        )
        keyboard = {"inline_keyboard": [[{"text": "✅ Дать доступ", "callback_data": f"u:add:{user_id}"}]]}
        for admin in sorted(access.admins()):
            try:
                self._api.send_message(admin, text, reply_markup=keyboard)
            except Exception as exc:  # noqa: BLE001 — владелец мог не нажать «Старт»
                logger.warning("Не удалось сообщить владельцу %s о новом пользователе: %s", admin, exc)

    def _grant_from_button(self, callback: dict[str, Any], chat_id: int, data: str) -> None:
        """Кнопка «Дать доступ» из сообщения о постороннем."""
        admin = (callback.get("from") or {}).get("id")
        target = data.rsplit(":", 1)[-1]
        if admin not in self._allowed:
            self._api.answer_callback_query(callback["id"], "Давать доступ могут только владельцы бота.")
            return
        if not target.lstrip("-").isdigit():
            self._api.answer_callback_query(callback["id"], "Кнопка устарела.")
            return
        target_id = int(target)
        added = access.add(target_id, admin)
        if added:
            logger.warning("Пользователь %s добавлен в бот кнопкой пользователем %s", target_id, admin)
        self._strangers.pop(target_id, None)
        self._api.answer_callback_query(callback["id"], "Доступ открыт" if added else "Уже в списке")
        self._api.edit_message_text(
            chat_id, callback["message"]["message_id"],
            f"✅ Доступ для <code>{target_id}</code> открыт. Убрать: /removeuser {target_id}",
            reply_markup={"inline_keyboard": []},
        )
        try:
            self._api.send_message(target_id, "✅ Доступ открыт. Отправьте /start, чтобы начать.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось сообщить %s об открытом доступе: %s", target_id, exc)

    def _report_failure(self, update: dict[str, Any]) -> None:
        """Сбой обработки: человек должен узнать, что ответа не будет, а не ждать его."""
        source = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
        chat_id = (source.get("chat") or {}).get("id")
        if chat_id is None:
            return
        try:
            self._api.send_message(
                chat_id,
                "⚠️ Не получилось обработать сообщение — внутренняя ошибка. "
                "Попробуйте ещё раз; если повторится — /check покажет, что не работает.",
            )
        except Exception:  # noqa: BLE001
            pass

    def _report_conflict(self, error: Exception) -> None:
        """409 от Telegram: этим же токеном опрашивает обновления ещё один экземпляр бота.

        Сообщения делятся между экземплярами случайно: часть людей «не получает
        ответа», потому что их сообщение забрал другой экземпляр — например,
        старый сервер со старым списком доступа. Владельцам — раз в 6 часов.
        """
        if "409" not in str(error) and "onflict" not in str(error):
            return
        webhook = "webhook" in str(error).lower()
        if webhook:
            # Кто-то (обычно старый сервер) снова включил webhook: забираем бота обратно.
            self._take_over_from_webhook()
        now = time.monotonic()
        if self._conflict_reported is not None and now - self._conflict_reported < CONFLICT_REPEAT_SECONDS:
            return
        self._conflict_reported = now
        logger.error("Этим токеном пользуется второй экземпляр бота: %s", error)
        text = (
            "⚠️ <b>Другой сервер включил webhook для этого бота.</b>\n"
            "Пока он включён, сообщения уходят туда, а не сюда. Я его снял, но если он "
            "включается снова — выключите бота на старом сервере (например, на Amvera)."
        ) if webhook else (
            "⚠️ <b>Работают два экземпляра бота с одним токеном.</b>\n"
            "Telegram отдаёт сообщения то одному, то другому, поэтому часть людей не получает "
            "ответа. Скорее всего, не выключен старый бот (например, на Amvera) или запущены "
            "две копии на сервере. Остановите лишний экземпляр."
        )
        for admin in sorted(access.admins()):
            try:
                self._api.send_message(admin, text)
            except Exception:  # noqa: BLE001
                pass

    # --- сообщения ---

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()

        if message.get("document"):
            self._handle_document(chat_id, message["document"], message.get("caption", ""))
            return

        if not text and (message.get("voice") or message.get("audio")):
            self._handle_voice(chat_id, message.get("voice") or message.get("audio"))
            return

        if not text:
            kind = next(
                (k for k in ("video", "video_note", "photo", "sticker") if k in message),
                "",
            )
            hint = {
                "video": "Видео пока не распознаю — пришлите голосовое или текст.",
                "video_note": "Видеосообщения пока не распознаю — пришлите голосовое.",
                "photo": "Картинки пока не читаю — пришлите документ файлом.",
            }.get(kind, "Понимаю текст, голосовые и документы.")
            self._api.send_message(
                chat_id,
                escape(
                    f"{hint} Принимаю текст, голосовые и документы файлом: "
                    ".docx, .pdf, .xlsx, .pptx, .md, .csv, .json."
                ),
            )
            return

        target = menu.keyboard_target(text)
        if target is not None:
            self._handle_keyboard(chat_id, target)
            return

        if text.startswith("/"):
            self._handle_command(chat_id, text, first_name=(message.get("from") or {}).get("first_name", ""))
            return

        self._handle_text(chat_id, text)

    def _handle_keyboard(self, chat_id: int, target: tuple[str, str]) -> None:
        """Кнопка постоянной клавиатуры: раздел, готовый запрос или команда."""
        kind, value = target
        if kind == "category":
            category = menu.category(value)
            self._api.send_message(chat_id, f"<b>{escape(category.label)}</b>",
                                   reply_markup=menu.category_keyboard(value))
        elif kind == "cmd":
            self._handle_command(chat_id, "/" + value)
        else:
            self._api.send_message(chat_id, f"<i>{escape(value)}</i>")
            self._handle_text(chat_id, value, from_menu=True)

    def _handle_voice(self, chat_id: int, voice: dict[str, Any]) -> None:
        """Голосовое → текст через шлюз → обычная реплика.

        Расшифровку показываем: человек видит, что бот услышал, и сразу
        замечает ошибку распознавания в имени или сумме.
        """
        if (voice.get("duration") or 0) > MAX_VOICE_SECONDS:
            self._api.send_message(
                chat_id, f"Голосовое длиннее {MAX_VOICE_SECONDS // 60} мин — разбейте на части."
            )
            return
        if (voice.get("file_size") or 0) > 20 * 1024 * 1024:
            self._api.send_message(chat_id, "Файл больше 20 МБ — Telegram не даёт боту его скачать.")
            return
        self._api.send_chat_action(chat_id, "typing")
        try:
            meta = self._api.get_file(voice["file_id"])
            data = self._api.download_file(meta["file_path"])
            mime = voice.get("mime_type") or "audio/ogg"
            name = voice.get("file_name") or ("voice.ogg" if "ogg" in mime else "audio")
            text, _model = stt.transcribe(data, name, mime)
        except (TelegramError, stt.STTError) as exc:
            self._api.send_message(chat_id, "❌ " + escape(str(exc)))
            return
        if not text:
            self._api.send_message(chat_id, "Не расслышал речи в голосовом — повторите, пожалуйста.")
            return
        self._api.send_message(chat_id, f"🎤 <i>{escape(text)}</i>")
        # Как нажатие кнопки: голосовое не может быть кодом авторизации Google.
        # Пометка для модели: имена и суммы могли быть распознаны неточно.
        self._handle_text(chat_id, "[Голосовое] " + text, from_menu=True)

    def _handle_text(self, chat_id: int, text: str, from_menu: bool = False) -> None:
        """Реплика пользователя — набранная или отправленная кнопкой меню."""
        state = self._states.setdefault(chat_id, ChatState())

        # Ожидаем код авторизации Google после /auth. Нажатие кнопки меню
        # значит, что от авторизации отказались, — ожидание снимаем молча.
        if state.oauth_started_at is not None:
            if not from_menu:
                self._handle_oauth_code(chat_id, state, text)
                return
            self._clear_oauth(state)

        # Ожидаем текст правок к отклонённому действию.
        if state.awaiting_comment_for:
            tool_use_id = state.awaiting_comment_for
            state.awaiting_comment_for = None
            state.comments[tool_use_id] = text
            self._maybe_resume(chat_id)
            return

        if state.cards:
            self._api.send_message(
                chat_id,
                "Сначала ответьте на запрос подтверждения кнопками выше.",
            )
            return

        session = store.get_or_create(f"tg-{chat_id}")
        self._run_turn(chat_id, self._agent.send_user_message(session, text))

    def _handle_command(self, chat_id: int, text: str, first_name: str = "") -> None:
        command = text.split()[0].lower().lstrip("/").split("@")[0]

        if command == "start":
            # Первое сообщение ставит клавиатуру под полем ввода, второе —
            # разделы инлайн-кнопками и кнопку пульта.
            self._api.send_message(
                chat_id,
                escape(_hello(first_name) + "\n\n" + WELCOME.format(org=settings.org_name)),
                reply_markup=menu.reply_keyboard(),
            )
            self._api.send_message(chat_id, escape(menu.ROOT_TEXT),
                                   reply_markup=menu.root_keyboard(miniapp_url()))
        elif command == "help":
            self._api.send_message(
                chat_id, escape(GREETING.format(org=settings.org_name)),
                reply_markup=menu.root_keyboard(),
            )
        elif command == "menu":
            self._api.send_message(chat_id, escape(menu.ROOT_TEXT), reply_markup=menu.reply_keyboard())
            self._api.send_message(chat_id, "Разделы:", reply_markup=menu.root_keyboard(miniapp_url()))
        elif command == "new":
            store.reset(f"tg-{chat_id}")
            self._states.pop(chat_id, None)
            self._api.send_message(chat_id, "Диалог очищен. Слушаю.")
        elif command == "status":
            self._api.send_message(chat_id, self._status_text(), parse_mode="HTML")
        elif command == "auth":
            argument = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            if argument and "@" in argument and google_sa.enabled():
                self._handle_sa_auth(chat_id, text)  # «/auth почта» — общий доступ через сервисный аккаунт
            elif google_oauth.configured() or not google_sa.enabled():
                self._handle_auth(chat_id)  # вход через Google — основной способ
            else:
                self._handle_sa_auth(chat_id, text)
        elif command == "app":
            self._handle_app(chat_id)
        elif command == "calendars":
            self._handle_calendars(chat_id, text)
        elif command in {"adduser", "removeuser", "users"}:
            self._handle_users(chat_id, command, text)
        elif command == "check":
            self._handle_check(chat_id)
        elif command == "routerai":
            from .. import routerai

            report = routerai.raw_report()
            for part in split_message(report, limit=3500):
                self._api.send_message(chat_id, f"<pre>{escape(part)}</pre>", parse_mode="HTML")
        else:
            self._api.send_message(
                chat_id, "Неизвестная команда. Есть /menu, /new, /status, /auth, /calendars, /app, /check, /users, /help."
            )

    # --- Mini App ---

    def _install_menu_button(self, users: Any = None) -> None:
        """Список команд и кнопка «Открыть» — только тем, кто в белом списке.

        Показывать их всем по умолчанию значило бы сообщить посторонним, что
        бот умеет больше, чем молчать. Сбой здесь не мешает работе бота.

        Пока человек не нажал «Старт», Telegram отвечает «chat not found»:
        такому ставим меню при первом его сообщении (_ensure_menu).
        """
        url = miniapp_url()
        for user_id in sorted(self._allowed if users is None else users):
            steps = [("команды", lambda uid=user_id: self._api.set_my_commands(menu.BOT_COMMANDS, uid))]
            if url:
                steps.append(("кнопку Mini App", lambda uid=user_id: self._api.set_chat_menu_button(uid, "Открыть", url)))
            not_started = False
            for name, step in steps:
                try:
                    step()
                except Exception as exc:  # noqa: BLE001 — меню необязательно
                    if "chat not found" in str(exc):
                        not_started = True
                        break
                    logger.warning("Не удалось задать %s для %s: %s", name, user_id, exc)
            if not_started:
                logger.info("Меню для %s поставлю, когда он откроет бота и нажмёт «Старт»", user_id)
            else:
                self._menu_ready.add(user_id)

    def _ensure_menu(self, user_id: int) -> None:
        if user_id not in self._menu_ready:
            self._install_menu_button({user_id})

    def _handle_menu(self, callback: dict[str, Any], chat_id: int, data: str) -> None:
        """Кнопки меню: раздел → действия → запрос ассистенту или команда."""
        message_id = callback["message"]["message_id"]
        parts = data.split(":", 2)

        if data == "m:root":
            self._api.answer_callback_query(callback["id"])
            self._api.edit_message_text(chat_id, message_id, escape(menu.ROOT_TEXT),
                                        reply_markup=menu.root_keyboard())
            return

        if len(parts) == 3 and parts[1] == "c":
            category = menu.category(parts[2])
            if category is None:
                self._api.answer_callback_query(callback["id"], "Раздел не найден.")
                return
            self._api.answer_callback_query(callback["id"])
            self._api.edit_message_text(chat_id, message_id, f"<b>{escape(category.label)}</b>",
                                        reply_markup=menu.category_keyboard(category.id))
            return

        action = menu.action(parts[2]) if len(parts) == 3 and parts[1] == "a" else None
        if action is None:
            self._api.answer_callback_query(callback["id"], "Кнопка устарела — откройте /menu.")
            return

        self._api.answer_callback_query(callback["id"], action.label)
        if action.kind == "hint":
            self._api.send_message(chat_id, escape(action.payload))
        elif action.kind == "cmd":
            self._handle_command(chat_id, "/" + action.payload)
        else:
            # Показываем, что именно спросили: ответ без вопроса читается хуже.
            self._api.send_message(chat_id, f"<i>{escape(action.payload)}</i>")
            self._handle_text(chat_id, action.payload, from_menu=True)

    def _handle_check(self, chat_id: int) -> None:
        """Самопроверка прямо в чате — когда под рукой нет компьютера."""
        from .. import selfcheck

        self._api.send_message(chat_id, "🔎 Проверяю модель, баланс, Google и поиск… до минуты.")
        self._api.send_chat_action(chat_id)
        report = selfcheck.render_telegram(selfcheck.run_groups())
        for part in split_message(report):
            self._api.send_message(chat_id, part, parse_mode="HTML")

    def _handle_app(self, chat_id: int) -> None:
        url = miniapp_url()
        if not url:
            self._api.send_message(
                chat_id,
                "Приложение открывается только по https. Задайте OPERON_PUBLIC_URL "
                "(адрес проекта в Amvera, https://…) и перезапустите.",
            )
            return
        self._api.send_message(
            chat_id,
            "Пульт: встречи на сегодня, поручения, документы «Кванта», выбор модели и расходы.",
            reply_markup={"inline_keyboard": [[{"text": "📱 Открыть пульт", "web_app": {"url": url}}]]},
        )

    # --- приём документов ---

    def _handle_document(self, chat_id: int, document: dict[str, Any], caption: str) -> None:
        """Скачивает присланный файл, разбирает и спрашивает, класть ли в базу.

        Файл в базе знаний — это изменение данных, поэтому решение принимает
        человек, как и по любому другому изменению.
        """
        state = self._states.setdefault(chat_id, ChatState())
        if state.cards or state.pending_file:
            self._api.send_message(chat_id, "Сначала ответьте на запрос выше.")
            return

        name = document.get("file_name") or "документ"
        size = document.get("file_size") or 0
        if size > intake.MAX_FILE_BYTES:
            self._api.send_message(
                chat_id,
                escape(
                    f"«{name}» весит {size // (1024 * 1024)} МБ — Telegram не отдаёт "
                    "ботам файлы больше 20 МБ. Загрузите его через панель Amvera."
                ),
            )
            return

        self._api.send_chat_action(chat_id, "typing")
        try:
            meta = self._api.get_file(document["file_id"])
            data = self._api.download_file(meta["file_path"])
        except TelegramError as exc:
            self._api.send_message(chat_id, "❌ " + escape(str(exc)))
            return

        try:
            prepared = intake.prepare(data, name, category=caption)
        except intake.IntakeError as exc:
            self._api.send_message(chat_id, "❌ " + escape(str(exc)))
            return

        state.pending_file = prepared
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ В базу знаний", "callback_data": "f:add"},
                    {"text": "✕ Не сохранять", "callback_data": "f:no"},
                ]
            ]
        }
        lines = [
            "<b>Документ разобран</b>",
            f"<b>Файл:</b> {escape(prepared.original_name)}",
            f"<b>Размер:</b> {prepared.size // 1024} КБ, извлечено {prepared.characters} символов",
            f"<b>Путь в базе:</b> {escape(prepared.relative_path)}",
            "",
            "<b>Начало текста:</b>",
            f"<pre>{escape(prepared.preview[:350])}</pre>",
            "",
            "<i>Сохранить в базу знаний? Без ответа файл не сохраняется.</i>",
        ]
        self._api.send_message(chat_id, "\n".join(lines), reply_markup=keyboard)

    def _handle_file_decision(self, callback: dict[str, Any], chat_id: int, verdict: str) -> None:
        state = self._states.get(chat_id)
        prepared = state.pending_file if state else None
        if prepared is None:
            self._api.answer_callback_query(callback["id"], "Файл уже обработан.")
            return

        state.pending_file = None
        message_id = callback["message"]["message_id"]

        if verdict != "add":
            self._api.answer_callback_query(callback["id"], "Файл не сохранён")
            self._api.edit_message_text(
                chat_id, message_id,
                f"<b>{escape(prepared.original_name)}</b>\n✕ Не сохранён",
                reply_markup={"inline_keyboard": []},
            )
            return

        try:
            intake.save(prepared)
        except intake.IntakeError as exc:
            self._api.answer_callback_query(callback["id"], "Ошибка")
            self._api.send_message(chat_id, "❌ " + escape(str(exc)))
            return

        stats = knowledge_base.stats
        self._api.answer_callback_query(callback["id"], "Добавлено")
        self._api.edit_message_text(
            chat_id, message_id,
            f"<b>{escape(prepared.original_name)}</b>\n"
            f"✅ В базе знаний: {escape(prepared.relative_path)}\n"
            f"Документов всего: {stats['documents']}",
            reply_markup={"inline_keyboard": []},
        )

    # --- подключение Google ---

    def _handle_users(self, chat_id: int, command: str, text: str) -> None:
        """Доступ к боту из чата: /users, /adduser ID, /removeuser ID.

        Управляют только люди из TELEGRAM_ALLOWED_USERS — добавленный командой
        раздавать доступ дальше не может.
        """
        user_id = getattr(self, "_current_user", None)
        if user_id not in self._allowed:
            self._api.send_message(chat_id, "Управлять доступом могут только владельцы бота.")
            return
        added = access.extra()
        if command == "users":
            lines = ["<b>Доступ к боту</b>", "Владельцы (TELEGRAM_ALLOWED_USERS в настройках сервера):"]
            lines += [f"• <code>{uid}</code>" for uid in sorted(self._allowed)]
            lines.append("Добавлены командой:" if added else "Добавленных командой нет.")
            lines += [f"• <code>{uid}</code>" for uid in sorted(added)]
            lines.append("\nДобавить: /adduser ID · убрать: /removeuser ID")
            self._api.send_message(chat_id, "\n".join(lines))
            return
        parts = text.split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            self._api.send_message(
                chat_id, f"Укажите Telegram ID цифрами: /{command} 159893732\n"
                "Свой ID человек узнаёт у @userinfobot."
            )
            return
        target = int(parts[1])
        if command == "adduser":
            if access.add(target, user_id):
                logger.warning("Пользователь %s добавлен в бот пользователем %s", target, user_id)
                self._api.send_message(
                    chat_id, f"✅ <code>{target}</code> добавлен. Пусть откроет бота и нажмёт «Старт»."
                )
            else:
                self._api.send_message(chat_id, f"<code>{target}</code> уже есть в списке.")
            return
        if target in self._allowed:
            self._api.send_message(
                chat_id, "Это владелец из настроек сервера — убрать его можно только там "
                "(TELEGRAM_ALLOWED_USERS в bot.env)."
            )
        elif access.remove(target):
            logger.warning("Пользователь %s удалён из бота пользователем %s", target, user_id)
            self._menu_ready.discard(target)
            self._api.send_message(chat_id, f"Доступ для <code>{target}</code> закрыт.")
        else:
            self._api.send_message(chat_id, f"<code>{target}</code> в списке не было.")

    def _handle_calendars(self, chat_id: int, text: str) -> None:
        """/calendars — какие календари читает бот; add/remove — для открытого календаря."""
        from ..tools import calendar as calendar_tool

        parts = text.split()
        action = parts[1].lower() if len(parts) > 1 else ""
        target = parts[2] if len(parts) > 2 else ""
        if action in {"add", "добавить"}:
            try:
                access = google_sa.add_calendar(target)
            except google_sa.LinkError as exc:
                self._api.send_message(chat_id, "❌ " + escape(str(exc)))
                return
            except Exception as exc:  # noqa: BLE001 — сеть или Google
                self._api.send_message(chat_id, "❌ Не удалось проверить календарь: " + escape(str(exc)[:300]))
                return
            note = "" if access in {"writer", "owner"} else " Только чтение — встречи в нём создавать нельзя."
            self._api.send_message(chat_id, f"✅ Календарь <code>{escape(target)}</code> добавлен.{escape(note)}")
            return
        if action in {"remove", "убрать", "удалить"}:
            done = google_sa.remove_calendar(target)
            self._api.send_message(
                chat_id, f"Календарь <code>{escape(target)}</code> {'убран' if done else 'не был добавлен'}."
            )
            return
        try:
            calendars = calendar_tool.readable_calendars()
        except Exception as exc:  # noqa: BLE001 — Google не подключён или недоступен
            self._api.send_message(chat_id, "❌ " + escape(str(exc)[:300]) + "\nПодключите Google: /auth")
            return
        lines = ["<b>Календари, которые читает бот</b>"]
        lines += [
            f"• {escape(c.get('name') or c['id'])}" + (" (основной)" if c.get("primary") and c.get("name") != "основной" else "")
            for c in calendars
        ]
        if google_client.mode() == "shared":
            lines.append(
                "\nДругой календарь (рабочий, общий): откройте его адресу "
                f"<code>{escape(google_sa.email())}</code> и отправьте\n"
                "/calendars add <i>идентификатор</i>\n"
                "Идентификатор — в настройках календаря, раздел «Интеграция календаря»."
            )
        else:
            # Список получен не полностью — это и есть ответ на «не вижу рабочий
            # календарь». Показывать один основной и молчать нельзя.
            problem = calendar_tool.calendars_problem()
            if problem:
                lines.append(
                    "\n⚠️ <b>Список календарей аккаунта получить не удалось</b>, поэтому видно "
                    "только основной. Встречи из рабочих и общих календарей в ответы не попадают.\n"
                    "Обычно так бывает, когда вход выполнен до появления нужных прав: "
                    "отправьте /auth и разрешите доступ заново."
                )
                logger.info("Список календарей недоступен: %s", problem)
            else:
                lines.append("\nВстречи берутся из всех этих календарей. Нового календаря нет в списке — "
                             "проверьте, что он виден в Google Календаре этого аккаунта, или пройдите /auth заново.")
        self._api.send_message(chat_id, "\n".join(lines))

    def _handle_sa_auth(self, chat_id: int, text: str) -> None:
        """Google через сервисный аккаунт: «/auth почта» — и всё, без OAuth-экранов."""
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            current = google_sa.current_link()
            prefix = (
                f"Сейчас подключено: {google_sa.describe_link(current)}.\n"
                "Чтобы сменить почту — /auth новая@почта.\n\n"
                if current else ""
            )
            self._api.send_message(
                chat_id,
                escape(prefix + "Отправьте /auth и свою почту Google, например:\n/auth kirill@gmail.com\n\n"
                       + google_sa.instructions()),
            )
            return
        self._api.send_chat_action(chat_id, "typing")
        try:
            link = google_sa.connect(parts[1])
        except google_sa.LinkError as exc:
            self._api.send_message(chat_id, "❌ " + escape(str(exc)))
            return
        except Exception as exc:  # noqa: BLE001 — сеть или Google: сказать прямо
            self._api.send_message(chat_id, "❌ Не удалось проверить доступ: " + escape(str(exc)[:300]))
            return
        google_client.reset_cache()
        lines = ["✅ <b>Google подключён</b>", escape(google_sa.describe_link(link))]
        if link.get("mode") == "shared":
            lines.append(
                "\nПриглашения участникам в этом режиме не рассылаются — участники пишутся в описание "
                "встречи. Документы видны те, что открыты боту на Диске."
            )
            if link.get("access") == "reader":
                lines.append("⚠️ Календарь открыт только на чтение — создавать встречи не получится. "
                             "Поменяйте право на «Вносить изменения в мероприятия».")
        self._api.send_message(chat_id, "\n".join(lines))

    def _handle_auth(self, chat_id: int) -> None:
        """Выдаёт ссылку авторизации и переводит чат в ожидание кода.

        Без этой команды переавторизация означала бы ручной перенос файла
        токена на сервер — то есть в реальности не делалась бы никогда.
        """
        state = self._states.setdefault(chat_id, ChatState())
        if state.cards or state.awaiting_comment_for:
            self._api.send_message(
                chat_id, "Сначала закройте запрос подтверждения выше, потом /auth."
            )
            return

        # Проверяем ссылку у Google до того, как отдать её человеку: ссылка,
        # которая закончится «Доступ заблокирован», хуже, чем никакой.
        ok, problem = google_oauth.preflight()
        if not ok:
            logger.warning("Вход через Google не пройдёт: %s", problem)
            text = "❌ Вход через Google сейчас не сработает — это настройка Google Cloud, а не ваша ошибка.\n\n" + problem
            if google_sa.enabled():
                text += (
                    "\n\nПока можно подключить без входа — открыть свой календарь боту:\n"
                    + google_sa.instructions()
                )
            self._api.send_message(chat_id, escape(text))
            return

        try:
            url, oauth_state = google_oauth.start(label=f"telegram:{chat_id}")
        except google_oauth.OAuthError as exc:
            self._api.send_message(chat_id, "❌ " + escape(str(exc)))
            return

        state.oauth_started_at = time.monotonic()
        state.oauth_state = oauth_state
        current = google_client.status()
        prefix = (
            f"Google уже подключён ({escape(current.get('account_hint', '') or 'учётная запись определена')}). "
            "Новая авторизация заменит текущий доступ.\n\n"
            if current.get("connected")
            else ""
        )
        self._api.send_message(
            chat_id,
            f"{prefix}<b>Подключение Google</b>\n\n"
            f'<a href="{escape(url)}">Открыть страницу доступа</a>\n\n'
            + escape(google_oauth.instructions()),
            reply_markup={"inline_keyboard": [[{"text": "🔐 Войти через Google", "url": url}]]},
        )

    def _handle_oauth_code(self, chat_id: int, state: ChatState, text: str) -> None:
        if not google_oauth.looks_like_code(text):
            self._clear_oauth(state)
            self._api.send_message(
                chat_id,
                "Это не похоже на код авторизации — режим ожидания снят, "
                "сообщение не обработано. Повторите /auth, если хотели подключить Google.",
            )
            return

        oauth_state = state.oauth_state
        try:
            # Ссылку забываем только после обмена: в ней секрет PKCE, без него
            # Google код не примет.
            result = google_oauth.exchange_code(text, state=oauth_state)
        except google_oauth.OAuthError as exc:
            self._api.send_message(chat_id, "❌ " + escape(str(exc)))
            return
        finally:
            self._clear_oauth(state)

        self._report_connected(chat_id, result)

    @staticmethod
    def _clear_oauth(state: ChatState) -> None:
        if state.oauth_state:
            google_oauth.forget(state.oauth_state)
        state.oauth_started_at = None
        state.oauth_state = ""

    def _report_connected(self, chat_id: int, result: dict[str, Any]) -> None:
        lines = ["✅ <b>Google подключён</b>"]
        if result.get("account"):
            lines.append(f"Учётная запись: {escape(result['account'])}")
        lines.append(
            "Токен сохранён "
            + (
                "в зашифрованном виде."
                if result["encrypted"]
                else "БЕЗ шифрования — задайте OPERON_TOKEN_KEY."
            )
        )
        lines.append("")
        lines.append("Выданные разрешения:")
        lines.extend(f"• {escape(s)}" for s in result["scopes"])
        if result["missing_scopes"]:
            lines.append("")
            lines.append("⚠️ НЕ выданы (часть функций не заработает):")
            lines.extend(f"• {escape(s)}" for s in result["missing_scopes"])
        self._api.send_message(chat_id, "\n".join(lines))

    def _status_text(self) -> str:
        kb = knowledge_base.stats
        google = google_client.status()
        lines = [
            f"<b>Ассистент {escape(settings.org_name)}</b>",
            f"Модель: {escape(current_model())} ({escape(settings.provider)})",
            f"База знаний: {kb['documents']} документов"
            + (f" ({escape(', '.join(kb['categories']))})" if kb["categories"] else ""),
        ]
        if google.get("connected"):
            lines.append(f"Google: подключён {escape(google.get('account_hint', ''))}")
        else:
            lines.append(f"Google: не подключён — {escape(str(google.get('reason', '')))}")
            client = google_oauth.describe_client()
            lines.append(f"OAuth-клиент: {escape(client['source'])}")
            if client["configured"]:
                lines.append("Подключить: /auth")

        from ..tools import registry

        has_search = "internet_search" in registry.names()
        lines.append("Интернет: " + ("доступен" if has_search or settings.web_search_enabled else "нет ключа поиска"))

        rem = reminders.describe()
        if rem["enabled"]:
            lines.append(
                f"Напоминания: сводка в {rem['digest_hour']}:00, "
                f"предупреждение за {rem['remind_before_days']} дн., "
                + (
                    f"о встречах за {rem['meeting_remind_minutes']} мин, "
                    if rem["meeting_remind_minutes"] > 0
                    else ""
                )
                + f"тишина {rem['quiet_hours']}"
            )
        else:
            lines.append("Напоминания: выключены")
        return "\n".join(lines)

    # --- ход агента ---

    def _run_turn(self, chat_id: int, events: Any) -> None:
        self._api.send_chat_action(chat_id)
        placeholder = self._api.send_message(chat_id, THINKING, parse_mode=None)
        message_id = placeholder["message_id"]

        answer: list[str] = []
        activity: list[str] = []
        notices: list[str] = []
        pending_actions: list[dict[str, Any]] = []
        attachments: list[dict[str, Any]] = []
        last_edit = 0.0

        for event in events:
            kind = event["type"]

            if kind == "text_delta":
                answer.append(event["text"])
            elif kind == "tool_start":
                activity.append(event.get("activity", event["name"]))
                # Показываем прогресс, но не чаще раза в две секунды: у Telegram
                # жёсткие лимиты на редактирование сообщений.
                if time.monotonic() - last_edit > 2:
                    last_edit = time.monotonic()
                    self._api.edit_message_text(
                        chat_id, message_id, f"⏳ {escape(activity[-1])}…", parse_mode="HTML"
                    )
            elif kind == "tool_declined":
                activity.append(event["summary"])
            elif kind in {"error", "warning"}:
                notices.append(("⚠️ " if kind == "warning" else "❌ ") + event["message"])
            elif kind == "confirmation_required":
                pending_actions = event["actions"]
            elif kind == "attachment":
                attachments.append(event)

        text = "".join(answer).strip()
        body = to_telegram_html(text) if text else ""
        if notices:
            body = (body + "\n\n" if body else "") + "\n".join(escape(n) for n in notices)
        if not body:
            body = escape("Готово.") if not pending_actions else escape("Требуется подтверждение:")

        chunks = split_message(body)
        self._api.edit_message_text(chat_id, message_id, chunks[0])
        for chunk in chunks[1:]:
            self._api.send_message(chat_id, chunk)

        for attachment in attachments:
            meta = outbox.get(attachment["id"])
            if meta is None:
                continue
            try:
                self._api.send_document(chat_id, meta["path"], meta["name"])
            except TelegramError as exc:
                self._api.send_message(chat_id, "❌ " + escape(str(exc)))

        if pending_actions:
            self._send_confirmations(chat_id, pending_actions)

    # --- подтверждения ---

    def _send_confirmations(self, chat_id: int, actions: list[dict[str, Any]]) -> None:
        state = self._states.setdefault(chat_id, ChatState())

        for action in actions:
            token = secrets.token_urlsafe(6)
            card = self._render_card(action)
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Подтвердить", "callback_data": f"a:{token}"},
                        {"text": "✏️ Правки", "callback_data": f"e:{token}"},
                        {"text": "✕ Отмена", "callback_data": f"r:{token}"},
                    ]
                ]
            }
            sent = self._api.send_message(chat_id, card, reply_markup=keyboard)
            state.cards[token] = PendingCard(
                chat_id=chat_id,
                message_id=sent["message_id"],
                tool_use_id=action["tool_use_id"],
                title=action.get("title", action["name"]),
            )

    @staticmethod
    def _render_card(action: dict[str, Any]) -> str:
        lines = [
            f"<b>{escape(action.get('title', action['name']))}</b>",
            escape(action.get("summary", "")),
        ]
        details = action.get("details") or {}
        if details:
            lines.append("")
            for key, value in details.items():
                shown = value if isinstance(value, str) else str(value)
                if len(shown) > 600:
                    shown = shown[:600] + "…"
                lines.append(f"<b>{escape(str(key))}:</b> {escape(shown)}")
        lines.append("")
        ttl = settings.confirmation_ttl_minutes
        lines.append(
            "<i>Действие выполнится только после подтверждения."
            + (f" Без ответа за {ttl} мин — отмена.</i>" if ttl > 0 else "</i>")
        )
        return "\n".join(line for line in lines if line is not None)

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        data = callback.get("data", "")
        chat_id = callback["message"]["chat"]["id"]
        if data.startswith("m:"):
            self._handle_menu(callback, chat_id, data)
            return
        if data.startswith("u:add:"):
            self._grant_from_button(callback, chat_id, data)
            return
        state = self._states.get(chat_id)

        if not state or ":" not in data:
            self._api.answer_callback_query(callback["id"], "Запрос устарел.")
            return

        prefix, token = data.split(":", 1)

        if prefix == "f":
            self._handle_file_decision(callback, chat_id, token)
            return

        card = state.cards.get(token)
        if card is None:
            self._api.answer_callback_query(callback["id"], "Это подтверждение уже обработано.")
            return

        verdicts = {
            "a": ("approve", "✅ Подтверждено", "Выполняю"),
            "r": ("reject", "✕ Отменено", "Действие не выполнено"),
            "e": ("reject", "✏️ Отправлено на правки", "Напишите, что изменить"),
        }
        if prefix not in verdicts:
            self._api.answer_callback_query(callback["id"], "Неизвестная кнопка.")
            return

        decision, mark, toast = verdicts[prefix]
        state.decisions[card.tool_use_id] = decision
        del state.cards[token]

        self._api.answer_callback_query(callback["id"], toast)
        self._api.edit_message_text(
            chat_id,
            card.message_id,
            f"<b>{escape(card.title)}</b>\n{escape(mark)}",
            reply_markup={"inline_keyboard": []},
        )

        if prefix == "e":
            state.awaiting_comment_for = card.tool_use_id
            self._api.send_message(chat_id, "Напишите, что нужно изменить.")
            return

        self._maybe_resume(chat_id)

    def sweep_expired(self) -> None:
        """Гасит просроченные карточки и ожидания кода.

        «Нет ответа = отказ» должно наступать само: пользователь может закрыть
        Telegram и не вернуться, а замороженный ход не отпустит диалог, пока
        кто-то его не закроет.
        """
        for chat_id in list(self._states):
            state = self._states.get(chat_id)
            if state is None:
                continue

            # Клиент типа Web возвращает код прямо на сервер: результат
            # появляется сам, пользователю писать в чат нечего.
            if state.oauth_state:
                entry = google_oauth.take_result(state.oauth_state)
                if entry is not None:
                    self._clear_oauth(state)
                    if entry.error:
                        self._api.send_message(chat_id, "❌ " + escape(entry.error))
                    elif entry.result is not None:
                        self._report_connected(chat_id, entry.result)
                    continue

            if state.oauth_expired:
                self._clear_oauth(state)
                self._api.send_message(
                    chat_id,
                    f"Ответ Google так и не пришёл за {settings.oauth_wait_minutes} мин — "
                    "ожидание снято. Начните заново: /auth",
                )

            session = store.get(f"tg-{chat_id}")
            if session is None or session.pending is None or not session.pending.expired:
                continue

            for card in state.cards.values():
                self._api.edit_message_text(
                    chat_id,
                    card.message_id,
                    f"<b>{escape(card.title)}</b>\n{escape(EXPIRED_MARK)}",
                    reply_markup={"inline_keyboard": []},
                )

            decisions = dict(state.decisions)
            comments = dict(state.comments)
            state.cards.clear()
            state.decisions.clear()
            state.comments.clear()
            state.awaiting_comment_for = None
            logger.info("Чат %s: подтверждение просрочено, действия отменены", chat_id)
            self._run_turn(chat_id, self._agent.expire_pending(session, decisions, comments))

    def send_reminders(self) -> None:
        """Отправляет то, что бот должен сказать сам, без вопроса пользователя.

        Получателями считаем весь белый список: он и задуман как «те, кому
        этот ассистент принадлежит». Отправленное помечается только после
        успешной отправки — иначе сбой связи проглотил бы напоминание молча.
        """
        # Сводка у каждого своя: поручения общие, а встречи дня — из его
        # собственного календаря. Общая сводка разослала бы всем встречи владельца.
        for user_id in sorted(self._allowed | frozenset(access.extra())):
            with accounts.use(user_id):
                plan = reminders.pending(account=str(user_id))
                if not plan:
                    continue
                delivered: list[reminders.Reminder] = []
                for reminder in plan:
                    if not reminder.text:
                        # Пустая сводка: отмечаем как обработанную, но не пишем.
                        # Ежедневное «всё в порядке» перестают читать.
                        delivered.append(reminder)
                        continue
                    try:
                        for chunk in split_message(reminder.text):
                            self._api.send_message(user_id, chunk)
                        delivered.append(reminder)
                    except TelegramError as exc:
                        # Обычная причина — пользователь не открывал диалог с ботом.
                        logger.warning("Напоминание не доставлено %s: %s", user_id, exc)
                if delivered:
                    reminders.mark_sent(delivered)
                    logger.info("Напоминаний для %s: %s", user_id, sum(1 for r in delivered if r.text))

    def _maybe_resume(self, chat_id: int) -> None:
        """Продолжает ход, когда решения приняты по всем карточкам."""
        state = self._states.get(chat_id)
        if state is None or state.cards or state.awaiting_comment_for:
            return

        decisions = dict(state.decisions)
        comments = dict(state.comments)
        state.decisions.clear()
        state.comments.clear()

        session = store.get(f"tg-{chat_id}")
        if session is None or session.pending is None:
            return
        if session.pending.expired:
            # Кнопку нажали, но срок уже вышел: закрываем ход отказом, а не
            # тишиной — иначе диалог останется замороженным навсегда.
            self._run_turn(chat_id, self._agent.expire_pending(session, decisions, comments))
            return
        self._run_turn(chat_id, self._agent.resume_with_decisions(session, decisions, comments))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    from .supervisor import TelegramSupervisor

    supervisor = TelegramSupervisor()
    if not settings.telegram_token:
        logger.error("Не задан TELEGRAM_BOT_TOKEN")
        return 1
    if not settings.telegram_allowed_users:
        logger.error("Не задан TELEGRAM_ALLOWED_USERS — без белого списка бот не запускается")
        return 1
    try:
        # Отдельный процесс — тот же надзор: сбой не должен оставлять без бота.
        supervisor.run_forever()
    except KeyboardInterrupt:
        supervisor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
