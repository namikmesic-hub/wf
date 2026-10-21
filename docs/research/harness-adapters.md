# Harness adapters: Codex, Cursor, and fresh vs continued sessions

Researched 2026-10-18 to answer the two open questions in `docs/workflow-spec.md`.

Versions checked:
- Codex CLI 0.163.0 (protocol schema generated from the installed CLI)
- `cursor-sdk` and `@cursor/sdk` 1.0.38
- `cursor-agent` 2026.09.26
- `claude-agent-sdk` 0.2.168, which bundles Claude Code 2.1.301
- On 2026-10-19, for `wf steer`: the same SDK and Codex, see [section 7](#7-steering-a-live-session)

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
     Superseded on 2026-10-19: wf drives `cursor-agent acp` on the local login instead, see [section 6](#6-cursor-through-acp).
     Superseded again on 2026-10-20: wf drives the TypeScript `@cursor/sdk` in the Cursor host, with `edit` still on ACP, see [section 8](#8-cursor-through-cursorsdk).
2. **Keep `session: fresh` as the default.** `session: continue` stays opt-in. It forks the
   previous pass's session where the harness can, sends only the new feedback, and drops
   back to fresh when it can't continue cleanly.
3. **Shared runner code, thin adapters.** Retries, reminders, timeouts, validation and
   fallbacks are written once in the runner. Each adapter only opens a session and reports
   how each turn ended. See [Adapter design](#2-adapter-design).

The spec changes this implies are listed at the [end](#4-proposed-spec-changes).

## 1. The three harnesses side by side

The Cursor column describes the `cursor-sdk` design.
[Section 6](#6-cursor-through-acp) replaced it on 2026-10-19 with `cursor-agent acp`, which runs on the local login.

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
  In 0.163.0, `TokenUsageBreakdown` copies Codex's `TokenUsage` field by field [V source: [thread.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L1992-L2022)].
  `TokenUsage` takes `cachedInputTokens` and `cacheWriteInputTokens` from the Responses API's `input_tokens_details` [V source: [responses.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/codex-api/src/sse/responses.rs#L119-L152)].
  So both are parts of `inputTokens`, and `totalTokens` is input plus output.
  Codex's own test `parses_cache_write_token_usage` reads 100 input tokens as 40 cached and 60 written, with a total of 110.
  When a turn overflows the context window, Codex sets the total to the window's size, with each part 0 [V source: [protocol.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/protocol/src/protocol.rs#L2438-L2451)].
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

Superseded on 2026-10-19 by [section 6](#6-cursor-through-acp): this design needs a dashboard API key, and `cursor-agent acp` doesn't.

- **Setup.** `pip install cursor-sdk`. The wheel is about 50 MB and bundles Node. It needs an
  API key from the Cursor dashboard [V]. Read `CURSOR_API_KEY` once, pass it as `api_key=`,
  and remove it from the environment before starting the bridge. Agent shell commands inherit
  the bridge's environment and could read the key otherwise [I].
- **Process.** Once per run:
  `AsyncClient.launch_bridge(workspace=repo, state_root=<run folder>/cursor)`. Keeping the
  state inside the run folder lets `wf retry` find agents.
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
- **Everything else stays simple** with fresh sessions: retries, `wf retry`, cost totals,
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

- **Cursor**, which needs an API key ([section 6](#6-cursor-through-acp) covers the ACP adapter):
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

Checked 2026-10-19 with `cursor-agent` 2026.10.14-0f9edde and 2026.10.16-e3cb207, for `workflow/harnesses/cursor.py`.
That file is now `workflow/harnesses/cursor_acp.py`, and its tests are in `tests/test_cursor_acp.py`: the line numbers below are of the file as it was then.
On 2026-10-20 every test marked `-m harness` in `tests/test_cursor.py` passed with 2026.10.18-ea085cb, which is now `TESTED`.
These tests include the check that `project_instructions: false` keeps the repo's rules and `AGENTS.md` out.
`cursor-agent acp` speaks the Agent Client Protocol, JSON-RPC 2.0 over stdio, on the local Cursor login, so it needs no API key and no `cursor-sdk`.
Each row says what Cursor does, what the adapter does about it, where, and which test fails when the two disagree.
Tests marked `-m harness` run the real `cursor-agent`.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| `authenticate` with `cursor_login` returns at once when the CLI is logged in [V live]. | Every session authenticates that way, with `CURSOR_API_KEY` and `CURSOR_AUTH_TOKEN` removed from cursor-agent's environment. | `cursor.py:306`, `cursor.py:216` | `test_a_session_runs_on_the_local_login_with_wfs_own_cursor_config` |
| When the CLI isn't logged in, `authenticate` starts a browser login and `--list-models` fails [V]. | The adapter runs `--list-models` before any session, sets `NO_OPEN_BROWSER`, and reports Cursor unavailable. | `cursor.py:182`, `cursor.py:217`, `cursor.py:224` | `test_a_cursor_agent_that_is_not_logged_in_is_unavailable` |
| The agent keeps one model per process. `--model <id>` takes the effort in the id only when `initialize` sends `clientCapabilities._meta.parameterizedModelPicker: true`: then `gpt-5.6-sol-max-fast` runs with `reasoning: max, fast: true` [V live]. | One process per session, started with `--model`. `initialize` declares parameterized models, and the transcript records the model and parameters the session reports. | `cursor.py:292`, `cursor.py:305`, `cursor.py:323` | `test_a_real_cursor_session_takes_the_effort_in_the_model_id` (`-m harness`), `test_the_transcript_records_the_model_and_effort_cursor_uses` |
| Cursor saves the model, and the parameters it last used for it, into its config folder (`CURSOR_CONFIG_DIR`), and writes the account's details there [V live]. | Each process gets a new folder in `~/.wf/temp/<run-id>/cursor-agent/`, outside the repo, and the folder goes when its session closes. | `cursor.py:210`, `cursor.py:448` | `test_each_cursor_agent_gets_a_new_config_folder_and_none_is_left` |
| cursor-agent merges the permissions of `.cursor/cli.json` files from the git root down to its folder. An invalid one stops it at start, and the hidden flag `--disable-project-configs` stops the merge [V live]. | cursor-agent always starts with `--disable-project-configs`. | `cursor.py:136` | `test_a_real_cursor_session_ignores_the_repos_cursor_settings` (`-m harness`) |
| An HTTP MCP server named in `session/new` loads without approval. Each call asks `session/request_permission` (kind `other`), then comes as `tools/call` [V live]. | `submit_result`, and `git` for read-only, are served on a URL of the session's own, which ends when the session closes. | `cursor.py:286` | `test_a_real_read_only_agent_node_on_cursor` (`-m harness`), `test_closing_a_session_ends_its_mcp_url` |
| With `approvalMode: allowlist` and an empty allowlist, every command, MCP tool, web search and web fetch asks, and writes inside the workspace don't [V live]. | Each request is answered from the node's access level, with "allow once" or "reject". | `cursor.py:377` | `test_permission_requests_are_answered_from_the_access_level` |
| Outside the workspace, Cursor writes to temp folders and to Markdown files in `~/.cursor` without asking. A deny rule, where `*` matches any text, blocks such a write: `Write(/private/tmp/*)` blocked one in `/private/tmp` [V live]. | `read-only` denies `Write(**)`, and `edit` denies the temp folders and `~/.cursor`. | `cursor.py:146`, `cursor.py:150` | `test_edit_and_full_run_in_agent_mode_with_writes_allowed`, `test_edit_denies_the_writes_cursor_makes_outside_the_workspace_without_asking` |
| With the sandbox on, a command still asks, and one the client allows runs outside the sandbox: it wrote a file outside the workspace and reached the network [V live]. | `--sandbox disabled` at every level, since the sandbox confines nothing the adapter doesn't already decide. | `cursor.py:136` | `test_a_session_runs_on_the_local_login_with_wfs_own_cursor_config` |
| `session/prompt` returns only a `stopReason`, and no update carries tokens or cost [V]. | Cursor turns count zero tokens and zero dollars. | `cursor.py:327` | `test_cursor_reports_no_usage` |
| A failed request ends the turn with `end_turn` and a last paragraph `Error: [code] ...`, `Error: SomeError: ...` or an account notice such as `Upgrade your plan to continue`, sent in one or more chunks [V]. A turn ended with `Error: RetriableError: [resource_exhausted] Error` while the account had usage left [V live, 2026-10-19]. By its name, a `RetriableError` is an error that a retry can clear [I]. | The message since the last tool call is read. A `RetriableError` fails the turn, and the node's retry starts 30 seconds later. Else a notice, `[unauthenticated]` or `[resource_exhausted]` makes Cursor unavailable, another error fails the turn, and other text is the agent's. | `cursor.py:355`, `cursor.py:479` | `test_a_retriable_error_is_not_a_usage_limit_and_the_retry_runs_after_a_wait`, `test_a_resource_exhausted_error_that_is_not_retriable_hands_over_at_once`, `test_a_usage_limit_or_a_login_problem_makes_cursor_unavailable`, `test_a_notice_sent_in_several_chunks_is_still_read`, `test_a_paragraph_that_only_starts_with_error_is_the_agents_own_text` |
| Sessions are saved in the config folder. `session/load` replays the conversation as `session/update` notifications, then answers. There's no fork [V]. | The saved session moves into the new process's folder and loads there. Replayed updates are ignored, and after a continued attempt fails the retry starts fresh. | `cursor.py:297`, `cursor.py:308`, `cursor.py:268`, `cursor.py:279` | `test_continue_loads_the_last_passs_session`, `test_after_a_continued_attempt_fails_the_retry_starts_fresh`, `test_a_continued_attempt_that_times_out_after_its_result_is_not_continued_again` |
| `acp` and `--disable-project-configs` aren't in `cursor-agent --help`, and the messages above can change between versions [V]. | wf is tested with one version and warns once when another is installed. | `cursor.py:134`, `cursor.py:184` | `test_a_cursor_agent_other_than_the_tested_one_gets_one_warning` |

## 7. Steering a live session

Checked 2026-10-19 for `wf steer` (`workflow/steer.py`), which sends a message into the live session of an agent node.
Claude Code was checked live through `claude-agent-sdk` 0.2.168, with its Claude Code 2.1.301 and Haiku 5.5, in three sessions.
Codex was checked against the schema that `codex app-server generate-json-schema` writes for Codex 0.163.0.
Each row says what the harness does, what wf does about it, where, and which test fails when the two disagree.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| Claude Code takes a user message that comes while the agent works into the running turn. It adds the message to the next step, beside that step's tool results, as "The user sent a new message while you were working". The turn then ends with one result [V live]. | The message goes to Claude Code at once, and counts as `queued` until Claude Code reads it. | `workflow/harnesses/claude_code.py:261` | `test_claude_code_reads_a_message_from_wf_steer_at_its_next_step` |
| With `--replay-user-messages`, Claude Code repeats each user message, with the `uuid` that the client gave it, when it reads the message [V live]. | Each message goes out with an id, and the run records `delivered` when its id comes back. | `workflow/harnesses/claude_code.py:191`, `workflow/harnesses/claude_code.py:319` | `test_every_session_sets_everything_explicitly`, `test_claude_code_reads_a_message_from_wf_steer_at_its_next_step` |
| A message that comes after the agent's last step, while the model writes its last reply, starts a turn of its own after the result, with a result of its own [V live]. | The adapter reads on to that turn's result, unless the agent already submitted its result, or the turn failed. | `workflow/harnesses/claude_code.py:361`, `workflow/harnesses/claude_code.py:365` | `test_a_message_after_the_last_step_gets_a_turn_of_its_own_and_no_reminder_goes`, `test_a_message_claude_code_hasnt_read_when_the_agent_submits_is_dropped`, `test_a_result_that_ends_the_attempt_doesnt_wait_for_a_write_that_stalls` |
| In one of the two sessions with a message mid-turn, the agent took the message for part of a tool's output, and ignored it [V live]. | Nothing in wf. The spec and the firstmate skill say so. | | |
| The SDK answers a tool call as it comes, before the client reads the messages up to that call [V]. | Whether the agent submitted comes from the messages that the adapter has read, not from `submit_result` itself. | `workflow/harnesses/claude_code.py:339` | `test_a_message_after_the_last_step_gets_a_turn_of_its_own_and_no_reminder_goes` |
| `turn/steer` takes `threadId`, `input` and `expectedTurnId`, and fails when that turn isn't the one in progress [V schema]. | A message goes into the running turn, named by its id. When the request fails, the runner sends the message as the next turn. When no answer comes, the message isn't sent again, and the run records it `unconfirmed`. | `workflow/harnesses/codex.py:242` | `test_a_message_from_wf_steer_joins_the_running_turn`, `test_a_message_that_misses_the_turn_goes_as_the_next_turn_before_a_reminder`, `test_a_message_codex_never_answers_for_is_unconfirmed_and_never_sent_again` |
| The Agent Client Protocol has no request that adds to a running `session/prompt`. A client can only cancel the turn [I]. | The runner sends the message as the next `session/prompt`, when the turn ends. | `workflow/harnesses/cursor_acp.py:337` | `test_a_message_from_wf_steer_goes_to_cursor_as_the_next_turn` in `tests/test_cursor_acp.py` |
| A local run of `@cursor/sdk` 1.0.38 has `steer(text)`, which answers `complete_delivered` or `revert_to_followup`, and `revert_to_followup` when the run's controller has none [V source: `dist/esm/index.js`]. | The Cursor host offers the message to the run. `complete_delivered` is `delivered`, and anything else goes as the next turn. | `workflow/harnesses/cursor_host/src/cursor.ts:374` | `test_a_message_from_wf_steer_joins_the_running_turn`, `test_a_message_from_wf_steer_goes_to_cursor_as_the_next_turn` in `tests/test_cursor.py` |

## 8. Cursor through `@cursor/sdk`

Checked 2026-10-20 for the Cursor host (`workflow/harnesses/cursor_host/`) and `workflow/harnesses/cursor.py`, against `@cursor/sdk` 1.0.38.
The sources are the SDK's type files (`dist/esm/*.d.ts`) and its bundle (`dist/esm/index.js`, `dist/esm/748.js`), from `npm pack @cursor/sdk@1.0.38`.
Live checks without an API key ran against Cursor's API.
The checks that need a key are tests marked `-m harness`, and they skip without one.
Each row says what the SDK does, what the host or wf does about it, where, and which test fails when the two disagree.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| The SDK takes an API key only: the `apiKey` option, else `CURSOR_API_KEY`, else `~/.cursor/sdk/auth.json` [V bundle]. A key it doesn't know is an `AuthenticationError` (401, "Invalid User API Key") [V live]. An empty `apiKey` is refused without the fallbacks [V live]. | wf reads the key from the Keychain or from the file that `cursor.api_key_file` in `config.toml` names, and gives it to the host in `hello`. The host always passes `apiKey`, and its environment has no `CURSOR_API_KEY`. With no key, read-only and full sessions run through `cursor-agent acp`, and the run warns once with the fix. A refused key is an `auth` error with the fix. | `workflow/harnesses/cursor.py:120`, `workflow/harnesses/cursor.py:141`, `workflow/harnesses/cursor.py:332`, `workflow/harnesses/cursor_host/src/main.ts:65` | `test_with_no_api_key_sessions_run_through_cursor_agents_acp_and_one_warning_says_how_to_add_one`, `test_a_refused_api_key_makes_cursor_unavailable`, `test_a_real_cursor_api_refuses_a_key_it_does_not_know_as_a_sign_in_problem` (`-m harness`, no key needed) |
| `local.enableAgentRetries` has two defaults in 1.0.38: `enableAgentRetries??!1` (off) on one path and `!1!==r.enableAgentRetries` (on) on another [V bundle]. | The host always sets it to `true`. | `workflow/harnesses/cursor_host/src/cursor.ts:168` | `test_a_read_only_session_runs_on_the_sdk_with_wfs_settings`, `test_a_45_second_network_blackhole_mid_turn_ends_in_one_attempt` (`-m harness`) |
| The SDK checks a model against the catalog of `GET /v1/models`, by `id` or `aliases`, and keeps the parameters it is given [V bundle]. cursor-agent's ids, such as `gpt-5.6-sol-max-fast`, carry the parameters in the id [V live: `cursor-agent --list-models`]. The catalog's real entries need a key [I]. | The host maps an id to a catalog model and the one variant that the rest of the id names, takes `id[name=value]` as it is, and refuses anything else as `unavailable`. | `workflow/harnesses/cursor_host/src/models.ts:26` | `cursor_host/test/models.test.ts`, `test_a_model_cursor_cannot_run_is_unavailable_and_the_next_harness_runs`, `test_doctor_checks_node_the_host_the_key_and_how_model_ids_map` |
| `tools` restricts the built-in tools, `disallowedTools` removes some, and `"mcp"` carries every MCP tool, the custom tools included [V types]. Unknown names are a `ConfigurationError` at `Agent.create` [V types]. | Read-only gets `["mcp"]` and no `task`. Every level loses `askQuestion`, and the web tools when `web` is false. | `workflow/harnesses/cursor_host/src/cursor.ts:108` | `cursor_host/test/host.test.ts`, `test_each_access_level_and_web_setting_maps_to_the_sdks_tool_lists` |
| The SDK's grep, glob, ls and read tools take any path. Grep checks an outside path only against `.cursorignore` [V bundle: `LocalGrepExecutor`, `checkOutsideTarget`]. A read boundary exists only in the per-user `~/.cursor/sandbox.json` and in team settings [V bundle: `readControlSetting`, `getFileReadBoundaryBlocker`]. There is no option to answer a call before it runs [V types]. | A read-only session reads files only through wf's file tools, which stay in the workspace and the files and folders the instruction names. A call of a tool the session wasn't given cancels the turn. | `workflow/harnesses/files_tool.py:121`, `workflow/harnesses/cursor_host/src/cursor.ts:354`, `workflow/nodes.py:695` | `test_a_read_only_agent_reads_and_searches_only_inside_its_workspace`, `test_a_tool_that_a_read_only_session_was_not_given_stops_the_turn`, `test_a_folder_of_wfs_that_the_instruction_names_is_readable_too`, `test_a_file_or_folder_that_the_instruction_names_is_readable_too`, `test_a_quoted_file_or_folder_whose_path_has_spaces_is_readable_too`, `tests/test_files_tool.py` |
| `local.sandboxOptions.enabled` turns on a `workspace_readwrite` policy, run by the bundled `cursorsandbox` [V bundle]. It is unknown whether it keeps writes of commands and of the edit tool in the workspace on macOS [I]. It is also unknown whether it lets commands reach the network, and keeps them from the Keychain [I]. | `edit` runs through ACP. Read-only turns the sandbox on, and `full` off. `edit` moves to the SDK only when the first live check passes, and the second one decides whether Cursor at `edit` still reaches the Keychain. | `workflow/harnesses/cursor.py:85`, `workflow/harnesses/cursor_host/src/cursor.ts:113` | `test_edit_runs_through_cursor_agents_acp`, `test_cursors_sandbox_keeps_edit_writes_in_the_workspace_and_lets_commands_reach_the_network` and `test_cursors_sandbox_keeps_edit_commands_from_the_keychain` (`-m harness`) |
| `local.customTools` are callbacks in the host process, offered as the `custom-user-tools` MCP server, and need no approval [V types]. | Each of the session's tools (submit_result, git, the file tools, the wf tools) is a custom tool that sends a `call` frame and waits for wf's `ret`. A wf tool's answer reaches the events only as its size. | `workflow/harnesses/cursor_host/src/cursor.ts:197`, `workflow/harnesses/remote.py:568` | `test_cursors_sdk_gets_a_custom_tool_per_operation_and_its_calls_are_relayed`, `test_cursors_sdk_reports_text_and_tool_calls_for_the_trace_but_not_what_a_wf_tool_returned` |
| `mcpServers` on the agent and on each send replaces the servers Cursor would load [I: t3code's adapter says so]. | A read-only session, and any with `tools`, gets `mcpServers: {}` on create and on each send, and a call of another server's tool cancels the turn. | `workflow/harnesses/cursor_host/src/cursor.ts:162`, `workflow/harnesses/cursor_host/src/cursor.ts:250` | `test_a_tool_that_a_read_only_session_was_not_given_stops_the_turn` |
| `local.settingSources` left out loads no settings layer. `"project"` loads the repo's [V types]. | `project_instructions: false` leaves it out, and `true` gives `["project"]`. | `workflow/harnesses/cursor_host/src/cursor.ts:165` | `test_without_project_instructions_no_cursor_settings_layer_loads`, `test_a_real_cursor_node_without_project_instructions_gets_no_repo_rules` (`-m harness`) |
| A turn reports tokens in `usage` messages, and `agent.getUsage()` reports billed cents later [V types]. | Each turn's `total_tokens` counts in wf's usage, and the transcript keeps `getUsage()` at close. | `workflow/harnesses/cursor_host/src/cursor.ts:411`, `workflow/harnesses/remote.py:608` | `test_tokens_that_the_sdk_reports_count_in_the_run_and_its_events`, `test_a_token_limit_counts_cursor_work` |
| `toTokenUsage()` makes the counts of each `usage` message, and sets `totalTokens` to `inputTokens + outputTokens + cacheReadTokens + cacheWriteTokens`. `reasoningTokens` is a part of the output [V bundle: `./src/agent/usage-types.ts` in `dist/esm/index.js`, called by both builders of a `usage` message, in `dist/esm/index.js` and `dist/esm/448.js`. V types: `usage-types.d.ts`]. | The host passes the counts on as they are. wf takes `input_tokens` as the input that the cache did not read or write, and `cached_input_tokens` as the cache reads. | `workflow/harnesses/cursor_host/src/cursor.ts:461`, `workflow/harnesses/remote.py:402` | `cursor_host/test/host.test.ts`, `test_the_trace_keeps_the_parts_of_the_tokens_that_the_sdk_reports` |
| A local run sends a block of thinking as `thinking` messages with the pieces of its text. Then one `thinking` message with empty text gives `thinking_duration_ms` [V bundle: `dist/esm/index.js` of SDK 1.0.38 is one line of 2,331,126 bytes. It maps `thinking-delta` at byte 2,290,861 and `thinking-completed` at byte 2,290,938 that way]. The types make `thinking_duration_ms` optional [V types: `messages.d.ts`]. A composer-2.5 turn gave blocks with their times [V live, 2026-10-21]. | The host joins the pieces of a block, and sends one `turn.thinking` event with the text and `duration_ms` when the block ends. A block that the end of the turn cuts off has `duration_ms: null`. The trace gets a `thinking` record on the turn. | `workflow/harnesses/cursor_host/src/cursor.ts:306`, `workflow/harnesses/cursor_host/src/cursor.ts:276`, `workflow/harnesses/remote.py:546` | `cursor_host/test/session.test.ts`, `test_the_trace_has_cursors_thinking_with_its_time_and_the_id_of_each_request`, `test_a_real_cursor_turn_gives_the_trace_its_request_id_and_the_time_of_its_thinking` (`-m harness`), which fails when a block has no time |
| A local run writes its `request` message, with the id of the send, only to the agent's store. The flag `emitLive: false` keeps it out of `run.stream()` [V bundle: `dist/esm/index.js` writes the message with `emitLive:!1` at byte 2,288,673. It streams a message only when `!1!==r?.emitLive`, at byte 2,288,311]. The `Run` that `send()` returns has the same id as `requestId` [V bundle: byte 2,293,528]. So does its `RunResult` [V bundle: `wait()` returns the id at byte 2,281,654]. Both are optional in the types [V types: `run.d.ts`]. A composer-2.5 turn gave one id [V live, 2026-10-21]. | The host sends one `turn.request` event for each id that the run, a `request` message or the result gives, once for each id. The trace gets a `request` record on the turn. | `workflow/harnesses/cursor_host/src/cursor.ts:256`, `workflow/harnesses/cursor_host/src/cursor.ts:267`, `workflow/harnesses/cursor_host/src/cursor.ts:314`, `workflow/harnesses/remote.py:546` | `cursor_host/test/session.test.ts`, `test_the_trace_has_cursors_thinking_with_its_time_and_the_id_of_each_request`, `test_a_real_cursor_turn_gives_the_trace_its_request_id_and_the_time_of_its_thinking` (`-m harness`), which fails when a turn has no request id |
| `Agent.resume(id)` loads an agent from the local store, which `JsonlLocalAgentStore(folder)` keeps in a folder, and `local.force` expires a run that a crashed host left active [V types]. | Each session has its own store folder. One that a later pass may continue moves to `~/.wf/sessions/<run-id>-<hash>/cursor-sdk/`, which outlives the run's success when a follow-up can continue it. A loaded agent's first send has `force`. | `workflow/harnesses/cursor_host/src/cursor.ts:174`, `workflow/harnesses/cursor.py:360` | `test_continue_loads_the_last_passs_agent`, `test_after_a_continued_attempt_fails_the_retry_starts_fresh`, `test_a_follow_up_continues_the_agent_that_the_run_ended_with` |
| The SDK can reject a promise that nothing waits for when a shell can't start, which ends a Node process [V t3code: `CursorSdk.ts`]. The SDK loads its chunks from disk, so it can't be bundled into one file [V t3code: `sdk.ts`]. | The host keeps running on that one error, and exits on any other. wf runs one host per session, installed with the SDK beside it. | `workflow/harnesses/cursor_host/src/main.ts:27`, `workflow/harnesses/cursor.py:198` | `test_a_promise_that_the_sdk_leaves_unawaited_ends_the_host_and_the_attempt`, `test_the_first_session_installs_the_sdk_from_the_lockfile` |
| The SDK's error classes carry their names, while the bundle's class names are short: `AuthenticationError` reads as `e.name`, and its constructor is named `a` [V live]. | The host classifies errors by `name`. | `workflow/harnesses/cursor_host/src/cursor.ts:476` | `cursor_host/test/host.test.ts` |

