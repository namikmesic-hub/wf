"""Connection: the connection string of your ClickHouse, `url` under [metrics] in config.toml.

    https://user:password@host:port/database      (http://... for a ClickHouse on this machine)

wf speaks ClickHouse's HTTP interface with Python's standard library, so the scheme is http or
https. ClickHouse's own clickhouse:// strings name its native TCP port instead. The user, the
password and the database use percent-encoding: %40 for @, %3A for :, %2F for / and %23 for #.

The password is the one secret of config.toml. str() and repr() of a Connection give `masked`, the
string with *** for the password, and so does every message and `wf config show`. wf shows the
string only when it parses and has a user and a password, since wf then knows where the password
is. For the same reason, a config.toml that sets the string at all must be the user's alone
(load.py).
"""

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

FORM = "https://user:password@host:port/database"
SCHEMES = ("http", "https")
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")  # RFC 3986: a password written before :// doesn't match
MASK = "***"
LOGIN = ("the user", "the password")
ESCAPED = str.maketrans({"/": "%2F", "?": "%3F", "#": "%23"})  # in the login: urlsplit would end the host at them
BRACKETS = str.maketrans({"[": "%5B", "]": "%5D"})
SEPARATORS = ":/?#@"
FOLDED = "has a character that isn't ASCII, such as a full-width colon, in place of a :, /, ?, # or @"
BROKEN = "(not shown, because it doesn't parse)"
UNSURE = "(not shown, because it lacks the user or the password)"


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
    broken: tuple[str, ...] = ()  # why it doesn't parse, in words
    scheme_problem: str = ""  # a scheme that isn't http or https

    @classmethod
    def parse(cls, text: str) -> "Connection":
        """The parts of the text in the order of FORM, also when it doesn't parse. The first :// ends the
        scheme, the last @ ends the user and the password, and the next / ends the host and the port. Text
        before :// that isn't a scheme can hold the login before its last @. But with an @ after the ://,
        the login can start on either side of it, so wf names no missing login. Before the database, wf
        reads the ASCII character for each character that NFKC folds into a :, /, ?, # or @."""
        head, sep, rest = text.partition("://")
        if sep and head and not SCHEME.fullmatch(head):
            after = cls.parse("http://" + rest)
            folds = _gains(head, _fold(head)) or FOLDED in after.broken
            head, rest = _fold(head), _plain(rest)
            user, _, password = head.rpartition("@")[0].partition(":")
            lacking = () if "@" in rest else tuple(part for part, value in zip(LOGIN, (user, password)) if not value)
            return cls(text, broken=("has text before :// that isn't a scheme",) + ((FOLDED,) if folds else ()),
                       missing=lacking + tuple(part for part in after.missing if part not in LOGIN))
        scheme, rest = (head.lower(), rest) if sep else ("", text)
        login, at, place = rest.rpartition("@")
        try:
            parts = urlsplit("//" + login.translate(ESCAPED) + at + place)
            host = parts.hostname or ""
        except ValueError:  # brackets that hold no IPv6 address, or a character that NFKC folds into a separator
            folded = text.removesuffix(rest) + _plain(rest)
            why = FOLDED if _gains(text, folded) else f"doesn't parse as {FORM}"
            plain = folded if folded != text else text.translate(BRACKETS)  # an IPv6 host needs its brackets
            return cls(text, broken=(why,), missing=cls.parse(plain).missing if plain != text else ())
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
        if "/" in login:
            broken.append("has an @ after the host, so a / in the user or the password isn't percent-encoded")
        names = [name for name in parts.path.split("/") if name]
        if len(names) > 1:
            broken.append("has more than one name after the port, and the database is one name")
        found = {"scheme": scheme, "user": unquote(parts.username or ""), "password": unquote(parts.password or ""),
                 "host": host, "port": port or None, "database": unquote(names[0]) if names else ""}
        missing = tuple(f"the {name}" for name, value in found.items()
                        if not value and not (name == "port" and port == 0) and not (name == "database" and names))
        problem = "" if scheme in SCHEMES or not scheme else (
            "has a scheme other than http and https, and wf speaks ClickHouse's HTTP interface: "
            "a clickhouse:// string names the native TCP port")
        return cls(text, **found, missing=missing, broken=tuple(broken), scheme_problem=problem)

    @property
    def problems(self) -> list[str]:
        """What keeps it from being a whole connection string, in words, as "lacks the user and the port"."""
        found = list(self.broken) + ([f"lacks {_listed(self.missing)}"] if self.missing else [])
        return found + ([self.scheme_problem] if self.scheme_problem else [])

    @property
    def visible(self) -> bool:
        """Can wf show it, with *** for the password? Only when it parses and has a user and a password."""
        return not self.broken and bool(self.user and self.password)

    @property
    def masked(self) -> str:
        """The text, with *** for the password, or why wf doesn't show it."""
        if not self.visible:
            return BROKEN if self.broken else UNSURE
        head, sep, rest = self.text.partition("://")
        if not sep:
            head, rest = "", self.text
        place, slash, path = rest.partition("/")
        login, _, host = place.rpartition("@")
        return f"{head}{sep}{login.partition(':')[0]}:{MASK}@{host}{slash}{path}"

    def __str__(self) -> str:
        return self.masked

    def __repr__(self) -> str:
        return f"Connection({self.masked!r})"


def _listed(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _fold(text: str) -> str:
    """The text in the NFKC form that urlsplit checks, where a full-width colon is a colon."""
    return unicodedata.normalize("NFKC", text)


def _plain(rest: str) -> str:
    """The text after ://, folded up to the database: up to the first /, ? or # after the last @. urlsplit
    checks only that part, so it reads a full-width @ in the database as it is."""
    place = rest.rpartition("@")[2]
    cut = len(rest) - len(place) + re.match(r"[^/?#]*", place).end()
    return _fold(rest[:cut]) + rest[cut:]


def _gains(text: str, folded: str) -> bool:
    """Does the fold add a :, /, ?, # or @ to the text?"""
    return any(folded.count(separator) > text.count(separator) for separator in SEPARATORS)
