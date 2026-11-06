"""HTTP-слой: чат по SSE, подтверждение действий, статус интеграций."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .tools import web as web_tools
from . import auth, dashboard, model_choice, model_families, routerai
from .agent import agent
from .config import env_source, BASE_DIR, settings
from .integrations import accounts, google_client, google_oauth
from .kb import knowledge_base
from .sessions import store
from .telegram import webapp
from .telegram.format import escape
from .tools import outbox, registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("operon")
# httpx пишет каждый запрос с полным адресом, а в адресе Telegram — токен бота.
# В консоль и журналы его выводить незачем.
logging.getLogger("httpx").setLevel(logging.WARNING)

STATIC_DIR = BASE_DIR / "static"


# Бот живёт фоновым потоком того же процесса (на Amvera процесс один), но не
# сам по себе: за перезапуском после сбоя следит надзор, он же отвечает на
# вопрос «бот жив?» в /api/health.
from .telegram.supervisor import supervisor as telegram


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _startup_report()
    telegram.start()
    yield
    telegram.stop()


app = FastAPI(title=f"Ассистент {settings.org_name}", version="1.0.0", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32_000)
    session_id: str | None = None


class Decision(BaseModel):
    tool_use_id: str
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = ""


class ConfirmRequest(BaseModel):
    session_id: str
    decisions: list[Decision]


class ResetRequest(BaseModel):
    session_id: str | None = None


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class TelegramAuthRequest(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)


class ModelRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)


# Пути, доступные без входа: проверка живости, сама страница входа и статика.
# Сюда же адрес возврата OAuth: на него браузер приводит Google, куки нашего
# приложения там может не быть. Защищает не вход, а одноразовый state —
# обменять можно только код по ранее выданной ссылке.
# Mini App: страница открыта, но всё, что она запрашивает, требует токена,
# полученного обменом подписанных Telegram данных (/api/telegram/auth).
PUBLIC_PATHS = {
    "/api/health",
    "/api/login",
    "/login",
    "/favicon.ico",
    "/oauth2/callback",
    "/miniapp",
    "/api/telegram/auth",
}


def _oauth_page(title: str, body: str, ok: bool) -> HTMLResponse:
    """Страница, которую увидит пользователь после согласия Google."""
    colour = "#0f766e" if ok else "#b91c1c"
    return HTMLResponse(
        f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title></head>
<body style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
background:#f8fafc;color:#0f172a;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0;padding:24px">
<main style="max-width:520px;background:#fff;border-radius:14px;padding:32px;
box-shadow:0 1px 3px rgba(0,0,0,.1)">
<h1 style="margin:0 0 12px;font-size:20px;color:{colour}">{escape(title)}</h1>
<p style="margin:0;line-height:1.6;white-space:pre-wrap">{escape(body)}</p>
</main></body></html>""",
        status_code=200 if ok else 400,
    )


@app.get("/oauth2/callback")
def oauth_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    """Принимает ответ Google и сохраняет токен — без копирования кода вручную."""
    if error:
        google_oauth.forget(state)
        # Причину разбирает тот же словарь, что и в боте: человек видит на
        # странице не «access_denied», а что именно случилось и что делать.
        return _oauth_page(
            "Доступ не выдан",
            google_oauth.describe_denied(error) + "\n\nВернитесь в бот и повторите /auth.",
            ok=False,
        )
    if not code:
        return _oauth_page(
            "Кода нет",
            "Google не передал код авторизации. Повторите /auth в боте.",
            ok=False,
        )

    try:
        result = google_oauth.handle_callback(code, state)
    except google_oauth.OAuthError as exc:
        return _oauth_page("Не удалось подключить Google", str(exc), ok=False)

    account = result.get("account") or "учётная запись определена"
    missing = result.get("missing_scopes") or []
    tail = (
        "\n\nВНИМАНИЕ: часть разрешений не выдана — "
        + ", ".join(missing)
        + ". Часть функций не заработает."
        if missing
        else ""
    )
    return _oauth_page(
        "Google подключён",
        f"Доступ выдан: {account}.\nТокен сохранён на сервере"
        + (" в зашифрованном виде." if result.get("encrypted") else " БЕЗ шифрования.")
        + "\n\nМожно закрыть вкладку и вернуться в бот."
        + tail,
        ok=True,
    )


def _sse(events: Iterator[dict[str, Any]]) -> Iterator[str]:
    """Оборачивает поток событий агента в Server-Sent Events."""
    try:
        for event in events:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    except Exception as exc:  # noqa: BLE001 — иначе клиент зависнет на открытом стриме
        logger.exception("Сбой в потоке агента")
        payload = {
            "type": "error",
            "message": f"Внутренняя ошибка агента: {exc.__class__.__name__}: {exc}",
        }
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'stop': 'error'}, ensure_ascii=False)}\n\n"


def _stream_response(events: Iterator[dict[str, Any]]) -> StreamingResponse:
    return StreamingResponse(
        _sse(events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    """Пропускает запрос только с действующей кукой, если задан пароль."""
    path = request.url.path
    # /api/files/<id>: ссылка-ключ на готовый документ (id — 24 случайных знака,
    # живёт неделю). Без неё не скачать файл из Mini App: загрузчик Telegram
    # не передаёт заголовок авторизации.
    if (
        not settings.auth_required
        or path in PUBLIC_PATHS
        or path.startswith("/static/")
        or path.startswith("/api/files/")
    ):
        return await call_next(request)

    if auth.token_is_valid(request.cookies.get(auth.COOKIE_NAME)):
        return await call_next(request)
    # Mini App носит токен в заголовке: в веб-версии Telegram он работает во
    # фрейме чужого сайта, и браузер туда куки не отдаёт.
    bearer = request.headers.get("authorization", "")
    if bearer.lower().startswith("bearer ") and auth.token_is_valid(bearer[7:].strip()):
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse({"detail": "Требуется вход в систему."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.post("/api/login")
def login(request: LoginRequest, http_request: Request) -> JSONResponse:
    if not settings.auth_required:
        return JSONResponse({"status": "ok", "auth_required": False})

    client = http_request.client.host if http_request.client else "unknown"
    locked_for = auth.throttle_state(client)
    if locked_for:
        return JSONResponse(
            {"detail": f"Слишком много попыток. Повторите через {locked_for} с."},
            status_code=429,
        )

    if not auth.password_is_valid(request.password):
        auth.register_failure(client)
        return JSONResponse({"detail": "Неверный пароль."}, status_code=401)

    auth.register_success(client)
    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_token(),
        max_age=settings.auth_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        # За прокси Amvera соединение идёт по HTTPS — куку помечаем Secure.
        secure=bool(settings.public_url.startswith("https://")),
    )
    return response


@app.post("/api/telegram/auth")
def telegram_auth(request: TelegramAuthRequest) -> JSONResponse:
    """Обменивает подписанные Telegram данные Mini App на токен сессии."""
    try:
        user = webapp.validate(request.init_data)
    except webapp.InitDataError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)
    return JSONResponse(
        {
            "status": "ok",
            "token": auth.issue_token(str(user.get("id") or "")),
            "user": {"id": user.get("id"), "first_name": user.get("first_name", "")},
        }
    )


@app.get("/api/models")
def models(tools_only: bool = True) -> JSONResponse:
    """Каталог моделей шлюза с ценами за 1 млн токенов."""
    chosen = model_choice.choice()
    if not routerai.available():
        return JSONResponse(
            {"available": False, "detail": "Каталог есть только у RouterAI и OpenRouter.", "current": chosen}
        )
    try:
        catalog = routerai.list_models()
    except routerai.BillingError as exc:
        return JSONResponse({"available": False, "detail": str(exc), "current": chosen}, status_code=502)
    # Без вызова инструментов агент бесполезен — такие модели не предлагаем.
    # Неизвестно (шлюз не сообщил) — показываем с пометкой.
    # Выбор ограничен тремя семействами: каталог большой, а людям нужны они.
    tooled = [m for m in catalog if not tools_only or m["tools"] is not False]
    # Из каждого семейства — только несколько актуальных версий (model_families.curate).
    allowed = [m for m in tooled if model_families.is_allowed(m["id"])]
    listed = [
        {**m, "family": model_families.family(m["id"])}
        for m in model_families.curate(allowed, chosen.get("model", ""))
    ]
    return JSONResponse(
        {
            "available": True,
            "currency": routerai.currency(),
            "current": chosen,
            "models": listed,
            "families": [
                {"key": key, "label": label, "count": sum(1 for m in listed if model_families.family(m["id"]) == key)}
                for key, label in model_families.FAMILIES
            ],
            "hidden_without_tools": len(catalog) - len(tooled),
            "hidden_by_family": len(tooled) - len(allowed),
            "hidden_old_versions": len(allowed) - len(listed),
        }
    )


@app.post("/api/model")
def choose_model(request: ModelRequest, http_request: Request) -> JSONResponse:
    model_id = request.model.strip()
    if routerai.available():
        try:
            found = routerai.find_model(model_id)
        except routerai.BillingError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=502)
        if found is None:
            return JSONResponse({"detail": f"Модели «{model_id}» нет в каталоге шлюза."}, status_code=404)
        if found["tools"] is False:
            return JSONResponse(
                {"detail": "Эта модель не умеет вызывать инструменты — агент с ней работать не сможет."},
                status_code=422,
            )
    if not model_families.is_allowed(model_id):
        return JSONResponse(
            {"detail": "В выборе только модели DeepSeek, GPT и Claude. Другие задаются в настройках сервера (OPERON_MODEL)."},
            status_code=422,
        )
    who = http_request.headers.get("x-operon-user", "")[:100]
    model_choice.set_model(model_id, changed_by=who)
    logger.info("Модель переключена на %s (%s)", model_id, who or "веб")
    return JSONResponse({"status": "ok", "current": model_choice.choice()})


@app.get("/api/billing")
def billing() -> JSONResponse:
    """Баланс, расход по ключу и цены текущей модели."""
    chosen = model_choice.choice()
    if not routerai.available():
        return JSONResponse({"available": False, "current": chosen})
    data = routerai.billing()
    try:
        data["model"] = routerai.find_model(chosen["model"])
    except routerai.BillingError as exc:
        data["errors"].append(str(exc))
    return JSONResponse({"available": True, "current": chosen, **data})


@app.get("/api/dashboard")
def dashboard_summary(http_request: Request) -> JSONResponse:
    """Пульт «Сегодня»: встречи, поручения, показатели, документы, модель и расходы."""
    return JSONResponse(dashboard.summary(_account(http_request)))


@app.get("/api/documents")
def dashboard_documents(http_request: Request, limit: int = 300) -> JSONResponse:
    """Документы «Квант», доступные этому человеку (по его роли на сайте)."""
    return JSONResponse(dashboard.documents(_account(http_request), min(max(limit, 1), 500)))


@app.post("/api/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(auth.COOKIE_NAME)
    return response


@app.get("/login")
def login_page() -> Response:
    if not settings.auth_required:
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


def _account(request: Request) -> str:
    """Чей Google: Telegram ID из токена Mini App; вход паролем — владелец."""
    bearer = request.headers.get("authorization", "")
    token = bearer[7:].strip() if bearer.lower().startswith("bearer ") else request.cookies.get(auth.COOKIE_NAME)
    return auth.token_subject(token)


@app.post("/api/chat")
def chat(request: ChatRequest, http_request: Request) -> StreamingResponse:
    session = store.get_or_create(request.session_id)
    if not session.account:
        session.account = _account(http_request)

    def events() -> Iterator[dict[str, Any]]:
        yield {"type": "session", "session_id": session.session_id}
        yield from agent.send_user_message(session, request.message)

    return _stream_response(events())


@app.post("/api/confirm")
def confirm(request: ConfirmRequest, http_request: Request) -> StreamingResponse:
    session = store.get(request.session_id)
    if session is not None and session.account != _account(http_request):
        # Чужую сессию не подтверждаем: действие выполнилось бы с чужим Google.
        session = None
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия не найдена — начните новый диалог.")
    if session.pending_expired:
        # Запоздалое «подтверждаю» не принимаем: срок вышел, действие уже
        # отклонено. Отказ уедет модели вместе со следующей репликой.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Срок подтверждения истёк ({settings.confirmation_ttl_minutes} мин) — "
                "действие отменено. Повторите запрос, если оно всё ещё нужно."
            ),
        )
    if not session.awaiting_confirmation:
        raise HTTPException(status_code=409, detail="Нет действий, ожидающих подтверждения.")

    decisions = {d.tool_use_id: d.decision for d in request.decisions}
    comments = {d.tool_use_id: d.comment for d in request.decisions if d.comment}

    def events() -> Iterator[dict[str, Any]]:
        yield {"type": "session", "session_id": session.session_id}
        yield from agent.resume_with_decisions(session, decisions, comments)

    return _stream_response(events())


@app.post("/api/session/reset")
def reset(request: ResetRequest) -> dict[str, str]:
    session = store.reset(request.session_id or "")
    return {"session_id": session.session_id}


@app.get("/api/status")
def status(http_request: Request) -> dict[str, Any]:
    with accounts.use(_account(http_request)):
        google = google_client.status()
    kb = knowledge_base.stats
    return {
        "org": settings.org_name,
        "model": model_choice.current_model(),
        "provider": settings.provider,
        "billing": routerai.available(),
        "effort": settings.effort if settings.effort_enabled else None,
        "auth_required": settings.auth_required,
        "timezone": settings.timezone_name,
        "knowledge_base": kb,
        "google": google,
        "google_oauth": google_oauth.describe_client(),
        "web_search": web_tools.internet_available(),
        "telegram": telegram.status(),
        "confirmation_ttl_minutes": settings.confirmation_ttl_minutes,
        "tools": [
            {
                "name": spec.name,
                "requires_confirmation": spec.requires_confirmation,
                "activity": spec.activity,
            }
            for spec in registry.all()
        ],
    }


@app.get("/api/health")
def health(strict: bool = False) -> JSONResponse:
    """Живость процесса и состояние бота.

    По умолчанию код всегда 200: это проверка живости, а перезапуск контейнера
    из-за неверного токена Telegram превратился бы в бесконечный цикл — бота
    и так перезапускает надзор внутри процесса. Для внешнего мониторинга есть
    ?strict=1: там неработающий, но настроенный бот даёт 503.
    """
    bot = telegram.status()
    healthy = bool(bot["healthy"])
    payload: dict[str, Any] = {
        "status": "ok" if healthy else "degraded",
        "web": "ok",
        "telegram": bot,
    }
    return JSONResponse(payload, status_code=503 if (strict and not healthy) else 200)


@app.get("/api/files/{file_id}")
def download(file_id: str) -> FileResponse:
    meta = outbox.get(file_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Файл не найден или срок хранения истёк.")
    return FileResponse(meta["path"], filename=meta["name"], media_type=meta.get("mime"))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


TELEGRAM_SCRIPT = '<script async src="https://telegram.org/js/telegram-web-app.js"></script>'
# Флаг включает пульт (Сегодня, Документы, Поручения): он есть только в мини-приложении.
MINIAPP_FLAG = "<script>window.OPERON_MINIAPP = true;</script>"


@app.get("/miniapp")
def miniapp() -> HTMLResponse:
    """Та же страница, что веб-чат, плюс скрипт Telegram: вход по его подписи.

    Скрипт подключается только здесь: в обычном веб-чате он не нужен, а
    недоступный telegram.org задерживал бы загрузку страницы.
    """
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(page.replace("<!--telegram-web-app-->", TELEGRAM_SCRIPT + MINIAPP_FLAG, 1))


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _startup_report() -> None:
    from .config import ENV_FILES_LOADED

    if ENV_FILES_LOADED:
        logger.info(
            "Настройки прочитаны из файлов: %s",
            ", ".join(str(path) for path in ENV_FILES_LOADED),
        )
    kb = knowledge_base.stats
    logger.info(
        "Провайдер: %s | модель: %s | адрес: %s",
        settings.provider,
        model_choice.current_model(),
        settings.base_url or "api.anthropic.com",
    )
    if not settings.auth_required:
        logger.warning("Пароль не задан (OPERON_ACCESS_PASSWORD) — вход в интерфейс свободный")
    logger.info(
        "База знаний: %s документов в %s", kb["documents"], kb["root"]
    )
    from . import quant

    quant_conf = quant.config()
    if quant_conf.enabled:
        logger.info("Квант подключён: %s — основная база знаний", quant_conf.url)
    elif not kb["documents"]:
        logger.warning(
            "База знаний пуста — агент будет честно отвечать «данных нет». "
            "Положите документы в %s (структура описана в knowledge_base/README.md).",
            kb["root"],
        )
    google = google_client.status()
    if google.get("connected"):
        logger.info("Google подключён: %s", google.get("account_hint") or "учётная запись определена")
    else:
        logger.warning("Google не подключён: %s", google.get("reason"))
    logger.info("Инструментов зарегистрировано: %s", len(registry.names()))
    logger.info(
        "Подтверждение действий: %s",
        f"ждём ответа {settings.confirmation_ttl_minutes} мин, затем отказ"
        if settings.confirmation_ttl_minutes > 0
        else "без ограничения по времени (OPERON_CONFIRMATION_TTL_MINUTES=0)",
    )
    if settings.telegram_enabled:
        logger.info(
            "Telegram: белый список из %s пользователей %s (%s)",
            len(settings.telegram_allowed_users),
            sorted(settings.telegram_allowed_users),
            env_source("TELEGRAM_ALLOWED_USERS"),
        )
