"""API keys the user enters through the bot (never in the code or the chat history).
Stored in data/secrets.json; the bot deletes the message the key was sent in."""
from __future__ import annotations

import json

from arb.config import DATA_DIR

FILE = DATA_DIR / "secrets.json"


def get(name: str) -> str | None:
    try:
        return json.loads(FILE.read_text(encoding="utf-8")).get(name) or None
    except (OSError, ValueError):
        return None


def put(name: str, value: str) -> None:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[name] = value
    DATA_DIR.mkdir(exist_ok=True)
    FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
