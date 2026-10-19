"""Harnesses: the adapters that drive coding agents.

Read base.py first (the Harness and Session interfaces), then the adapters:
    claude_code.py   Claude Code, through the Claude Agent SDK
    codex.py         Codex, through `codex app-server` (JSON-RPC over stdio)
    cursor.py        Cursor, through `cursor-agent acp` (the Agent Client Protocol
                     over stdio), with mcp_http.py serving it submit_result

create() builds a harness by name. FACTORIES maps names to constructors and
imports each adapter only when it's first used, so a workflow that never runs
Claude Code never imports the Claude Agent SDK. Tests swap in fakes by patching
FACTORIES.
"""

from collections.abc import Callable

from .base import Harness, Session, SessionSpec, TurnEnd, Unavailable, Usage


def _claude_code() -> Harness:
    from .claude_code import ClaudeCode

    return ClaudeCode()


def _codex() -> Harness:
    from .codex import Codex

    return Codex()


def _cursor() -> Harness:
    from .cursor import Cursor

    return Cursor()


FACTORIES: dict[str, Callable[[], Harness]] = {"claude-code": _claude_code, "codex": _codex, "cursor": _cursor}


def create(name: str) -> Harness:
    if name not in FACTORIES:
        raise Unavailable(f"the `{name}` harness isn't supported yet")
    return FACTORIES[name]()


__all__ = ["FACTORIES", "Harness", "Session", "SessionSpec", "TurnEnd", "Unavailable", "Usage", "create"]
