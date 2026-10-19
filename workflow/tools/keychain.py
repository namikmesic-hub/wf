"""Keychain: where tool sign-ins are kept, through the system keyring.

On macOS that's your login Keychain: one item per tool, with the service "wf tools" and the
tool's name as the account, holding the sign-in as JSON. Nothing else keeps it: not the run
folder, not a workflow file, and not the environment that agents and their commands get.
Tests swap in a fake keyring with keyring.set_keyring().
"""

import json

import keyring
from keyring.errors import KeyringError

SERVICE = "wf tools"


class Unavailable(Exception):
    """There's no keyring to keep secrets in, or it refused (a locked Keychain)."""


def load(name: str) -> dict | None:
    """The stored sign-in for a tool, or None if it isn't set up."""
    try:
        text = keyring.get_password(SERVICE, name)
    except KeyringError as error:
        raise Unavailable(f"can't read the {name} sign-in from the keyring: {_reason(error)}") from error
    if text is None:
        return None
    try:
        stored = json.loads(text)
    except json.JSONDecodeError:
        return None  # not something wf wrote: setup replaces it
    return stored if isinstance(stored, dict) and stored.get("access_token") else None


def can_write(stored: dict) -> bool:
    """Does a stored sign-in allow writes? Only one that setup asked for reads alone doesn't."""
    return stored.get("write", True)


def save(name: str, stored: dict) -> None:
    try:
        keyring.set_password(SERVICE, name, json.dumps(stored))
    except KeyringError as error:
        raise Unavailable(f"can't save the {name} sign-in in the keyring: {_reason(error)}") from error


def _reason(error: KeyringError) -> str:
    return str(error) or type(error).__name__
