"""Harnesses: the adapters that drive coding agents.

Read base.py first (the Harness and Session interfaces), then the adapters:
    claude_code.py   Claude Code, through the Claude Agent SDK
    codex.py         Codex, through `codex app-server` (JSON-RPC over stdio)
    cursor.py        Cursor, through `cursor-agent acp` (the Agent Client Protocol
                     over stdio), with mcp_http.py serving it submit_result

KEPT_INSTRUCTIONS names what a harness still gives its agent when a node sets
`project_instructions: false`, which `wf validate` warns about.

KEYCHAIN_SHELL names the access levels at which a harness's agent runs commands
that can reach the macOS Keychain, and so every stored sign-in of wf tools: no
sandbox at all, or Codex's danger-full-access. A node with `tools` runs there
only with `tools_with_shell: true` (validate.py, nodes.py).

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


KEYCHAIN_SHELL = {"claude-code": ("edit", "full"), "cursor": ("edit", "full"), "codex": ("full",)}

KEPT_INSTRUCTIONS = {  # what a harness still loads with `project_instructions: false`
    "codex": "your global ~/.codex/AGENTS.md, or the skills in your home folder (~/.agents/skills, ~/.codex/skills)",
    "cursor": "your Cursor account's User Rules, the skills in your home folder (~/.claude/skills, ~/.agents/skills "
              "and others), or the repo's hooks in .cursor/hooks.json and .claude/settings.json",
}


def reaches_keychain(harness: str, access: str) -> bool:
    return access in KEYCHAIN_SHELL.get(harness, ())


def create(name: str) -> Harness:
    if name not in FACTORIES:
        raise Unavailable(f"the `{name}` harness isn't supported yet")
    return FACTORIES[name]()


__all__ = ["FACTORIES", "KEPT_INSTRUCTIONS", "KEYCHAIN_SHELL", "reaches_keychain", "Harness", "Session", "SessionSpec", "TurnEnd", "Unavailable", "Usage", "create"]
