"""HTTP-слой: SSE-поток, подтверждения, статус."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import server
from app.sessions import store


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(server.app) as test_client:
        yield test_client


def sse_events(response) -> list[dict[str, Any]]:
    events = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


class FakeAgent:
    """Заменяет реального агента, чтобы не ходить в API Anthropic."""

    def __init__(self, script: list[dict[str, Any]], *, fail: bool = False) -> None:
        self.script = script
        self.fail = fail
        self.resumed_with: dict[str, str] | None = None

    def send_user_message(self, session, text: str):
        if self.fail:
            raise RuntimeError("сломалось внутри агента")
        yield from self.script

    def resume_with_decisions(self, session, decisions, comments=None):
        self.resumed_with = decisions
        session.pending = None
        yield {"type": "done", "stop": "end_turn"}


class TestBasics:
    def test_health(self, client: TestClient) -> None:
        payload = client.get("/api/health").json()
        assert payload["status"] == "ok"
        assert payload["web"] == "ok"
        # Бот не настроен в тестах — это не деградация, а осознанное выключение.
        assert payload["telegram"]["configured"] is False
        assert payload["telegram"]["healthy"] is True

    def test_health_reports_dead_bot(self, client: TestClient, monkeypatch) -> None:
        """Молча умерший бот не должен выглядеть здоровым."""
        from app import server

        monkeypatch.setattr(
            server.telegram,
            "status",
            lambda: {"healthy": False, "running": False, "enabled": True,
                     "configured": True, "restarts": 3, "last_error": "TelegramError: 401"},
        )
        response = client.get("/api/health")
        assert response.status_code == 200, "обычная проверка живости не должна падать"
        assert response.json()["status"] == "degraded"
        assert response.json()["telegram"]["last_error"] == "TelegramError: 401"

        strict = client.get("/api/health?strict=1")
        assert strict.status_code == 503, "строгая проверка обязана видеть мёртвого бота"

    def test_status_exposes_configuration(self, client: TestClient) -> None:
        payload = client.get("/api/status").json()
        assert payload["model"]
        assert payload["knowledge_base"]["documents"] == 0
        assert payload["google"]["connected"] is False
        assert any(tool["requires_confirmation"] for tool in payload["tools"])

    def test_index_page_is_served(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "Ассистент OPERON" in response.text


class TestChat:
    def test_stream_returns_events_in_order(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setattr(
            server,
            "agent",
            FakeAgent(
                [
                    {"type": "text_delta", "text": "Привет"},
                    {"type": "text_delta", "text": ", коллега"},
                    {"type": "done", "stop": "end_turn"},
                ]
            ),
        )
        response = client.post("/api/chat", json={"message": "Здравствуйте"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        events = sse_events(response)
        assert events[0]["type"] == "session"
        assert "".join(e["text"] for e in events if e["type"] == "text_delta") == "Привет, коллега"

    def test_session_id_is_reused(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setattr(server, "agent", FakeAgent([{"type": "done", "stop": "end_turn"}]))
        first = sse_events(client.post("/api/chat", json={"message": "раз"}))[0]["session_id"]
        second = sse_events(
            client.post("/api/chat", json={"message": "два", "session_id": first})
        )[0]["session_id"]
        assert first == second

    def test_empty_message_is_rejected(self, client: TestClient) -> None:
        assert client.post("/api/chat", json={"message": ""}).status_code == 422

    def test_agent_crash_is_reported_in_the_stream(self, client: TestClient, monkeypatch) -> None:
        """Клиент не должен зависнуть на открытом соединении при внутренней ошибке."""
        monkeypatch.setattr(server, "agent", FakeAgent([], fail=True))
        events = sse_events(client.post("/api/chat", json={"message": "привет"}))
        assert events[-2]["type"] == "error"
        assert events[-1] == {"type": "done", "stop": "error"}


class TestConfirm:
    def test_unknown_session_is_404(self, client: TestClient) -> None:
        response = client.post(
            "/api/confirm",
            json={"session_id": "нет-такой", "decisions": [{"tool_use_id": "t1", "decision": "approve"}]},
        )
        assert response.status_code == 404

    def test_confirm_without_pending_action_is_409(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setattr(server, "agent", FakeAgent([{"type": "done", "stop": "end_turn"}]))
        session_id = sse_events(client.post("/api/chat", json={"message": "привет"}))[0]["session_id"]

        response = client.post(
            "/api/confirm",
            json={"session_id": session_id, "decisions": [{"tool_use_id": "t1", "decision": "approve"}]},
        )
        assert response.status_code == 409

    def test_invalid_decision_value_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/confirm",
            json={"session_id": "x", "decisions": [{"tool_use_id": "t1", "decision": "может быть"}]},
        )
        assert response.status_code == 422

    def test_decisions_reach_the_agent(self, client: TestClient, monkeypatch) -> None:
        fake = FakeAgent([{"type": "done", "stop": "awaiting_confirmation"}])
        monkeypatch.setattr(server, "agent", fake)
        session_id = sse_events(client.post("/api/chat", json={"message": "привет"}))[0]["session_id"]

        from app.agent import PendingTurn

        store.get(session_id).pending = PendingTurn()

        client.post(
            "/api/confirm",
            json={
                "session_id": session_id,
                "decisions": [{"tool_use_id": "t1", "decision": "reject", "comment": "не сейчас"}],
            },
        )
        assert fake.resumed_with == {"t1": "reject"}


class TestSessionReset:
    def test_reset_clears_history(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setattr(server, "agent", FakeAgent([{"type": "done", "stop": "end_turn"}]))
        session_id = sse_events(client.post("/api/chat", json={"message": "привет"}))[0]["session_id"]
        store.get(session_id).messages.append({"role": "user", "content": "мусор"})

        client.post("/api/session/reset", json={"session_id": session_id})
        assert store.get(session_id).messages == []


class TestOAuthCallback:
    """Адрес возврата Google: доступен без входа, но пускает только по state."""

    def test_callback_is_reachable_without_login(self, client: TestClient) -> None:
        """Браузер приводит сюда Google — куки приложения там может не быть."""
        response = client.get("/oauth2/callback")
        assert response.status_code != 401
        assert response.status_code != 303

    def test_missing_code_is_explained(self, client: TestClient) -> None:
        response = client.get("/oauth2/callback")
        assert response.status_code == 400
        assert "Кода нет" in response.text

    def test_google_error_is_shown(self, client: TestClient) -> None:
        """Сырой код Google ничего не говорит человеку: нужна причина и действие."""
        response = client.get("/oauth2/callback?error=access_denied&state=x")
        assert response.status_code == 400
        assert "Доступ не выдан" in response.text
        assert "Test users" in response.text, "причина «доступ заблокирован» должна быть названа"
        assert "/auth" in response.text

    def test_domain_policy_error_is_named(self, client: TestClient) -> None:
        """Код Google сам по себе — не объяснение: нужна причина и что делать."""
        response = client.get("/oauth2/callback?error=admin_policy_enforced&state=x")
        assert response.status_code == 400
        assert "администратор" in response.text
        assert "Google вернул:" not in response.text, "голый код Google ничего не объясняет"

    def test_unknown_state_is_refused(self, client: TestClient) -> None:
        """Без этой проверки любой запрос к публичному адресу что-то подключал бы."""
        response = client.get("/oauth2/callback?code=4/0Axyz_abcdefghijkl&state=подделка")
        assert response.status_code == 400
        assert "устарела" in response.text

    def test_successful_exchange_reports_the_account(self, client: TestClient, monkeypatch) -> None:
        from app.integrations import google_oauth

        monkeypatch.setattr(
            google_oauth,
            "handle_callback",
            lambda code, state: {
                "account": "boss@operon.ru",
                "encrypted": True,
                "scopes": [],
                "missing_scopes": [],
            },
        )
        response = client.get("/oauth2/callback?code=4/0Axyz&state=st1")
        assert response.status_code == 200
        assert "Google подключён" in response.text
        assert "boss@operon.ru" in response.text
        assert "зашифрованном виде" in response.text

    def test_missing_scopes_are_warned_about(self, client: TestClient, monkeypatch) -> None:
        from app.integrations import google_oauth

        monkeypatch.setattr(
            google_oauth,
            "handle_callback",
            lambda code, state: {
                "account": "",
                "encrypted": False,
                "scopes": [],
                "missing_scopes": ["https://www.googleapis.com/auth/calendar.events"],
            },
        )
        response = client.get("/oauth2/callback?code=4/0Axyz&state=st1")
        assert "calendar.events" in response.text
        assert "не выдана" in response.text
