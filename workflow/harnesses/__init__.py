"""Harnesses: the adapters that drive coding agents.

Read base.py first (the Harness and Session interfaces), then the adapters:
    claude_code.py   Claude Code, through the Claude Agent SDK
    codex.py         Codex, through `codex app-server` (JSON-RPC over stdio)
    remote.py        any harness whose sessions run in another program, a peer
                     spoken to with the wf-session protocol
    cursor.py        Cursor, through its SDK in the Cursor host (cursor_host/, in
                     TypeScript), a peer of remote.py
    cursor_acp.py    Cursor, through `cursor-agent acp` (the Agent Client Protocol
                     over stdio), with mcp_http.py serving it submit_result: for
                     `edit` sessions, and for every session with `cursor_runtime: acp`

KEPT_INSTRUCTIONS names what a harness still gives its agent when a node sets
`project_instructions: false`, which `wf validate` warns about.

reports_tokens() says whether a harness counts a session's tokens: Cursor does on its SDK,
but not through cursor-agent's ACP, which runs its `edit` sessions.

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
    from .cursor import create

    return create()


FACTORIES: dict[str, Callable[[], Harness]] = {"claude-code": _claude_code, "codex": _codex, "cursor": _cursor}


KEYCHAIN_SHELL = {"claude-code": ("edit", "full"), "cursor": ("edit", "full"), "codex": ("full",)}

KEPT_INSTRUCTIONS = {  # what a harness still loads with `project_instructions: false`
    "codex": "your global ~/.codex/AGENTS.md, or the skills in your home folder (~/.agents/skills, ~/.codex/skills)",
    "cursor": "your Cursor account's User Rules, the skills in your home folder (~/.claude/skills, ~/.agents/skills "
              "and others), or the repo's hooks in .cursor/hooks.json and .claude/settings.json",
}


def reaches_keychain(harness: str, access: str) -> bool:
    return access in KEYCHAIN_SHELL.get(harness, ())


def reports_tokens(harness: str, access: str) -> bool:
    """Does the harness report a session's tokens at this access level? A cursor_runtime setting
    that can't be read counts as the default, since a run would stop at it anyway."""
    if harness != "cursor":
        return True
    from .cursor import ON_ACP, runtime

    try:
        on_acp = runtime() == "acp"
    except Unavailable:
        on_acp = False
    return not on_acp and access not in ON_ACP


def create(name: str) -> Harness:
    if name not in FACTORIES:
        raise Unavailable(f"the `{name}` harness isn't supported yet")
    return FACTORIES[name]()


__all__ = ["FACTORIES", "KEPT_INSTRUCTIONS", "KEYCHAIN_SHELL", "reaches_keychain", "reports_tokens", "Harness", "Session", "SessionSpec", "TurnEnd", "Unavailable", "Usage", "create"]
