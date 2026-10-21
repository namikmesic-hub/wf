"""Load: config.toml, the environment and wf's defaults, as one Config, or every problem at once.

Each key's value comes from its environment variable, else from config.toml, else from wf's
default. A flag wins over all three (Config.given). A variable that is set but blank counts as not
set. A blank WF_RUNS_DIR is the one that gives its default, also over the file: so
`WF_RUNS_DIR= wf retry <run-id>` finds a run in .wf/runs. The file is checked all the same.

The checks: config.yaml isn't there (legacy.py), the file is valid TOML, each key is one that wf
knows and has a value of its kind, and metrics.url is a whole connection string when
metrics.enabled is true. The file also belongs to the user, and no other user may change it,
because it names programs that wf runs. A file that sets metrics.url is the user's alone
(mode 0600), because the password goes there.
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
    table, facts = _read(file)
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
        text = environ.get(key.variable, "").strip() if key.variable is not None else ""
        if text or (key.blank_is_default and key.variable in environ):
            source = Source("environment", key.variable)
            try:
                value = key.kind.from_text(text) if text else key.default
            except Wrong:
                problems.append(f"{key.variable} must be {key.kind.allowed_in_text or key.kind.allowed}, "
                                f"not `{_cut(text)}`")
        values[key.name], sources[key.name] = value, source
    problems += _metrics(values, file) + _guarded(table, facts, file)
    if problems:
        raise ConfigError(problems)
    return build(values, sources, file)


def _read(file: Path) -> tuple[dict, os.stat_result | None]:
    """The file's table, and its mode and owner, or ({}, None) when there's no file."""
    try:
        with file.open("rb") as opened:
            facts = os.fstat(opened.fileno())
            data = opened.read()
    except FileNotFoundError:
        return {}, None
    except OSError as error:
        raise ConfigError([f"can't read {file}: {error.strerror or error}"]) from None
    try:
        return tomllib.loads(data.decode("utf-8")), facts
    except UnicodeDecodeError:
        raise ConfigError([f"{file} isn't UTF-8 text"]) from None
    except tomllib.TOMLDecodeError as error:  # it names a character only when it can't be in TOML at all
        said = re.sub(r"^Illegal character .*? \(", "Illegal character (", str(error))
        raise ConfigError([f"{file} isn't valid TOML: {said}"]) from None
    except ValueError:  # Python reads no whole number of more than 4,300 digits
        raise ConfigError([f"{file} isn't valid TOML for Python: it has a number with too many digits"]) from None


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
            problems += [f"{file}: unknown key {name}.{_quoted(inner)}. [{name}] takes {_listed(_leaves(name))}"
                         f"{_hint(inner)}" for inner in value if f"{name}.{inner}" not in KEYS]
        elif name not in top:  # also "ui.port" in quotes, which TOML reads as one key at the top
            problems.append(f"{file}: unknown key {_quoted(name)}. The top of the file takes {_listed(top)}, and the "
                            f"tables {_listed(f'[{section}]' for section in SECTIONS)}{_hint(name)}")
    return problems


def _leaves(section: str) -> list[str]:
    return [key.leaf for key in KEYS.values() if key.section == section]


def _quoted(name: str) -> str:
    """A key as TOML writes it: in quotes when it holds a dot, as "ui.port" at the top of the file."""
    return f'"{name}"' if "." in name else name


def _hint(name: str) -> str:
    """Where a key with this name goes, when it is in another place."""
    places = [key for key in KEYS.values() if name in (key.leaf, key.name)]
    if len(places) != 1:
        return ""
    found = places[0]
    return (f" ({found.leaf} goes under [{found.section}])" if found.section else
            f" ({found.leaf} goes at the top of the file, above the first table)")


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
    shown = f" ({url.masked})" if url.visible else ""
    return [f"{file}: metrics.enabled is true, and metrics.url{shown} {_listed(url.problems)}. Write it as {FORM}, "
            "with each @, :, /, ? and # in the user and the password percent-encoded"]


def _guarded(table: dict, facts: os.stat_result | None, file: Path) -> list[str]:
    """The file is the user's, and no other user can change it, since it names the programs that wf
    runs. When it sets metrics.url, no other user can open it either: wf can't tell a URL without a
    password from one with a password where wf reads the host, the port or the database."""
    if facts is None:
        return []
    mode, url = stat.S_IMODE(facts.st_mode), _lookup(table, KEYS["metrics.url"])
    if facts.st_uid not in (os.getuid(), 0):
        return [f"{file} belongs to another user, who can change the programs that wf runs. Use a config.toml of "
                f"your own, or set WF_HOME to a folder of yours"]
    if mode & 0o077 and isinstance(url, str) and url.strip():
        return [f"{file} sets metrics.url, where the password of ClickHouse goes, and its mode {mode:04o} lets "
                f"other users open it. Make it yours alone: chmod 600 {file}"]
    if mode & 0o022:
        return [f"{file} names the programs that wf runs, and its mode {mode:04o} lets other users change it. "
                f"Make it yours: chmod go-w {file}"]
    return []


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
    return f"`{_cut(json.dumps(value) if isinstance(value, str) else str(value))}`"


def _cut(text: str) -> str:
    """A value for a message: a long one, as a number of 300 digits, is cut to its start."""
    return text if len(text) <= 60 else f"{text[:57]}..."


def _listed(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"
