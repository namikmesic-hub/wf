"""Settings: wf's own settings for the user, in ~/.wf/config.yaml (WF_HOME moves ~/.wf).

    max_agents: 4      # the cap on live agent sessions across all runs (capacity.py)
    cursor_runtime: sdk                      # or acp, for one release (harnesses/cursor.py)
    cursor_api_key_file: ~/.config/cursor-key  # a file with the Cursor API key, in place of the Keychain
    telemetry:
      clickhouse_url: http://127.0.0.1:8123  # where `wf metrics export` writes runs (export.py)

This module owns the file: get(name) reads one setting, and the feature that
uses it checks its value. KNOWN lists every setting wf reads, so a misspelt
name is an error rather than a setting quietly left out. A feature that adds
a setting adds its name to KNOWN.
"""

from pathlib import Path

import yaml

from .workspaces import home

KNOWN = ("max_agents", "cursor_runtime", "cursor_api_key_file", "telemetry")


class SettingsError(Exception):
    """config.yaml can't be read, or names a setting wf doesn't know."""


def path() -> Path:
    return home() / "config.yaml"


def get(name: str, missing: object = None) -> object:
    """Setting `name`, or `missing` when the file or the setting isn't there (a setting
    written with no value is there, as None). Raises SettingsError."""
    if name not in KNOWN:
        raise ValueError(f"`{name}` isn't in settings.KNOWN")
    return _read().get(name, missing)


def _read() -> dict:
    file = path()
    try:
        settings = yaml.safe_load(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise SettingsError(f"can't read {file}: {error}") from None
    if settings is None:  # an empty file
        return {}
    if not isinstance(settings, dict):
        raise SettingsError(f"{file} must hold settings by name, like `max_agents: 4`")
    unknown = [str(name) for name in settings if name not in KNOWN]
    if unknown:
        raise SettingsError(f"{file}: unknown setting {', '.join(unknown)} (known: {', '.join(KNOWN)})")
    return settings
