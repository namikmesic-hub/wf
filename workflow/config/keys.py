"""Keys: every key of config.toml, as the fields of typed sections.

Each field that key() makes, in Config and in its sections, is one key. A key has a kind (the values
that it allows) and a default, and it can have an environment variable and a command-line flag. Here
and in messages, a key in a section has the name <section>.<field>, as `cursor.runtime`. In the file
it is `runtime` under [cursor]. KEYS lists them all, in the order that `wf config show` prints them.
"""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

from .connection import FORM, Connection


class ConfigError(Exception):
    """wf can't start with this configuration. Each problem is one message, which names the key or
    the variable, the file, and what the key allows."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


class Wrong(ValueError):
    """A value that a key's kind doesn't allow."""


class Kind:
    allowed = ""  # what the kind allows, in words, as "a whole number of 1 or more"
    allowed_in_text = ""  # what it allows in a variable or a flag, where that is another thing

    def from_toml(self, value: object) -> object:
        """The value, from tomllib. Raises Wrong."""
        raise NotImplementedError

    def from_text(self, text: str) -> object:
        """The value, from an environment variable or a flag (stripped, never blank). Raises Wrong."""
        raise NotImplementedError


class Whole(Kind):
    def __init__(self, low: int, high: int | None = None):
        self.low, self.high = low, high
        self.allowed = f"a whole number of {low} or more" if high is None else f"a whole number from {low} to {high}"

    def from_toml(self, value: object) -> int:
        if isinstance(value, int) and not isinstance(value, bool) and value >= self.low and (self.high is None or value <= self.high):
            return value
        raise Wrong

    def from_text(self, text: str) -> int:
        try:
            value = int(text) if text.isascii() and text.isdigit() else None
        except ValueError:  # more digits than int() reads
            value = None
        return self.from_toml(value)


class Seconds(Kind):
    allowed = "a number of seconds above 0"

    def from_toml(self, value: object) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                seconds = float(value)
            except OverflowError:  # a whole number too big for a float
                raise Wrong from None
            if math.isfinite(seconds) and seconds > 0:
                return value
        raise Wrong

    def from_text(self, text: str) -> float:
        try:
            return self.from_toml(float(text))
        except ValueError:
            raise Wrong from None


class Choice(Kind):
    def __init__(self, *options: str):
        self.options = options
        self.allowed = " or ".join(options)

    def from_toml(self, value: object) -> str:
        if isinstance(value, str) and value in self.options:
            return value
        raise Wrong

    from_text = from_toml


class Location(Kind):
    """A file or a folder. In config.toml it starts with / or ~, because wf reads the file in any
    folder. From a variable or a flag, a relative path is relative to the folder that wf runs in.
    A ~ names the home folder of a user that exists."""

    allowed = "a path that starts with / or with the ~ of a user"
    allowed_in_text = "a path, where ~ is the home folder of a user"

    def from_toml(self, value: object) -> Path:
        path = _expanded(value) if isinstance(value, str) else None
        if path is not None and path.is_absolute():
            return path
        raise Wrong

    def from_text(self, text: str) -> Path:
        path = _expanded(text)
        if path is None:
            raise Wrong
        return path


def _expanded(text: str) -> Path | None:
    """The path with its ~ expanded. None for an empty path, for a path with a NUL, and for one with
    the ~ of a user that doesn't exist. os.path leaves that ~ as it is, where pathlib raises RuntimeError."""
    if not text or "\0" in text:
        return None
    expanded = os.path.expanduser(text)
    return None if expanded.startswith("~") else Path(expanded)


class Boolean(Kind):
    allowed = "true or false"

    def from_toml(self, value: object) -> bool:
        if isinstance(value, bool):
            return value
        raise Wrong

    def from_text(self, text: str) -> bool:
        return self.from_toml({"true": True, "false": False}.get(text))


class ConnectionString(Kind):
    """A blank string is no connection string. Connection.parse() finds the parts, and a check of
    [metrics] needs them all when metrics.enabled is true (load.py)."""

    allowed = f"a connection string, as {FORM}"

    def from_toml(self, value: object) -> Connection | None:
        if isinstance(value, str):
            return Connection.parse(value) if value.strip() else None
        raise Wrong

    def from_text(self, text: str) -> Connection:
        return Connection.parse(text)


def key(kind: Kind, default: object = None, *, variable: str | None = None, flag: str | None = None,
        none: str = "", secret: bool = False, blank_is_default: bool = False):
    """A field that is a key: `none` says what no value means, for `wf config show`. A `secret` key's
    value is never in a message, and `wf config show` shows it masked. A blank variable counts as no
    variable, but with `blank_is_default` it gives the default, also over the file."""
    return field(default=default, metadata={"kind": kind, "variable": variable, "flag": flag, "none": none, "secret": secret,
                                            "blank_is_default": blank_is_default})


@dataclass(frozen=True)
class Claude:
    """[claude]: how wf runs Claude Code (harnesses/claude_code.py)."""

    auth: str = key(Choice("subscription", "api-key"), "subscription", variable="WF_CLAUDE_AUTH")


@dataclass(frozen=True)
class Cursor:
    """[cursor]: how wf runs Cursor (harnesses/cursor.py)."""

    runtime: str = key(Choice("sdk", "acp"), "sdk", variable="WF_CURSOR_RUNTIME")
    api_key_file: Path | None = key(Location(), none="the macOS Keychain")


@dataclass(frozen=True)
class UI:
    """[ui]: wf ui, which listens on 127.0.0.1 only (ui.py)."""

    port: int = key(Whole(0, 65535), 8765, flag="wf ui --port")


@dataclass(frozen=True)
class Tools:
    """[tools]: the sign-in of the tools that agents use (tools/signin.py)."""

    signin_port: int = key(Whole(1, 65535), 47152, flag="wf tools <name> setup --port")


@dataclass(frozen=True)
class Git:
    """[git]: the runner's commits (workspaces.py)."""

    signing_timeout: float = key(Seconds(), 30)


@dataclass(frozen=True)
class Paths:
    """[paths]: the programs that wf runs, for a machine with more than one install of one."""

    codex: Path | None = key(Location(), none="codex on PATH")
    cursor_agent: Path | None = key(Location(), none="cursor-agent on PATH")
    node: Path | None = key(Location(), none="node on PATH")


@dataclass(frozen=True)
class Metrics:
    """[metrics]: your own ClickHouse, for the records of runs. With enabled false, wf exports nothing."""

    enabled: bool = key(Boolean(), False)
    url: Connection | None = key(ConnectionString(), secret=True)


@dataclass(frozen=True)
class Source:
    """Where a key's value comes from: a flag, the environment, the file, or wf's default."""

    kind: str
    name: str = ""  # the flag or the variable

    def __str__(self) -> str:
        return f"{self.kind} ({self.name})" if self.name else self.kind


DEFAULT = Source("default")


@dataclass(frozen=True)
class Config:
    """The keys at the top of config.toml, a section for each of its tables, the file, and the source of each value.
    in_file holds what the file sets, by key name, also where a variable or a flag wins over it."""

    runs_dir: Path | None = key(Location(), variable="WF_RUNS_DIR", flag="--runs-dir", none=".wf/runs in each repo",
                                blank_is_default=True)  # WF_RUNS_DIR= finds a run in .wf/runs (runs.Run.command)
    max_agents: int | None = key(Whole(1), variable="WF_MAX_AGENTS", none="no cap")
    claude: Claude = field(default_factory=Claude)
    cursor: Cursor = field(default_factory=Cursor)
    ui: UI = field(default_factory=UI)
    tools: Tools = field(default_factory=Tools)
    git: Git = field(default_factory=Git)
    paths: Paths = field(default_factory=Paths)
    metrics: Metrics = field(default_factory=Metrics)
    file: Path = Path("config.toml")  # the config.toml that load() looked for
    sources: Mapping[str, Source] = field(default_factory=dict, repr=False, compare=False)
    in_file: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    def source(self, name: str) -> Source:
        return self.sources.get(name, DEFAULT)

    def value(self, name: str) -> object:
        """A key's value by its name, as cursor.runtime."""
        section, _, leaf = name.rpartition(".")
        return getattr(getattr(self, section) if section else self, leaf)

    def given(self, flags: Mapping[str, object]) -> "Config":
        """This configuration with the values of flags, by key name. A flag wins over each other source.
        A flag with the value None leaves its key as it is."""
        chosen = {name: value for name, value in flags.items() if value is not None}
        if not chosen:
            return self
        if unknown := set(chosen) - set(KEYS):
            raise ValueError(f"{', '.join(sorted(unknown))} isn't in config.KEYS")
        values = {name: self.value(name) for name in KEYS} | chosen
        sources = {**self.sources, **{name: Source("flag", KEYS[name].flag) for name in chosen}}
        return build(values, sources, self.file, self.in_file)


@dataclass(frozen=True)
class Key:
    name: str  # cursor.runtime
    kind: Kind
    default: object
    variable: str | None  # its environment variable
    flag: str | None  # its command-line flag, as `wf ui --port`
    none: str  # what no value means
    secret: bool
    blank_is_default: bool

    @property
    def section(self) -> str:
        """Its table in config.toml, or "" at the top of the file."""
        return self.name.rpartition(".")[0]

    @property
    def leaf(self) -> str:
        """Its name in its table."""
        return self.name.rpartition(".")[2]


SECTIONS = {top.name: top.default_factory for top in fields(Config) if is_dataclass(top.type)}


def _keys() -> dict[str, Key]:
    found = {}
    for top in fields(Config):
        members = [(f"{top.name}.{one.name}", one) for one in fields(top.type)] if top.name in SECTIONS else [(top.name, top)]
        for name, one in members:
            if "kind" in one.metadata:
                found[name] = Key(name, default=one.default, **one.metadata)
    return found


KEYS = _keys()


def build(values: Mapping[str, object], sources: Mapping[str, Source], file: Path,
          in_file: Mapping[str, object] | None = None) -> Config:
    """A Config from each key's value, by name, and the values that the file sets."""
    parts = {name: values[name] for name in KEYS if not KEYS[name].section}
    for section, make in SECTIONS.items():
        parts[section] = make(**{one.name: values[f"{section}.{one.name}"] for one in fields(make)})
    return Config(**parts, file=file, sources=dict(sources), in_file=dict(in_file or {}))
