"""Авторизация Google: набор прав и разбор кода из адресной строки.

Состав scope'ов зафиксирован тестом намеренно. Каждое изменение набора — это
новый экран согласия и заново пройденный OAuth: старый токен новых прав не
получает. Такое решение должно приниматься осознанно, а не проскакивать
незамеченным в рефакторинге.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.integrations import google_oauth

DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"

DESKTOP_CLIENT = {
    "installed": {
        "client_id": "123.apps.googleusercontent.com",
        "project_id": "operon",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": "GOCSPX-test",
        "redirect_uris": ["http://localhost"],
    }
}


@pytest.fixture
def client_secret(tmp_path, monkeypatch):
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps(DESKTOP_CLIENT), encoding="utf-8")
    monkeypatch.setenv("OPERON_GOOGLE_CLIENT_SECRET", str(path))
    return path


class TestScopeSet:
    def test_exact_scope_set(self) -> None:
        # Список календарей — только чтение списка, не событий и не настроек.
        calendar_list = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
        assert set(settings.google_scopes) == {DRIVE_READONLY, DRIVE_FILE, CALENDAR_EVENTS, calendar_list}

    def test_no_full_drive_access(self) -> None:
        """drive.file даёт доступ только к файлам, созданным самим агентом."""
        assert "https://www.googleapis.com/auth/drive" not in settings.google_scopes

    def test_no_calendar_listing_scope(self) -> None:
        """Полный calendar не нужен: события — через calendar.events, список — через calendarlist.readonly."""
        assert "https://www.googleapis.com/auth/calendar" not in settings.google_scopes

    def test_scopes_are_overridable(self, monkeypatch) -> None:
        from dataclasses import replace

        limited = replace(settings, google_scopes=(DRIVE_READONLY,))
        assert limited.google_scopes == (DRIVE_READONLY,)


class TestAuthorizationUrl:
    def test_url_requests_offline_access(self, client_secret) -> None:
        """Без refresh_token доступ отвалится через час и потребует ручной работы."""
        url = google_oauth.authorization_url()
        assert "access_type=offline" in url
        assert "prompt=consent" in url

    def test_url_carries_every_scope(self, client_secret) -> None:
        url = google_oauth.authorization_url()
        for scope in (DRIVE_READONLY, DRIVE_FILE, CALENDAR_EVENTS):
            assert scope.rsplit("/", 1)[-1] in url

    def test_missing_client_secret_names_both_ways(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("OPERON_GOOGLE_CLIENT_SECRET", str(tmp_path / "нет.json"))
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        with pytest.raises(google_oauth.OAuthError, match="GOOGLE_CLIENT_ID"):
            google_oauth.authorization_url()

    def test_web_client_with_foreign_redirect_is_refused(self, monkeypatch, tmp_path) -> None:
        path = tmp_path / "web.json"
        path.write_text(json.dumps({"web": {
            "client_id": "1", "client_secret": "2",
            "auth_uri": "https://a", "token_uri": "https://t",
            "redirect_uris": ["https://example.test/cb"],
        }}), encoding="utf-8")
        monkeypatch.setenv("OPERON_GOOGLE_CLIENT_SECRET", str(path))
        with pytest.raises(google_oauth.OAuthError, match="redirect URIs"):
            google_oauth.authorization_url()


class TestCodeExtraction:
    """Пользователь копирует то, что видит, — разбирать надо все формы."""

    @pytest.mark.parametrize(
        ("pasted", "expected"),
        [
            ("http://localhost:8765/?code=4%2F0Axyz_abc-def&scope=x", "4/0Axyz_abc-def"),
            ("localhost:8765/?code=4/0Axyz_abc-def", "4/0Axyz_abc-def"),
            ("4/0Axyz_abcdefghijkl", "4/0Axyz_abcdefghijkl"),
            ("4%2F0Axyz_abcdefghijkl", "4/0Axyz_abcdefghijkl"),
            ("  4/0Axyz_abcdefghijkl  ", "4/0Axyz_abcdefghijkl"),
        ],
    )
    def test_code_is_extracted(self, pasted: str, expected: str) -> None:
        assert google_oauth.extract_code(pasted) == expected

    def test_denied_access_is_explained(self) -> None:
        with pytest.raises(google_oauth.OAuthError, match="отказ"):
            google_oauth.extract_code("http://localhost:8765/?error=access_denied")

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            # «Доступ заблокирован» при нажатом «Разрешить» — это Testing/Test users,
            # а не отмена: совет «нажмите Разрешить» тут бесполезен.
            ("http://localhost:8765/?error=access_denied", "Test users"),
            ("http://localhost:8765/?error=admin_policy_enforced", "администратор"),
            ("http://localhost:8765/?error=org_internal", "Internal"),
            ("http://localhost:8765/?error=disallowed_useragent", "браузер"),
        ],
    )
    def test_denial_reason_is_named(self, url: str, expected: str) -> None:
        with pytest.raises(google_oauth.OAuthError, match=expected):
            google_oauth.extract_code(url)

    def test_unknown_denial_still_explains(self) -> None:
        """Незнакомый код Google не должен превращаться в молчание."""
        with pytest.raises(google_oauth.OAuthError, match="something_new"):
            google_oauth.extract_code("http://localhost:8765/?error=something_new")

    def test_denial_url_is_recognised_as_an_answer(self) -> None:
        """Адрес со страницы ошибки — ответ на /auth, а не посторонний текст.

        Иначе бот отвечал «это не похоже на код» и не говорил, что случилось.
        """
        assert google_oauth.looks_like_code("http://localhost:8765/?error=access_denied") is True
        assert google_oauth.looks_like_code(
            "http://localhost:8765/?error=admin_policy_enforced&state=abc"
        ) is True

    def test_url_without_code_is_rejected(self) -> None:
        with pytest.raises(google_oauth.OAuthError, match="code"):
            google_oauth.extract_code("http://localhost:8765/?state=abc&code=")

    @pytest.mark.parametrize(
        "text", ["какие встречи на завтра?", "привет", "", "подготовь КП для Иванова"]
    )
    def test_ordinary_text_is_not_a_code(self, text: str) -> None:
        assert google_oauth.looks_like_code(text) is False

    @pytest.mark.parametrize(
        "text", ["4/0Axyz_abcdefghijkl", "http://localhost:8765/?code=4/0A"]
    )
    def test_code_is_recognised(self, text: str) -> None:
        assert google_oauth.looks_like_code(text) is True


class TestInstructions:
    def test_instructions_warn_about_the_browser_error(self) -> None:
        """Ошибка соединения — ожидаемый шаг, иначе пользователь решит, что сломалось."""
        text = google_oauth.instructions()
        assert "ошибку соединения" in text
        assert settings.oauth_redirect_uri in text


class TestEnvClient:
    """Клиент из переменных окружения: на Amvera так проще, чем файлом."""

    @pytest.fixture(autouse=True)
    def env_client(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "111111111111-test.apps.googleusercontent.com")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-env-test")
        monkeypatch.setenv("OPERON_GOOGLE_CLIENT_SECRET", str(tmp_path / "нет.json"))

    def test_env_beats_missing_file(self, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_OAUTH_REDIRECT_URI", "http://localhost:8765/")
        url = google_oauth.authorization_url()
        assert "111111111111-test" in url

    def test_web_section_when_callback_is_used(self, monkeypatch) -> None:
        """Публичный адрес приложения = клиент типа Web, секция «web»."""
        monkeypatch.setenv("OPERON_OAUTH_REDIRECT_URI", "https://operon.amvera.io/oauth2/callback")
        config = google_oauth._config_from_env()
        assert "web" in config and "installed" not in config
        assert config["web"]["redirect_uris"] == ["https://operon.amvera.io/oauth2/callback"]

    def test_installed_section_for_loopback(self, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_OAUTH_REDIRECT_URI", "http://localhost:8765/")
        config = google_oauth._config_from_env()
        assert "installed" in config and "web" not in config

    def test_describe_names_the_source(self, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_OAUTH_REDIRECT_URI", "https://operon.amvera.io/oauth2/callback")
        described = google_oauth.describe_client()
        assert described["configured"] is True
        assert described["source"] == "переменные окружения"
        assert described["callback_mode"] is True


class TestStateGate:
    """Адрес возврата публичен — обменивать можно только код по выданной ссылке."""

    @pytest.fixture(autouse=True)
    def env_client(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "1-test.apps.googleusercontent.com")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-env-test")
        monkeypatch.setenv("OPERON_GOOGLE_CLIENT_SECRET", str(tmp_path / "нет.json"))
        monkeypatch.setenv("OPERON_OAUTH_REDIRECT_URI", "https://operon.amvera.io/oauth2/callback")
        google_oauth._pending.clear()

    def test_unknown_state_is_refused(self) -> None:
        with pytest.raises(google_oauth.OAuthError, match="неизвестна или устарела"):
            google_oauth.handle_callback("4/0Axyz_abcdefghijkl", "подделанный-state")

    def test_empty_state_is_refused(self) -> None:
        with pytest.raises(google_oauth.OAuthError, match="неизвестна или устарела"):
            google_oauth.handle_callback("4/0Axyz_abcdefghijkl", "")

    def test_start_issues_unique_states(self) -> None:
        _, first = google_oauth.start("telegram:1")
        _, second = google_oauth.start("telegram:1")
        assert first != second
        assert len(first) >= 24
        assert set(google_oauth._pending) == {first, second}

    def test_state_travels_in_the_url(self) -> None:
        url, state = google_oauth.start()
        assert f"state={state}" in url

    def test_result_is_taken_once(self, monkeypatch) -> None:
        _, state = google_oauth.start("telegram:1")
        monkeypatch.setattr(
            google_oauth,
            "exchange_code",
            lambda text, state="": {"encrypted": True, "scopes": [], "missing_scopes": [], "account": "a@b.c"},
        )
        google_oauth.handle_callback("4/0Axyz_abcdefghijkl", state)

        first = google_oauth.take_result(state)
        assert first is not None and first.result["account"] == "a@b.c"
        assert google_oauth.take_result(state) is None, "результат забирается ровно один раз"

    def test_used_link_cannot_be_replayed(self, monkeypatch) -> None:
        _, state = google_oauth.start("telegram:1")
        monkeypatch.setattr(
            google_oauth,
            "exchange_code",
            lambda text, state="": {"encrypted": True, "scopes": [], "missing_scopes": [], "account": ""},
        )
        google_oauth.handle_callback("4/0Axyz_abcdefghijkl", state)
        with pytest.raises(google_oauth.OAuthError, match="уже использована"):
            google_oauth.handle_callback("4/0Axyz_abcdefghijkl", state)

    def test_failed_exchange_is_remembered_for_the_bot(self, monkeypatch) -> None:
        _, state = google_oauth.start("telegram:1")

        def boom(text: str, state: str = ""):
            raise google_oauth.OAuthError("Google отклонил код (invalid_grant).")

        monkeypatch.setattr(google_oauth, "exchange_code", boom)
        with pytest.raises(google_oauth.OAuthError):
            google_oauth.handle_callback("4/0Axyz_abcdefghijkl", state)

        entry = google_oauth.take_result(state)
        assert entry is not None and "invalid_grant" in entry.error


class TestClientType:
    """Перепутанный тип клиента даёт redirect_uri_mismatch уже после клика.

    Причина в этот момент совсем не очевидна, поэтому тип задаётся явно и
    важнее наличия публичного адреса.
    """

    @pytest.fixture(autouse=True)
    def env_client(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "1-test.apps.googleusercontent.com")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-test")
        monkeypatch.setenv("OPERON_GOOGLE_CLIENT_SECRET", str(tmp_path / "нет.json"))
        monkeypatch.delenv("OPERON_OAUTH_REDIRECT_URI", raising=False)

    def _settings(self, monkeypatch, **kw):
        from app.config import Settings

        conf = Settings(**kw)
        monkeypatch.setattr("app.integrations.google_oauth.settings", conf)
        return conf

    def test_desktop_ignores_the_public_url(self, monkeypatch) -> None:
        """Desktop-клиент зарегистрированный https-адрес не примет."""
        monkeypatch.setenv("OPERON_GOOGLE_CLIENT_TYPE", "desktop")
        conf = self._settings(monkeypatch, public_url="https://operon.amvera.io")

        assert conf.oauth_redirect_uri == "http://localhost:8765/"
        assert conf.oauth_callback_enabled is False
        assert "installed" in google_oauth._config_from_env()

    def test_web_uses_the_callback(self, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_GOOGLE_CLIENT_TYPE", "web")
        conf = self._settings(monkeypatch, public_url="https://operon.amvera.io")

        assert conf.oauth_redirect_uri.endswith("/oauth2/callback")
        assert conf.oauth_callback_enabled is True
        assert "web" in google_oauth._config_from_env()

    def test_without_type_the_public_url_decides(self, monkeypatch) -> None:
        monkeypatch.delenv("OPERON_GOOGLE_CLIENT_TYPE", raising=False)
        conf = self._settings(monkeypatch, public_url="https://operon.amvera.io")
        assert conf.oauth_callback_enabled is True

        bare = self._settings(monkeypatch, public_url="")
        assert bare.oauth_redirect_uri == "http://localhost:8765/"

    def test_explicit_redirect_beats_everything(self, monkeypatch) -> None:
        monkeypatch.setenv("OPERON_GOOGLE_CLIENT_TYPE", "desktop")
        monkeypatch.setenv("OPERON_OAUTH_REDIRECT_URI", "http://127.0.0.1:9999/")
        conf = self._settings(monkeypatch, public_url="https://operon.amvera.io")
        assert conf.oauth_redirect_uri == "http://127.0.0.1:9999/"

    def test_desktop_instructions_explain_the_paste(self, monkeypatch) -> None:
        """В режиме localhost пользователь обязан знать, что делать с адресом."""
        monkeypatch.setenv("OPERON_GOOGLE_CLIENT_TYPE", "desktop")
        self._settings(monkeypatch, public_url="https://operon.amvera.io")
        text = google_oauth.instructions()
        assert "ошибку соединения" in text
        assert "пришлите его сюда" in text


class TestPkce:
    """Библиотека Google сама добавляет PKCE в ссылку. Секрет живёт в объекте,
    выдавшем ссылку, а обмен идёт в новом — без переноса секрета Google
    отвечает invalid_grant («Missing code verifier»), и /auth не работает."""

    def _intercept_token_request(self, monkeypatch):
        import requests

        sent = {}

        def fake_request(session, method, url, data=None, **kwargs):
            sent["url"] = url
            sent["data"] = data if isinstance(data, dict) else dict(
                pair.split("=", 1) for pair in (data or "").split("&") if "=" in pair
            )
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({
                "access_token": "ya29.test", "refresh_token": "1//refresh", "expires_in": 3599,
                "token_type": "Bearer", "scope": f"{DRIVE_READONLY} {DRIVE_FILE} {CALENDAR_EVENTS}",
            }).encode()
            response.headers["Content-Type"] = "application/json"
            response.url = url
            response.request = requests.Request(method, url).prepare()
            return response

        monkeypatch.setattr(requests.Session, "request", fake_request)
        monkeypatch.setattr(google_oauth.token_store, "save_token", lambda payload: "token-path")
        monkeypatch.setattr(google_oauth, "_account_email", lambda: "kirill@example.com")
        return sent

    @staticmethod
    def _challenge(verifier: str) -> str:
        import base64
        import hashlib

        return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    def test_pasted_code_is_exchanged_with_the_links_verifier(self, client_secret, monkeypatch) -> None:
        from urllib.parse import parse_qs, unquote, urlparse

        url, state = google_oauth.start("telegram:1107365044")
        challenge = parse_qs(urlparse(url).query)["code_challenge"][0]
        sent = self._intercept_token_request(monkeypatch)

        # Человек прислал только код, без адреса — state знает бот.
        result = google_oauth.exchange_code("4/0Axyz_abcdefghijkl", state=state)

        verifier = unquote(sent["data"]["code_verifier"])
        assert self._challenge(verifier) == challenge
        assert result["account"] == "kirill@example.com" and result["missing_scopes"] == []

    def test_state_is_taken_from_the_pasted_address(self, client_secret, monkeypatch) -> None:
        from urllib.parse import parse_qs, unquote, urlparse

        url, state = google_oauth.start("telegram:1107365044")
        challenge = parse_qs(urlparse(url).query)["code_challenge"][0]
        sent = self._intercept_token_request(monkeypatch)

        google_oauth.exchange_code(f"http://localhost:8765/?state={state}&code=4%2F0Axyz_abcdefghijkl&scope=x")
        assert self._challenge(unquote(sent["data"]["code_verifier"])) == challenge
