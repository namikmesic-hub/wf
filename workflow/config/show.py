"""Show: `wf config show`, the value of each key and where it comes from.

It never shows the password of metrics.url, and shows the URL only masked (connection.py).
"""

from .connection import Connection
from .keys import KEYS, Config, Key


def lines(config: Config) -> list[str]:
    rows = [(name, _shown(config.value(name), key), str(config.source(name))) for name, key in KEYS.items()]
    names, values = (max(len(row[column]) for row in rows) for column in (0, 1))
    there = "" if config.file.is_file() else " (there's no such file, so no value comes from it)"
    return [f"{config.file}{there}", *(f"{name:<{names}}  {value:<{values}}  {source}" for name, value, source in rows)]


def _shown(value: object, key: Key) -> str:
    if value is None:
        return f"none ({key.none})" if key.none else "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Connection):
        return value.masked
    return str(value)
