"""Secrets for real betting (Polymarket and SX Bet wallet private keys).

They live in the operating system's key store through `keyring` - on Windows that is
the Credential Manager (Upravljač akreditivima), encrypted with the Windows login.
Never in the code, never in a plain file. Windows keeps at most ~2.5 KB per entry,
so longer values are split into numbered pieces."""
from __future__ import annotations

SERVICE = "arbitraza"
CHUNK = 1000  # characters per entry (Windows: 2560 bytes, stored as UTF-16)
_SPLIT = "#parts:"


class VaultError(RuntimeError):
    pass


def _kr():
    try:
        import keyring
        from keyring.backends import fail
    except ImportError as e:
        raise VaultError("nije instalirana biblioteka keyring (pip install keyring)") from e
    if isinstance(keyring.get_keyring(), fail.Keyring):
        raise VaultError("na ovom računaru nema sigurnog skladišta za ključeve (keyring)")
    return keyring


def available() -> tuple[bool, str]:
    try:
        return True, type(_kr().get_keyring()).__name__
    except VaultError as e:
        return False, str(e)


def get(name: str) -> str | None:
    kr = _kr()
    value = kr.get_password(SERVICE, name)
    if value is None or not value.startswith(_SPLIT):
        return value
    parts = [kr.get_password(SERVICE, f"{name}#{i}") for i in range(int(value[len(_SPLIT):]))]
    return None if any(p is None for p in parts) else "".join(parts)


def put(name: str, value: str) -> None:
    kr = _kr()
    delete(name)
    if len(value) <= CHUNK:
        kr.set_password(SERVICE, name, value)
        return
    parts = [value[i:i + CHUNK] for i in range(0, len(value), CHUNK)]
    for i, p in enumerate(parts):
        kr.set_password(SERVICE, f"{name}#{i}", p)
    kr.set_password(SERVICE, name, f"{_SPLIT}{len(parts)}")


def delete(name: str) -> None:
    kr = _kr()
    from keyring.errors import PasswordDeleteError

    old = kr.get_password(SERVICE, name)
    if old is None:
        return
    if old.startswith(_SPLIT):
        for i in range(int(old[len(_SPLIT):])):
            try:
                kr.delete_password(SERVICE, f"{name}#{i}")
            except PasswordDeleteError:
                pass
    try:
        kr.delete_password(SERVICE, name)
    except PasswordDeleteError:
        pass
