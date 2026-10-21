"""Legacy: ~/.wf/config.yaml, where wf kept its settings before config.toml.

wf doesn't read it, and doesn't start while it is there, so that no setting in it quietly stops
applying. The message names each setting that the file holds, and the key of config.toml that takes
its place. It never shows a value.
"""

from pathlib import Path

import yaml

from .keys import KEYS

NAME = "config.yaml"
MOVED = {"max_agents": "max_agents", "cursor_runtime": "cursor.runtime", "cursor_api_key_file": "cursor.api_key_file"}


def problems(old: Path, new: Path) -> list[str]:
    """Nothing when there's no config.yaml, else one problem: what to do, with a line for each setting."""
    if not old.exists() and not old.is_symlink():
        return []
    head = f"{old} is still there, and wf reads its settings only from {new} now"
    try:
        settings = yaml.safe_load(old.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        why = error.strerror if isinstance(error, OSError) and error.strerror else "it isn't UTF-8 text"
        return [f"{head}. wf can't read it ({why}), so move its settings to config.toml yourself, then delete {old}"]
    except yaml.YAMLError:
        return [f"{head}. It isn't valid YAML, so wf can't name its settings: move them to config.toml yourself, "
                f"then delete {old}"]
    if not isinstance(settings, dict) or not settings:
        return [f"{head}. It holds no settings, so delete it"]
    lines = [f"  {name}: {_place(MOVED.get(str(name)))}" for name in settings]
    return [f"{head}. Move each of its settings to its key in config.toml, then delete {old}:\n" + "\n".join(lines)]


def _place(name: str | None) -> str:
    if name is None:
        return "no key of config.toml takes it"
    found = KEYS[name]
    return f"{found.leaf} under [{found.section}]" if found.section else f"{found.leaf}, at the top of config.toml"
