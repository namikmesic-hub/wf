"""Config: wf's settings for the user, in ~/.wf/config.toml (WF_HOME moves ~/.wf). wf never writes it.

    runs_dir = "~/wf-runs"        # run folders outside the repos, a folder per repo (runs.py)
    max_agents = 4                # the cap on live agent sessions across all runs (capacity.py)

    [claude]
    auth = "subscription"         # or "api-key": ANTHROPIC_API_KEY instead (harnesses/claude_code.py)

    [cursor]
    runtime = "sdk"               # or "acp", for one release (harnesses/cursor.py)
    api_key_file = "~/.config/cursor-key"   # the Cursor API key, in place of the macOS Keychain

    [ui]
    port = 8765                   # where wf ui listens, on 127.0.0.1 (ui.py)

    [tools]
    signin_port = 47152           # where wf tools <name> setup waits for the browser (tools/signin.py)

    [git]
    signing_timeout = 30          # the seconds that signing a commit may take (workspaces.py)

    [paths]
    codex = "/opt/homebrew/bin/codex"   # and cursor_agent and node: else wf finds each on PATH

    [metrics]
    enabled = true
    url = "https://wf:password@clickhouse.example.com:8443/wf"   # your ClickHouse (connection.py)

Read the modules in this order:
    keys.py        every key: the typed sections, and each key's kind, default, variable and flag
    connection.py  metrics.url: a connection string of ClickHouse, with its password masked
    load.py        one precedence for every key (flag, variable, file, default), and the checks
    legacy.py      config.yaml, which stops wf until its settings are in config.toml
    show.py        wf config show

cli.main() loads the configuration when a command starts, and stops with its problems when it has
some. Then it pins the configuration for that command, so get() gives each caller the same values.
A change to the file counts from the next command on. Outside a command, get() loads it at each
call. No other module reads config.toml or the environment variable of a key.
"""

import argparse
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

from . import show
from .connection import Connection
from .keys import KEYS, Claude, Config, ConfigError, Cursor, Git, Key, Metrics, Paths, Source, Tools, UI, Wrong
from .load import FILE, home, load, path

_pinned: list[Config] = []  # of the commands that run in this process, the latest last


def get(flags: Mapping[str, object] | None = None) -> Config:
    """The configuration of the command that runs, or else load()'s now, with the values of flags
    (by key name) on top. Raises ConfigError, only when no command pinned one."""
    found = _pinned[-1] if _pinned else load()
    return found.given(flags) if flags else found


@contextmanager
def pinned(config: Config) -> Iterator[Config]:
    """Inside this block, get() gives `config`. Each block takes out only its own, in any order."""
    _pinned.append(config)
    try:
        yield config
    finally:
        _pinned[:] = [one for one in _pinned if one is not config]


def flag(name: str) -> Callable[[str], object]:
    """The argparse type of a key's flag: the key's kind reads the text. A blank flag is no flag."""
    kind = KEYS[name].kind

    def read(text: str) -> object:
        if not text.strip():
            return None
        try:
            return kind.from_text(text.strip())
        except Wrong:
            raise argparse.ArgumentTypeError(f"`{text}` isn't {kind.allowed_in_text or kind.allowed}") from None

    read.__name__ = name
    return read


__all__ = ["FILE", "KEYS", "Claude", "Config", "ConfigError", "Connection", "Cursor", "Git", "Key", "Metrics", "Paths",
           "Source", "Tools", "UI", "flag", "get", "home", "load", "path", "pinned", "show"]
