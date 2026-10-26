"""Exceptions shown to users as plain error messages."""

from __future__ import annotations


class UserError(Exception):
    """An expected error whose message is safe to show in the chat (Russian, HTML-escaped)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
