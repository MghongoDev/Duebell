"""Every on/off setting reads the same words (issue #85)."""

from __future__ import annotations

import pytest

from app.config import get_settings

# Setting name -> (Settings field, default when unset or blank).
BOOLEAN_SETTINGS = {
    "MOCK_WEBHOOKS": ("mock_webhooks", False),
    "SMTP_STARTTLS": ("smtp_starttls", True),
    "COOKIE_SECURE": ("cookie_secure", False),
}

WORDS = [
    ("on", True),
    ("off", False),
    ("yes", True),
    ("no", False),
    ("1", True),
    ("0", False),
    ("true", True),
    ("false", False),
    (" ON ", True),
    ("Off", False),
    ("TRUE", True),
    ("No", False),
]


def read(monkeypatch, name, value):
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        return get_settings()
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("name", BOOLEAN_SETTINGS)
@pytest.mark.parametrize(("word", "expected"), WORDS)
def test_each_boolean_setting_reads_each_word(monkeypatch, name, word, expected):
    field, _ = BOOLEAN_SETTINGS[name]
    assert getattr(read(monkeypatch, name, word), field) is expected


@pytest.mark.parametrize("name", BOOLEAN_SETTINGS)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_each_boolean_setting_keeps_its_default_when_unset_or_blank(monkeypatch, name, value):
    field, default = BOOLEAN_SETTINGS[name]
    assert getattr(read(monkeypatch, name, value), field) is default


@pytest.mark.parametrize("name", BOOLEAN_SETTINGS)
@pytest.mark.parametrize("word", ["maybe", "enabled", "2", "tru"])
def test_each_boolean_setting_refuses_an_unknown_word(monkeypatch, name, word):
    from app.config import ConfigError

    with pytest.raises(ConfigError, match=f"{name}.*{word}"):
        read(monkeypatch, name, word)
