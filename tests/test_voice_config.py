"""Provider selection, compatibility and secret-safe configuration."""

import pytest
from pydantic import ValidationError

from is_this_us.config import Settings


@pytest.fixture(autouse=True)
def clean_voice_env(monkeypatch):
    import os

    for name in os.environ:
        if name.startswith(("VOICE_", "OPENAI_", "ELEVENLABS_", "AZURE_OPENAI_")):
            monkeypatch.delenv(name)


def test_new_installation_uses_elevenlabs_without_azure_fallback():
    s = Settings(_env_file=None, azure_openai_api_key="legacy-secret")
    assert s.voice_provider == "elevenlabs"
    assert not s.voice_configured
    assert s.azure_voice_configured
    assert "legacy-secret" not in repr(s)


def test_credentials_and_explicit_legacy_aliases():
    s = Settings(_env_file=None, elevenlabs_api_key="private-key", elevenlabs_voice_id="voice-id")
    assert s.voice_configured
    assert "private-key" not in repr(s)
    old = Settings(_env_file=None, openai_voice_provider="openai", azure_openai_api_key="old",
                   openai_audio_cache_enabled=False)
    assert old.voice_provider == "openai" and old.voice_configured
    assert not old.voice_cache_enabled


def test_new_environment_controls_override_legacy(monkeypatch):
    monkeypatch.setenv("VOICE_PROVIDER", "elevenlabs")
    monkeypatch.setenv("OPENAI_VOICE_PROVIDER", "none")
    monkeypatch.setenv("VOICE_CACHE_ENABLED", "true")
    monkeypatch.setenv("OPENAI_AUDIO_CACHE_ENABLED", "false")
    s = Settings(_env_file=None)
    assert s.voice_provider == "elevenlabs" and s.voice_cache_enabled


def test_none_is_explicit_even_with_credentials():
    s = Settings(_env_file=None, voice_provider="none", elevenlabs_api_key="key", elevenlabs_voice_id="id")
    assert not s.voice_configured


@pytest.mark.parametrize("values", [
    {"voice_provider": "typo"}, {"openai_voice_provider": "typo"},
    {"elevenlabs_stability": 2}, {"elevenlabs_timeout_seconds": 0},
    {"elevenlabs_output_format": "mp3_44100_128"},
])
def test_invalid_provider_and_audio_settings_fail_early(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
