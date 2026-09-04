"""Shared cleanup for text returned by local reasoning models."""
from __future__ import annotations

import re


def visible_answer(text: str) -> str:
    """Remove provider-specific hidden reasoning from user-visible output."""
    value = re.sub(r"<think>.*?</think>", "", text or "", flags=re.I | re.S)
    value = re.sub(r"<thinking>.*?</thinking>", "", value, flags=re.I | re.S)
    value = re.sub(
        r"^\s*(?:thinking|gedanken|self[- ]?correction)\s*:\s*.*?\n",
        "",
        value,
        flags=re.I | re.S,
    )
    return value.strip()
