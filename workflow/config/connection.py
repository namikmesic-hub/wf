"""Connection: the connection string of your ClickHouse, `url` under [metrics] in config.toml.

    https://user:password@host:port/database      (http://... for a ClickHouse on this machine)

wf speaks ClickHouse's HTTP interface with Python's standard library, so the scheme is http or
https. ClickHouse's own clickhouse:// strings name its native TCP port instead. The user, the
password and the database are percent-encoded: %40 for @, %3A for :, %2F for / and %23 for #.

The password is the one secret of config.toml. str() and repr() of a Connection give `masked`, the
string with *** for the password, and so does every message and `wf config show`. A string that
doesn't parse isn't shown at all, because wf can't tell which part of it is the password.
"""

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

FORM = "https://user:password@host:port/database"
SCHEMES = ("http", "https")
MASK = "***"
HIDDEN = "(not shown, because it doesn't parse)"


@dataclass(frozen=True, repr=False)
class Connection:
    text: str
    scheme: str = ""
    user: str = ""
    password: str = ""
    host: str = ""
    port: int | None = None
    database: str = ""
    missing: tuple[str, ...] = ()  # the parts that it lacks, as "the user"
    broken: tuple[str, ...] = ()  # why it doesn't parse, in words: then it is never shown
    scheme_problem: str = ""  # a scheme that isn't http or https

    @classmethod
    def parse(cls, text: str) -> "Connection":
        head, sep, rest = text.partition("://")
        scheme, rest = (head.lower(), rest) if sep else ("", text)
        try:
            parts = urlsplit("//" + rest)
            host = parts.hostname or ""
        except ValueError:  # brackets that hold no IPv6 address
            return cls(text, broken=(f"doesn't parse as {FORM}",))
        broken = []
        if any(c.isspace() or not c.isprintable() for c in text):
            broken.append("has a space or a control character that isn't percent-encoded")
        if "?" in rest or "#" in rest:
            broken.append("has a ? or a # that isn't percent-encoded")
        try:
            port = parts.port
        except ValueError:  # not a number, or above 65535
            port = 0
        if port == 0:
            broken.append("has a port that isn't a number from 1 to 65535")
        names = [name for name in parts.path.split("/") if name]
        if len(names) > 1:
            broken.append("has more than one name after the port, and the database is one name")
        found = {"scheme": scheme, "user": unquote(parts.username or ""), "password": unquote(parts.password or ""),
                 "host": host, "port": port or None, "database": unquote(names[0]) if names else ""}
        missing = tuple(f"the {name}" for name, value in found.items()
                        if not value and not (name == "port" and port == 0) and not (name == "database" and names))
        problem = "" if scheme in SCHEMES or not scheme else (
            f"has the scheme {scheme}, and wf speaks ClickHouse's HTTP interface: use http or https, "
            "because a clickhouse:// string names the native TCP port")
        return cls(text, **found, missing=missing, broken=tuple(broken), scheme_problem=problem)

    @property
    def problems(self) -> list[str]:
        """What keeps it from being a whole connection string, in words, as "lacks the user and the port".
        For a string that doesn't parse, why it doesn't: the parts that it seems to lack may be there."""
        found = list(self.broken) or ([f"lacks {_listed(self.missing)}"] if self.missing else [])
        return found + ([self.scheme_problem] if self.scheme_problem else [])

    @property
    def masked(self) -> str:
        """The string as it was written, with *** for the password. Everything from the scheme to the
        last @ is the user and the password, so a raw @ or / in the password stays hidden too."""
        if self.broken:
            return HIDDEN
        head, sep, rest = self.text.partition("://")
        if not sep:
            head, rest = "", self.text
        login, at, place = rest.rpartition("@")
        if not at or ":" not in login:
            return self.text
        return f"{head}{sep}{login.partition(':')[0]}:{MASK}@{place}"

    def __str__(self) -> str:
        return self.masked

    def __repr__(self) -> str:
        return f"Connection({self.masked!r})"


def _listed(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"
