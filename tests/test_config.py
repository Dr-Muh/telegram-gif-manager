import pytest

from bot.config import Config


def test_admin_user_ids_are_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("ADMIN_USER_IDS", "1, 2")
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)

    assert Config.from_environment().admin_user_ids == {1, 2}


def test_legacy_allowed_user_ids_are_treated_as_admins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    monkeypatch.setenv("ALLOWED_USER_IDS", "3")

    assert Config.from_environment().admin_user_ids == {3}
