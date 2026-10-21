"""Load: config.toml, the environment and wf's defaults, as one Config, or every problem at once.

Each key's value comes from its environment variable, else from config.toml, else from wf's
default. A flag wins over all three (Config.given). A variable that is set but blank gives its key
wf's default, also when the file sets the key: so `WF_RUNS_DIR= wf retry <run-id>` finds a run in
.wf/runs. The file is checked all the same.

The checks: config.yaml isn't there (legacy.py), the file is valid TOML, each key is one that wf
knows and has a value of its kind, metrics.url is a whole connection string when metrics.enabled
is true, and a file that holds the password of metrics.url is the user's alone (mode 0600).
"""

import json
import os
import re
import stat
import tomllib
from collections.abc import Mapping
from datetime import date, datetime, time
from pathlib import Path

from . import legacy
from .connection import FORM, Connection
from .keys import DEFAULT, KEYS, SECTIONS, Config, ConfigError, Key, Source, Wrong, build

FILE = "config.toml"
_MISSING = object()


def home() -> Path:
    """~/.wf, or WF_HOME: wf's own folder, per user."""
    return Path(os.environ.get("WF_HOME") or Path.home() / ".wf")


def path() -> Path:
    return home() / FILE


def load(folder: Path | None = None, environ: Mapping[str, str] | None = None) -> Config:
    """The configuration in `folder` (home()) and `environ` (os.environ). Raises ConfigError."""
    folder = home() if folder is None else folder
    environ = os.environ if environ is None else environ
    file = folder / FILE
    if problems := legacy.problems(folder / legacy.NAME, file):
        raise ConfigError(problems)
    table, mode = _read(file)
    problems = _unknown(table, file)
    values, sources = {}, {}
    for key in KEYS.values():
        value, source = key.default, DEFAULT
        found = _lookup(table, key)
        if found is not _MISSING:
            try:
                value, source = key.kind.from_toml(found), Source("file")
            except Wrong:
                problems.append(f"{file}: {key.name} must be {key.kind.allowed}, not {_shown(found, key)}")
        if key.variable is not None and key.variable in environ:
            text, source = environ[key.variable].strip(), Source("environment", key.variable)
            try:
                value = key.kind.from_text(text) if text else key.default
            except Wrong:
                problems.append(f"{key.variable} must be {key.kind.allowed}, not `{text}`")
        values[key.name], sources[key.name] = value, source
    problems += _metrics(values, file) + _private(table, mode, file)
    if problems:
        raise ConfigError(problems)
    return build(values, sources, file)


def _read(file: Path) -> tuple[dict, int | None]:
    """The file's table and mode, or ({}, None) when there's no file."""
    try:
        with file.open("rb") as opened:
            mode = os.fstat(opened.fileno()).st_mode
            data = opened.read()
    except FileNotFoundError:
        return {}, None
    except OSError as error:
        raise ConfigError([f"can't read {file}: {error.strerror or error}"]) from None
    try:
        return tomllib.loads(data.decode("utf-8")), mode
    except UnicodeDecodeError:
        raise ConfigError([f"{file} isn't UTF-8 text"]) from None
    except tomllib.TOMLDecodeError as error:  # it names a character only when it can't be in TOML at all
        said = re.sub(r"^Illegal character .*? \(", "Illegal character (", str(error))
        raise ConfigError([f"{file} isn't valid TOML: {said}"]) from None


def _lookup(table: dict, key: Key) -> object:
    inside = table.get(key.section, {}) if key.section else table
    return inside.get(key.leaf, _MISSING) if isinstance(inside, dict) else _MISSING


def _unknown(table: dict, file: Path) -> list[str]:
    """A problem for each key and table that wf doesn't know, and for a section that isn't a table."""
    top = [key.name for key in KEYS.values() if not key.section]
    problems = []
    for name, value in table.items():
        if name in SECTIONS and not isinstance(value, dict):
            problems.append(f"{file}: {name} must be a table, [{name}], with {_listed(_leaves(name))}")
        elif name in SECTIONS:
            problems += [f"{file}: unknown key {name}.{inner}. [{name}] takes {_listed(_leaves(name))}{_hint(inner)}"
                         for inner in value if f"{name}.{inner}" not in KEYS]
        elif name not in KEYS:
            problems.append(f"{file}: unknown key {name}. The top of the file takes {_listed(top)}, and the tables "
                            f"{_listed(f'[{section}]' for section in SECTIONS)}{_hint(name)}")
    return problems


def _leaves(section: str) -> list[str]:
    return [key.leaf for key in KEYS.values() if key.section == section]


def _hint(name: str) -> str:
    """Where a key with this name goes, when it is in another place."""
    places = [key for key in KEYS.values() if key.leaf == name]
    if len(places) != 1:
        return ""
    found = places[0]
    return (f" ({name} goes under [{found.section}])" if found.section else
            f" ({name} goes at the top of the file, above the first table)")


def _metrics(values: dict, file: Path) -> list[str]:
    """With metrics.enabled, metrics.url must hold every part of a connection string."""
    if not values["metrics.enabled"]:
        return []
    url: Connection | None = values["metrics.url"]
    if url is None:
        return [f"{file}: metrics.enabled is true, so metrics.url must be set to the connection string of your "
                f"ClickHouse, as {FORM}"]
    if not url.problems:
        return []
    shown = f" ({url.masked})" if not url.broken else ""
    return [f"{file}: metrics.enabled is true, and metrics.url{shown} {_listed(url.problems)}. Write it as {FORM}, "
            "with each @, :, /, ? and # in the user and the password percent-encoded"]


def _private(table: dict, mode: int | None, file: Path) -> list[str]:
    """A file that holds the password of metrics.url must be the user's alone."""
    url = _lookup(table, KEYS["metrics.url"])
    if mode is None or not mode & 0o077 or not isinstance(url, str) or not Connection.parse(url).password:
        return []
    return [f"{file} holds the password in metrics.url, and its mode {stat.S_IMODE(mode):04o} lets other users "
            f"open it. Make it yours alone: chmod 600 {file}"]


def _shown(value: object, key: Key) -> str:
    """A value from the file, for a message. A secret key's value is never shown, only its kind."""
    if isinstance(value, dict):
        return "a table"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, (datetime, date, time)):
        return "a date or a time"
    if key.secret:
        return {bool: "a boolean", int: "a number", float: "a number"}.get(type(value), "a string")
    if isinstance(value, bool):
        return f"`{'true' if value else 'false'}`"
    return f"`{json.dumps(value) if isinstance(value, str) else value}`"


def _listed(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"
