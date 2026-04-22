"""Logging filter that scrubs the Anthropic API key from every log record.

Install by attaching `AnthropicKeyRedactor()` to the root logger's handlers.
Both the API and worker logging configs do this at boot.
"""

from __future__ import annotations

import logging
import os
import re


class AnthropicKeyRedactor(logging.Filter):
    """Replace any occurrence of the current ANTHROPIC_API_KEY (or any plausible
    key-shaped token) with `[REDACTED]` before the record is formatted.
    """

    # Anthropic keys follow `sk-ant-...` with alphanumerics, dashes, underscores.
    KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")

    def filter(self, record: logging.LogRecord) -> bool:
        current_key = os.environ.get("ANTHROPIC_API_KEY", "") or ""
        replacements: list[tuple[str, str]] = []
        if current_key and len(current_key) >= 8:
            replacements.append((current_key, "[REDACTED]"))

        if isinstance(record.msg, str):
            msg = record.msg
            for needle, replacement in replacements:
                if needle in msg:
                    msg = msg.replace(needle, replacement)
            msg = self.KEY_PATTERN.sub("[REDACTED]", msg)
            record.msg = msg

        if record.args:
            new_args: list | tuple | dict
            if isinstance(record.args, dict):
                new_args = {k: self._scrub(v, replacements) for k, v in record.args.items()}
            else:
                new_args = tuple(self._scrub(a, replacements) for a in record.args)
            record.args = new_args  # type: ignore[assignment]
        return True

    def _scrub(self, value, replacements):
        if not isinstance(value, str):
            return value
        for needle, replacement in replacements:
            if needle in value:
                value = value.replace(needle, replacement)
        return self.KEY_PATTERN.sub("[REDACTED]", value)


def install_global_redactor() -> None:
    """Attach the redactor to every handler on the root logger. Idempotent."""
    root = logging.getLogger()
    redactor = AnthropicKeyRedactor()
    for h in root.handlers:
        if not any(isinstance(f, AnthropicKeyRedactor) for f in h.filters):
            h.addFilter(redactor)
    # Also attach to the root logger itself so emissions before handler setup
    # (unlikely but possible) still run the filter.
    if not any(isinstance(f, AnthropicKeyRedactor) for f in root.filters):
        root.addFilter(redactor)
