# Harness adapters: Codex, Cursor, and fresh vs continued sessions

Researched 2026-10-18 to answer the two open questions in `docs/workflow-spec.md`.

Versions checked:
- Codex CLI 0.163.0 (protocol schema generated from the installed CLI)
- `cursor-sdk` and `@cursor/sdk` 1.0.38
- `cursor-agent` 2026.09.26
- `claude-agent-sdk` 0.2.168, which bundles Claude Code 2.1.301

**[V]** = verified in source code, a generated schema, or a live run. **[I]** = inferred.

## Recommendation

1. **Run every harness from Python through its vendor's own interface. In all three,
   `submit_result` is a Python function the agent calls as a tool.** No local HTTP MCP server
   is needed.
   - **Claude Code:** `claude-agent-sdk`, with `submit_result` on an SDK MCP server. This is
     what `claude_code_chat/native_tools.py` already does.
   - **Codex:** speak JSON-RPC to `codex app-server` over stdio, and register `submit_result`
     as a *dynamic tool*. The server sends each call back to the runner as an `item/tool/call`
     request.
   - **Cursor:** the `cursor-sdk` Python package, with `submit_result` as a `custom_tools` entry.
     Superseded on 2026-10-19: wf drives `cursor-agent acp` on the local login instead, see
     [section 6](#6-cursor-through-acp).
2. **Keep `session: fresh` as the default.** `session: continue` stays opt-in. It forks the
   previous pass's session where the harness can, sends only the new feedback, and drops
   back to fresh when it can't continue cleanly.
3. **Shared runner code, thin adapters.** Retries, reminders, timeouts, validation and
   fallbacks are written once in the runner. Each adapter only opens a session and reports
   how each turn ended. See [Adapter design](#2-adapter-design).

The spec changes this implies are listed at the [end](#4-proposed-spec-changes).

## 1. The three harnesses side by side

| | Claude Code | Codex | Cursor |
|---|---|---|---|
| **From Python** | `claude-agent-sdk`: one CLI process per session | `codex app-server` over stdio: one process per run, many threads | `cursor-sdk`: one bundled Node "bridge" per run, many agents |
| **Maturity** | Stable | app-server is "[experimental]"; dynamic tools need `experimentalApi` [V] | Python SDK is "Public beta" [V] |
| **Tools run in the runner's process** | SDK MCP server [V] | `thread/start.dynamicTools` → `item/tool/call` [V live] | `LocalAgentOptions.custom_tools` [V] |
| **A tool error reaches the model** | `is_error: true` [V] | Only the reply's *text* reaches the model; the `success` flag doesn't [V live] | Raising an exception gives `isError: true` [V] |
| **Resume after a restart** | `resume=<id>`, same machine, 30-day retention [V] | `thread/resume` [V live] | `client.agents.resume(id, …)` [V] |
| **Fork** | `fork_session=True` [V] | `thread/fork` [V live] | None [V] |
| **Usage reported** | Tokens and `total_cost_usd` [V] | Tokens only [V] | Tokens; USD only where `get_usage()` is enabled for the account [V] |
| **Usage-limit signal** | `RateLimitEvent`, or an error result [V] | `error` with `codexErrorInfo: "usageLimitExceeded"`, `willRetry: false` [V] | Before a run: `RateLimitError`. Mid-run: only `status: error` plus message text [V] |
| **`read-only`** | Read, Grep and Glob tools, plus Bash limited to read-only git commands [I] | `sandbox: "read-only"`, which can still run read-only commands [V] | Read and search tools plus a runner-defined `git` tool; no shell [I] |
| **`edit`** | Read, Edit, Write and Bash, `permission_mode="dontAsk"` [V] | `sandbox: "workspace-write"` [V] | Default tools, `sandbox_options.enabled=True` [V] |
| **`full`** | `permission_mode="bypassPermissions"` [V] | `sandbox: "danger-full-access"` [V] | `sandbox_options.enabled=False` [V] |
| **Web off** | Leave out WebSearch and WebFetch | `config.web_search: "disabled"`. **The default is `"cached"`**, so always set it [V] | `disallowed_tools`: `webSearch`, `webFetch` [V] |
| **Agent can run `git commit` in a worktree** | Yes (no sandbox) | No: `.git`, including a worktree's real git folder, stays read-only even in `workspace-write` [V] | Probably not when sandboxed [I] |
| **Model and effort** | `model`, `effort` | `model`; effort per turn, or `config.model_reasoning_effort` [V] | `model` is required; effort goes in model params [V] |
| **Working folder** | `cwd` | `cwd` per thread [V live] | `local.cwd` per agent [V] |
| **Auth** | Your Claude login (`subscription_env()`) | The CLI's ChatGPT login [V live] | **A Cursor dashboard API key.** The CLI's browser login isn't reused [V] |
| **Repo instruction files** | `setting_sources=["project"]` loads `CLAUDE.md` | `AGENTS.md` always; `CLAUDE.md` only via `project_doc_fallback_filenames` [V] | `setting_sources=["project"]` loads `AGENTS.md`, `.cursor/rules`, `CLAUDE.md` [I] |
| **User-level config** | Left out with `setting_sources` | Always loaded: pin settings per thread and use `--disable hooks --disable apps` [V] | Left out by default, except `~/.cursor/sandbox.json` unless the sandbox is set explicitly [V] |

## 2. Adapter design

### The pattern

The runner owns one `ResultTool` per attempt: the node's schema, a JSON Schema validator,
and the recorded result. Every adapter wraps the same `submit` method in its SDK's tool type,
so validation and "first valid call wins" are written once.

```python
class ResultTool:
    """submit_result for one attempt. Each adapter wraps submit() in its SDK's tool type."""
    def __init__(self, schema: dict): ...
    result: dict | None = None
    def submit(self, args: dict) -> tuple[str, bool]:
        """Returns (text for the model, is_error). The text carries the errors,
        because Codex shows the model only the text."""

@dataclass
class SessionSpec:
    cwd: Path
    access: Literal["read-only", "edit", "full"]
    web: bool
    model: str | None
    effort: str | None
    tool: ResultTool
    fork_from: str | None = None   # previous pass's session id, for session: continue

@dataclass
class TurnEnd:
    status: Literal["done", "unavailable", "failed"]   # unavailable: usage limit, auth, can't start
    message: str
    usage: Usage                                         # tokens, plus cost_usd when reported

class Session(Protocol):
    id: str
    async def send(self, text: str) -> TurnEnd   # one turn; progress goes to the event callback
    async def close(self) -> None                # interrupts a running turn

class Harness(Protocol):
    name: str
    async def open(self, run_dir: Path) -> None  # start app-server / bridge once per run
    async def session(self, spec: SessionSpec) -> Session
    async def close(self) -> None
```

The attempt loop is written once, in the runner, for all harnesses:

```python
async def run_attempt(harness: Harness, spec: SessionSpec, instruction: str) -> tuple[dict, str]:
    session = await harness.session(spec)
    try:
        for text in [instruction, NUDGE, NUDGE]:
            end = await session.send(text)          # wrapped in asyncio.wait_for(timeout)
            if end.status == "unavailable":
                raise HarnessUnavailable(end.message)   # runner moves to the next harness
            if end.status == "failed":
                raise AttemptFailed(end.message)        # counts against retry
            if spec.tool.result is not None:
                return spec.tool.result, session.id
        raise AttemptFailed("no valid submit_result call")
    finally:
        await session.close()
```

Why this shape:
- The spec's "remind the agent up to 2 times" rule becomes "send another message in the same
  session". All three harnesses can do that on a live session: a new `query` (Claude), a new
  `turn/start` on the thread (Codex), and `agent.send` (Cursor). So **no harness needs resume
  just for reminders.**
- Per-attempt isolation comes from the session itself. Claude has one process per session,
  Codex routes calls by `threadId`, and Cursor keys tools by `agent_id`. This replaces the
  spec's per-attempt URL token.
- Each adapter is roughly 100 lines. It maps `SessionSpec` to SDK options, wraps `submit`,
  and turns SDK events into progress events and a `TurnEnd`.

### Claude Code adapter

- **Session.** `ClaudeSDKClient` with:
  - `cwd`
  - `setting_sources=["project"]`
  - `system_prompt={"type": "preset", "preset": "claude_code"}`, so agents get Claude Code's
    own coding prompt
  - tools and `permission_mode` from the access table above
  - the SDK MCP server holding `submit_result`
- **Usage limits.** A `RateLimitEvent` whose status isn't `allowed` (as `deep_research`
  already handles) maps to `unavailable`.
- **Cost on resumed sessions.** `total_cost_usd` on a resumed or forked call includes the
  session's earlier spend. Subtract it, or `run.cost_usd` counts it twice [V]
  ([cost tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking)).

### Codex adapter

- **Process.** Start `codex app-server --disable hooks --disable apps` once per run, then
  send `initialize` with `capabilities.experimentalApi: true` and the `initialized`
  notification [V live]. Use the protocol directly, not the `openai-codex` Python SDK, for
  three reasons:
  - Its "async" API just runs blocking calls in threads.
  - It auto-accepts approval requests by default.
  - It doesn't expose `dynamicTools`.

  Its source is still a useful reference [V]
  ([client.py](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/sdk/python/src/openai_codex/client.py)).
- **Starting a thread.** Set every value explicitly, because user config fills any gap. In
  the live test, the thread silently took the user's default model and `xhigh` effort [V live].

  ```json
  {"method": "thread/start", "id": 2, "params": {
    "cwd": "/abs/worktree", "model": "<from model/list>", "sandbox": "workspace-write",
    "approvalPolicy": "never", "ephemeral": true,
    "config": {"model_reasoning_effort": "high", "web_search": "disabled",
               "sandbox_workspace_write.network_access": false},
    "dynamicTools": [{"type": "function", "name": "submit_result",
                      "description": "…", "inputSchema": {"…": "node output schema"}}]}}
  ```

  Notes on these settings:
  - Use `ephemeral: false` only when the node has `session: continue`, so the thread can be
    forked later.
  - The sandbox values are kebab-case at thread level. The docs' `"workspaceWrite"` example
    disagrees with the 0.163.0 schema; trust the generated schema [V].
- **Tool calls.** Answer every `item/tool/call` with
  `{"success": …, "contentItems": [{"type": "inputText", "text": …}]}`. There is no
  server-side timeout, so a missing reply hangs the turn until `turn/interrupt` [V].
- **Approval requests.** Decline any that arrive. With `approvalPolicy: "never"`, none should.
- **Turn end.** A turn ends with `turn/completed` (`completed`, `failed` or `interrupted`).
  An `error` notification with `codexErrorInfo: "usageLimitExceeded"` and `willRetry: false`
  maps to `unavailable`. A plain `rateLimitExceeded` is retried by Codex itself, so ignore it [V].
- **Usage.** Read token counts from `thread/tokenUsage/updated`. USD cost is not available
  (an experimental `account/usage/read` returns a nullable estimate) [V].
- **What can't be switched off.** User-level config can't be fully ignored: the global
  `~/.codex/AGENTS.md` still loads. The only way around it is a separate `CODEX_HOME`, which
  means a separate login. I'd accept this and document it.
- **Version pinning.** Pin the Codex CLI version, because this API is experimental. `wf`
  can warn when `codex --version` differs from the tested version.
- **Terms.** App-server login "has never been permitted for commercial or hosted services".
  That's fine for a local CLI on your own account, but `wf` must not become a hosted service
  on someone's ChatGPT login [V]
  ([app-server docs](https://learn.chatgpt.com/docs/app-server)).

### Cursor adapter

Superseded on 2026-10-19 by [section 6](#6-cursor-through-acp): this design needs a dashboard
API key, and `cursor-agent acp` doesn't.

- **Setup.** `pip install cursor-sdk`. The wheel is about 50 MB and bundles Node. It needs an
  API key from the Cursor dashboard [V]. Read `CURSOR_API_KEY` once, pass it as `api_key=`,
  and remove it from the environment before starting the bridge. Agent shell commands inherit
  the bridge's environment and could read the key otherwise [I].
- **Process.** Once per run:
  `AsyncClient.launch_bridge(workspace=repo, state_root=<run folder>/cursor)`. Keeping the
  state inside the run folder lets `wf resume` find agents.
- **Session.**

  ```python
  client.agents.create(AgentOptions(
      model=..., api_key=key, tools=..., disallowed_tools=...,
      local=LocalAgentOptions(cwd=..., setting_sources=["project"],
                              sandbox_options=SandboxOptions(enabled=...),
                              custom_tools={"submit_result": CustomTool(...)})))
  ```

  - Always set `sandbox_options` explicitly, so `~/.cursor/sandbox.json` doesn't apply.
  - Keep `"mcp"` in any `tools` list, or the custom tools disappear [V].
  - For `read-only`, disallow `"task"` (subagents) [V].
- **`read-only` has no shell.** The SDK has no per-command shell allowlist. Instead, the
  runner adds a second custom tool, `git`, that runs only `diff`, `log`, `show` and `blame`,
  with a fixed argument list and no shell [I].
- **Turn end.** Mid-run usage limits only appear as `status: "error"` plus message text, so
  the adapter has to match the text (`usage limit|rate limit|quota`). This is the weakest
  part of the design [V].
- **Untested.** Nothing in the SDK path has been run, because there's no API key on this
  machine. The CLI fallback (`cursor-agent -p --output-format stream-json`) was tested live:
  tool errors reached the model and `--resume` worked. But the CLI can only load MCP servers
  from `.cursor/mcp.json` files and keeps approval state for every new worktree path, so it's
  a poor fit [V live].

## 3. Fresh vs continued sessions

**What the sources say:**
- **Anthropic's evaluator-optimizer reference code calls the generator fresh every round**,
  pasting earlier attempts and the latest feedback into the prompt
  ([evaluator_optimizer.ipynb](https://github.com/anthropics/claude-cookbooks/blob/a162e0184e71b60290808a43961a6cbc8060123c/patterns/agents/evaluator_optimizer.ipynb),
  [util.py#L22](https://github.com/anthropics/claude-cookbooks/blob/a162e0184e71b60290808a43961a6cbc8060123c/patterns/agents/util.py#L22)).
- **Claude Code best practices:** "After two failed corrections, `/clear` and write a better
  initial prompt incorporating what you learned", and "A fresh context improves code review
  since Claude won't be biased toward code it just wrote"
  ([best practices](https://code.claude.com/docs/en/best-practices)).
- **Agent SDK docs:** "Don't rely on session resume. Capture the results you need … and pass
  them into a fresh session's prompt. This is often more robust"
  ([sessions](https://code.claude.com/docs/en/agent-sdk/sessions)).
- **Anthropic's prompting guide:** "consider starting with a brand new context window rather
  than using compaction. Claude's latest models are extremely effective at discovering state
  from the local filesystem" … "Use git for state tracking"
  ([prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)).
- **The case for continuing:**
  - Anthropic's March 2026 harness post dropped context resets for Opus 4.5 and later
    ([harness design](https://www.anthropic.com/engineering/harness-design-long-running-apps)).
  - Cursor says to continue when "You're debugging something it just built", and to start
    over when the agent "keeps making the same mistakes"
    ([Cursor best practices](https://cursor.com/blog/agent-best-practices)).
- **OpenAI's Symphony orchestrator** continues a thread *within* one worker run. Each new run
  starts a fresh `thread/start` in the reused workspace (`openai/symphony` `SPEC.md`
  L667–671, `agent_runner.ex` L92).
- **No first-party benchmark compares the two modes on a review loop.**

**Why fresh is the right default for `wf`:**
- **Code state already carries over.** The worktree persists across passes.
- **Every instruction must work fresh anyway.** Harness fallback means the next pass can run
  on a different harness.
- **Everything else stays simple** with fresh sessions: retries, `wf resume`, cost totals,
  and reproducibility (each pass's input is just its `instruction.md` plus a commit).
- **Continuing isn't clearly cheaper.** Tests plus a review often take longer than the
  5-minute cache lifetime, and after that the whole history is re-sent at 1.25× the input
  price ([prompt caching](https://code.claude.com/docs/en/prompt-caching)).

**How `continue` should work when a node opts in:**
- **Fork, don't resume.** The runner records each pass's session id. The next pass forks it:
  `fork_session` on Claude, `thread/fork` on Codex. That way a retry always starts from the
  same clean point. Cursor can't fork, so it resumes the previous agent, and a retry after a
  failed continued attempt starts fresh.
- **Fall back to fresh**, and log why, when the harness or model changed, the session is gone,
  or two passes in a row have already continued.
- **Send only the feedback.** A `session.continued` template variable lets the instruction do
  that instead of repeating the whole task, as Symphony does. The message also says a *new*
  `submit_result` call is needed, because the old "Result recorded" reply is still in the
  history.

**Make fresh passes strong instead.** Give the implementer a `notes` output field (what
changed, what was tried and dropped). Render `history.implement` notes and earlier reviews
into its instruction, and tell it to start with `git log` and `git diff` against the base.

## 4. Proposed spec changes

All seven were applied to `docs/workflow-spec.md` on 2026-10-18.

1. **The output tool.** Replace "Codex and Cursor: the runner hosts a local HTTP MCP server"
   with in-process tools for all three harnesses (Claude SDK MCP server, Codex `dynamicTools`,
   Cursor `custom_tools`), and drop the per-attempt URL token. Reminders become extra
   messages in the same session, not resumes.
2. **Access levels.** Add the per-harness mapping table to the implementation notes. State
   that agents can't write to `.git` under Codex's sandbox (and probably Cursor's), so the
   runner always does the commits. Nodes that run git write commands need `access: full`.
   That includes `merge` in `feature.yaml`, which must change.
3. **Usage limits.** Codex reports no USD cost and Cursor usually doesn't either. Add
   `run.tokens` and `limits.tokens`, which work for all three, and say that `run.cost_usd`
   only counts harnesses that report cost. Also note that a resumed Claude session's cost
   includes earlier spend, which the runner must subtract.
4. **Sessions.** Close the open question: `fresh` stays the default. Define `continue` as
   above: fork, fall back, at most 2 continued passes in a row, and a `session.continued`
   template variable (so `session` joins the reserved names).
5. **Examples.**
   - Add a `notes` field to `implement`'s output, and render `history.implement` into its
     instruction with a "start from `git log`/`git diff`" line.
   - Set `merge` to `access: full`.
6. **Harness settings.**
   - Cursor requires a `model` (for example `composer-2.5`).
   - Codex model names come from its `model/list`.
   - Add a short setup section: `codex` logged in, a Cursor API key in `CURSOR_API_KEY`, and
     `cursor-sdk` as an optional dependency.
7. **Open question 1** closes with this design.

## 5. To check before or while building

- **Cursor** (needs an API key; for the ACP adapter, see [section 6](#6-cursor-through-acp)):
  - Does `setting_sources=["project"]` load `AGENTS.md`, `.cursor/rules` and `CLAUDE.md`?
  - Can a sandboxed `edit` agent run git in a worktree?
  - How does a usage limit actually show up?
  - Is `get_usage()` cost enabled for this account?
  - What are the parameter ids for effort?
  - How many agents can one bridge run at once?
- **Codex:**
  - Does `web_search: "disabled"` really remove the tool? (The setting was accepted live.)
  - How many threads can one app-server run at once?
  - Run `generate-json-schema --experimental` in CI to catch protocol drift.
- **Claude:** does an explicit `model` on a resumed session override the recorded one?

## 6. Cursor through ACP

Checked 2026-10-19 with `cursor-agent` 2026.10.14-0f9edde, for `workflow/harnesses/cursor.py`.

`cursor-agent acp` speaks the Agent Client Protocol, JSON-RPC 2.0 over stdio, on the local
Cursor login. It needs no API key and no `cursor-sdk` [V live].

- **Login.** `initialize` offers one auth method, `cursor_login`, and says the agent takes MCP
  servers over `http` and `sse` and can load sessions. `authenticate` with `cursor_login`
  returns at once when the CLI is logged in. When it isn't, it starts a browser login, so the
  adapter runs `cursor-agent --list-models` first and sets `NO_OPEN_BROWSER` [V].
- **One model per process.** Choosing a model in one session switches every session in the
  process, and `session/set_config_option` only takes the variants it lists. `--model <id>` at
  start takes any id from `--list-models`, effort included: `gemini-4-argon-high` gave
  `reasoning_effort: high` [V live]. So the adapter starts one process per session.
- **The model is saved.** `--model` rewrote `selectedModel` in `~/.cursor/cli-config.json`
  during this check. `CURSOR_CONFIG_DIR` moves that file, the saved sessions, MCP servers and
  hooks to another folder. The login is kept outside it and still works [V live].
- **`submit_result` over MCP.** An HTTP MCP server named in `session/new` loads without
  approval. Each call comes first as `session/request_permission` (kind `other`), then as
  `tools/call` to the server [V live].
- **Permissions.** With `approvalMode: allowlist` and an empty allowlist, every shell command
  asks (kind `execute`), and so do MCP tools (`other`) and web search and fetch (`search`,
  `fetch`), except a fetch that Cursor's server marks as needing no approval. Writes inside
  the workspace don't ask, and a `Write(**)` deny rule makes them fail. Ask mode still offers
  the shell, which asks [V live].
- **No usage.** `session/prompt` returns only a `stopReason`, and no update carries tokens or
  cost [V].
- **Errors are text.** A failed request ends the turn with `end_turn` and a last message
  `Error: ...`, or a notice such as `Upgrade your plan to continue` for a plan, payment or
  sign-in problem [V].
- **Continue.** Sessions are saved in the config folder. `session/load` replays the
  conversation as `session/update` notifications, then answers. There's no fork [V].
- Starting a process and a session takes 4 to 6 seconds [V live].
