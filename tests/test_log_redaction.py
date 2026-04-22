from __future__ import annotations

import logging

import pytest

from reelforge_core.log_redaction import AnthropicKeyRedactor


def _capture(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(rec.getMessage() for rec in caplog.records)


def test_redacts_literal_env_key(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-SECRET_TOKEN_ABC1234567890")
    logger = logging.getLogger("test_redact_literal")
    logger.addFilter(AnthropicKeyRedactor())
    with caplog.at_level(logging.INFO, logger="test_redact_literal"):
        logger.info(
            "starting with key %s", "sk-ant-api03-SECRET_TOKEN_ABC1234567890"
        )
    out = _capture(caplog)
    assert "SECRET_TOKEN" not in out
    assert "[REDACTED]" in out


def test_redacts_pattern_match(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    logger = logging.getLogger("test_redact_pattern")
    logger.addFilter(AnthropicKeyRedactor())
    with caplog.at_level(logging.INFO, logger="test_redact_pattern"):
        logger.info("found stray token sk-ant-api03-Oops1234567890abcdef in body")
    out = _capture(caplog)
    assert "Oops1234567890abcdef" not in out
    assert "[REDACTED]" in out


def test_non_string_args_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test_redact_nonstr")
    logger.addFilter(AnthropicKeyRedactor())
    with caplog.at_level(logging.INFO, logger="test_redact_nonstr"):
        logger.info("count=%d code=%s", 42, None)
    out = _capture(caplog)
    assert "count=42" in out
