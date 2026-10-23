# Tools

An agent node can read Slack, Granola meeting notes, Linear, Notion and Gmail through tools that wf brings itself.
Each tool is the vendor's own MCP server.
You sign in to it once per machine with `wf tools <name> setup`, which keeps the secret in the macOS Keychain.
A node lists the tools its agent may use, and wf attaches them to that agent's session on Claude Code, Codex or Cursor.
Such a node gets only those tools: none of your account's connectors, and none of your own MCP servers.

```sh
wf tools list              # each tool, how it signs in, and whether it's set up
wf tools linear setup      # sign in once, for reads
wf tools linear doctor     # check that it works; `wf tools doctor` checks every tool
```

```yaml
nodes:
  brief:
    needs: [start]
    tools: [slack, granola, linear]          # read-only
    instruction: Summarize what happened in #releases and in today's meetings.
  file:
    needs: [brief]
    tools: [{name: linear, write: true}]     # may also create and change issues
    instruction: "File an issue for each follow-up in: {{ brief.summary }}"
```

## The tools

Every vendor here hosts one remote MCP server and signs users in with OAuth, so each tool uses that server.
None of them needs a program on your machine.

| Tool | Server | Sign-in | Doctor's read call |
|---|---|---|---|
| `slack` | Slack's MCP server, `https://mcp.slack.com/mcp` | OAuth with a Slack app you make, or a pasted user token | `slack_read_user_profile` (you) |
| `granola` | Granola's MCP server, `https://mcp.granola.ai/mcp` | OAuth, and wf registers itself | `get_account_info` |
| `linear` | Linear's MCP server, `https://mcp.linear.app/mcp` | OAuth, and wf registers itself. Or a pasted API key | `get_user` (`me`) |
| `notion` | Notion's MCP server, `https://mcp.notion.com/mcp` | OAuth, and wf registers itself. Or a pasted MCP token | `notion-get-self` |
| `gmail` | Google's Gmail MCP server, `https://gmailmcp.googleapis.com/mcp/v1` | OAuth with a Google Cloud OAuth client you make | `list_labels` |

Notion also publishes a local server (`notion-mcp-server`).
But Notion says it's no longer actively maintained, and points to the remote one.
Google's Gmail server is a Developer Preview.

## Setting up

`wf tools <name> setup` signs in with OAuth's authorization code flow and PKCE, as MCP's authorization spec (revision 2026-07-28) describes.
It needs a terminal, and you.
Run it again to replace a sign-in, for example with `--write` when a node starts to write.

The spec prefers Client ID Metadata Documents for the OAuth client, which need an HTTPS URL that serves the client's metadata.
wf has no such URL, so with Granola, Linear and Notion it registers itself instead (RFC 7591).
The spec deprecates that registration, but keeps it for compatibility.
Slack and Google allow neither, so you make the client.

The tests named below are in `tests/test_tools.py`.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| Setup starts | It asks the MCP server for a session without a token. It reads the protected resource metadata (RFC 9728) that the 401 names, or else looks in the well-known places. It reads the authorization server's metadata (RFC 8414 or OpenID Connect Discovery), trying the URLs in the spec's order. | `workflow/tools/signin.py:166`, `workflow/tools/signin.py:175` | `test_discovery_follows_the_401_and_finds_openid_metadata_appended_to_an_issuer_path` |
| The authorization server's metadata names another issuer than the one wf asked about | Setup stops, and uses nothing from it: the two must be identical (RFC 8414). wf asks about the issuer the resource metadata names. For Gmail it asks about `https://accounts.google.com`, as Google's metadata writes it, because Google's resource metadata adds a last slash. | `workflow/tools/signin.py:179`, `workflow/tools/catalog.py:147` | `test_metadata_that_names_another_issuer_isnt_used`, `test_an_issuer_written_with_another_last_slash_is_another_issuer_unless_the_catalog_pins_it` |
| The tool is Granola, Linear or Notion | wf registers itself as a public OAuth client, with the redirect URL it waits on. | `workflow/tools/signin.py:123` | `test_setup_signs_in_in_the_browser_and_keeps_the_secret_in_the_keychain` |
| The tool is Slack or Gmail | Setup prints how to make the client, with the redirect URL and the scopes, and asks for its ID and secret. | `workflow/tools/signin.py:117` | `test_setup_with_a_client_of_yours_shows_how_to_make_it_and_asks_for_its_id_and_secret` |
| The tool is Slack or Linear | Setup asks for the read scopes, and stores the sign-in as one for reads only. With `--write`, it asks for the write scopes too. | `workflow/tools/signin.py:183` | `test_setup_signs_in_in_the_browser_and_keeps_the_secret_in_the_keychain`, `test_setup_asks_for_the_write_scopes_only_with_write` |
| The tool is Granola or Notion | Setup asks for the scopes the server's 401 names, or else those its resource metadata lists, and `offline_access` when the authorization server lists it. One sign-in does reads and writes. | `workflow/tools/signin.py:185` | `test_discovery_follows_the_401_and_finds_openid_metadata_appended_to_an_issuer_path` |
| The tool is Gmail | Setup asks for the two scopes Google's guide gives, with `access_type=offline` and `prompt=consent`, and sends no `resource`. | `workflow/tools/catalog.py:145` | `test_gmail_asks_google_for_offline_access_and_sends_no_resource` |
| The browser opens the sign-in page | Setup waits for the redirect on `http://localhost:47152/callback`, or on the port that `--port` gives, else `signin_port` under `[tools]` in `~/.wf/config.toml`. | `workflow/tools/signin.py:132`, `workflow/cli.py:671` | `test_setup_signs_in_in_the_browser_and_keeps_the_secret_in_the_keychain`, `test_setup_waits_for_the_redirect_on_signin_port_of_config_toml_unless_port_is_given` |
| A request reaches the callback without the `state` that setup sent | It gets an error page, and setup keeps waiting. | `workflow/tools/signin.py:394` | `test_a_request_to_the_callback_without_the_sign_ins_state_is_turned_away_and_the_wait_goes_on` |
| The redirect's `iss` isn't the issuer, exactly | Setup stops, and doesn't read the rest of the redirect, an error included. | `workflow/tools/signin.py:139` | `test_the_sign_ins_iss_must_be_the_issuer_exactly`, `test_a_sign_in_from_another_issuer_isnt_read_even_for_its_error` |
| The redirect has no `iss`, and the metadata says the server sends one | Setup stops. | `workflow/tools/signin.py:141` | `test_a_sign_in_without_the_iss_the_server_says_it_sends_is_ignored` |
| The vendor says no (`error=access_denied`), or sends no code | Setup stops, and asks for no token. | `workflow/tools/signin.py:144`, `workflow/tools/signin.py:146` | `test_a_sign_in_that_comes_back_wrong_gets_no_token` |
| No redirect comes back within 5 minutes | Setup stops. | `workflow/tools/signin.py:138` | `test_setup_gives_up_when_no_sign_in_comes_back` |
| The redirect is good | Setup trades the code for tokens with the PKCE verifier, saves them in the Keychain item, and runs doctor. | `workflow/cli.py:675`, `workflow/cli.py:680` | `test_setup_signs_in_in_the_browser_and_keeps_the_secret_in_the_keychain` |
| `--token` | Setup asks you to paste a token instead, saves it, and runs doctor. A pasted token has no refresh. Only the tools whose vendor gives tokens out take `--token`. | `workflow/tools/signin.py:109` | `test_setup_takes_a_pasted_token`, `test_only_tools_whose_vendor_gives_out_tokens_take_one` |
| There's no terminal | Setup refuses, since it asks you to sign in. | `workflow/cli.py:669` | `test_setup_needs_a_terminal` |

### Slack

Slack's MCP server only takes OAuth clients that are Slack apps.
And only internal apps or apps in the Slack Marketplace can use it.
So make an internal app for wf once:

1. At https://api.slack.com/apps, create a new app from scratch in your workspace.
2. Under Agents, turn on "Slack Model Context Protocol (MCP) Server".
3. Under OAuth & Permissions, add the redirect URL `http://localhost:47152/callback`.
   With `--port`, or `signin_port` under `[tools]` in `~/.wf/config.toml`, the URL has that port instead.
   Add the user token scopes that setup prints, which are the read scopes in the table below.
   With `--write`, setup asks for the write scopes too, so the app needs them as well.
4. Under Basic Information, copy the Client ID and the Client Secret.
   Setup asks for them.

| Slack scopes | |
|---|---|
| Read (always) | `search:read.public search:read.private search:read.mpim search:read.im search:read.files search:read.users channels:history groups:history mpim:history im:history channels:read groups:read mpim:read im:read files:read canvases:read lists:read reactions:read emoji:read users:read users:read.email` |
| Write (with `--write`) | `chat:write channels:write groups:write im:write mpim:write reactions:write canvases:write lists:write files:write` |

`wf tools slack setup --token` takes a user token (`xoxp-...`) instead.
For one, install the app to your workspace, and copy the User OAuth Token from OAuth & Permissions.
Such a token has the scopes the app has.

### Gmail

Google's Gmail MCP server takes OAuth clients that you make, in a Google Cloud project:

1. Join the Google Workspace Developer Preview Program, which the Gmail MCP server is part of.
2. In a Google Cloud project, enable the Gmail API and the Gmail MCP API.
3. Set up the OAuth consent screen as Internal.
   Give it the scopes `https://www.googleapis.com/auth/gmail.readonly` and `https://www.googleapis.com/auth/gmail.compose`, which Google's guide gives.
   External works too, with you as a test user.
   But while an External app is in Testing, Google ends its sign-ins after 7 days, and setup has to run again.
4. Create an OAuth client ID of type Web application.
   Tick "Use this client for an AI-powered agent", and add the authorized redirect URI `http://localhost:47152/callback`.
   With `--port`, or `signin_port` under `[tools]` in `~/.wf/config.toml`, the URI has that port instead.
5. Copy its Client ID and Client Secret.
   Setup asks for them.

Google gives a refresh token only to a request with `access_type=offline`, so setup asks for one, with `prompt=consent`.

### Granola, Linear and Notion

There's nothing to make: wf registers itself.
For Granola, your plan decides what the server shows.
On the free plan, that's notes from the last 30 days, and some operations are for paid plans only.

- `wf tools linear setup --token` takes a Linear API key (Settings > Security & access).
  A key with only the Read permission is enough for read-only nodes.
- `wf tools notion setup --token` takes a Notion MCP token.
  Workspace owners on the Business and Enterprise plans make one in Notion's Developer portal.

## Doctor

`wf tools <name> doctor` asks four questions, and stops at the first that fails:

1. Is there a sign-in in the Keychain?
2. Is it still good?
   Connecting refreshes an expired token, and the server must take it.
3. Does a harmless read call work?
   The first table on this page names each tool's.
4. Does wf's list of reads still fit the server's own list of operations?

```
linear: pass
  secret     an OAuth sign-in for reads only in the macOS Keychain, set up 2026-10-19
  sign-in    accepted by https://mcp.linear.app/mcp, which offers 41 read operations
  read call  `get_user` answered
  kept back  save_issue, delete_issue, ...: only for nodes with `write: true`

slack: fail
  secret     an OAuth sign-in for reads only in the macOS Keychain, set up 2026-10-19
  problem    Slack didn't refresh the sign-in (invalid_grant)
  next step  run `wf tools slack setup`
```

The tests named below are in `tests/test_tools.py`.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| The Keychain holds no sign-in for the tool, or one wf didn't write | `not set up`, with `run wf tools <name> setup`. `wf tools doctor` doesn't fail for it, and `wf tools <name> doctor` exits with 1. | `workflow/tools/doctor.py:50`, `workflow/cli.py:694` | `test_doctor_for_every_tool_reports_one_that_is_not_set_up_without_failing`, `test_a_stored_item_wf_didnt_write_counts_as_not_set_up` |
| The Keychain can't be read | `fail`, with "unlock your login Keychain". | `workflow/tools/doctor.py:48` | `test_doctor_says_when_the_keyring_cannot_be_read` |
| The access token has expired | It's refreshed, and the checks go on. | `workflow/tools/signin.py:227` | `test_doctor_refreshes_a_sign_in_that_has_expired` |
| The vendor refuses the sign-in, or the refresh | `fail`, with `run wf tools <name> setup`. | `workflow/tools/doctor.py:65` | `test_doctor_says_to_sign_in_again_when_the_vendor_refuses` |
| The server can't be reached | `fail`, with "check your network connection". | `workflow/tools/doctor.py:69` | `test_doctor_reports_a_server_it_cannot_reach` |
| The server no longer offers the read call | `fail`, with "update wf". | `workflow/tools/doctor.py:61` | `test_doctor_says_to_update_wf_when_the_server_no_longer_offers_its_read_call` |
| The read call answers an error | `fail`, with what the server said. | `workflow/tools/doctor.py:72` | `test_doctor_fails_when_the_read_call_fails` |
| The server offers operations that aren't on wf's list of reads, or doesn't offer some that are | The report names them, under `kept back` and `missing`. | `workflow/tools/doctor.py:83`, `workflow/tools/doctor.py:85` | `test_doctor_names_the_operations_it_keeps_back_and_the_reads_not_offered` |
| The server marks an operation read-only (`readOnlyHint`), and wf keeps it back | The report adds a note that suggests adding it to the list. | `workflow/tools/doctor.py:88` | `test_doctor_notes_an_operation_the_server_marks_read_only_that_wf_keeps_back` |
| wf counts an operation as a read, and the server marks it as changing things (`readOnlyHint: false` or `destructiveHint: true`) | `fail`, with "update wf". | `workflow/tools/doctor.py:93` | `test_doctor_fails_when_a_read_on_wfs_list_is_marked_as_changing_things` |
| All four checks pass | `pass` | `workflow/tools/doctor.py:95` | `test_doctor_passes_when_the_sign_in_works_and_a_read_call_answers` |
| Setup saved a sign-in that doctor fails | Setup exits with 1. | `workflow/cli.py:682` | `test_setup_that_saves_a_sign_in_doctor_rejects_exits_with_1` |

## In a workflow

`tools` is an agent node field, and can go in `defaults` like the others.
Each entry is a tool's name, which gives the agent that tool's read operations.
Or it's `{name: ..., write: true}`, which gives the agent every operation the server offers.
That includes deleting, and for Granola signing out.
Posting, sending and creating reach other people, so a node only gets them when it asks for them by name.

A node with tools must not have a shell that reaches the Keychain, where every sign-in is stored, unless it says so.
That's Claude Code and Cursor at `edit` and `full` access, and Codex at `full` (see Who can reach the Keychain).
There, the node needs `tools_with_shell: true`, set on the node itself.
Such an agent could read every stored sign-in, not just its own tools': a Gmail, Granola or Notion one included, which can write.
A node that needs both tools and a shell can often be two nodes instead.
A read-only node with tools gathers, and a node without tools writes files from its output.

A node that lists `tools` gets exactly those, and no other MCP server or connector, at every access level.
`tools: []` gives it none at all.
A node without `tools` gets what it got before, which on Claude Code at `full` access includes your claude.ai connectors.

The tests named below are in `tests/test_tools.py` for validation, and in `tests/test_tool_sessions.py` for runs.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| A `tools` entry names an unknown tool, or one that's listed twice | `wf validate` errors. | `workflow/validate.py:720`, `workflow/validate.py:722` | `test_validate_knows_the_tools_and_warns_about_one_not_set_up` |
| `defaults.tools` has such an entry | The same errors, once, even in a workflow without an agent node. | `workflow/validate.py:154` | `test_tools_in_defaults_are_checked_even_without_an_agent_node` |
| `write` isn't `true` or `false` | `wf validate` errors. | `workflow/spec.py:147` | `test_tools_entries_have_a_shape` |
| A tool isn't set up on this machine | `wf validate` warns. | `workflow/validate.py:734` | `test_validate_knows_the_tools_and_warns_about_one_not_set_up` |
| A node has `write: true`, and the tool is set up for reads only | `wf validate` warns, and the node fails when it runs, with `run wf tools <name> setup --write`. | `workflow/validate.py:736`, `workflow/tools/relay.py:94` | `test_a_node_that_writes_with_a_read_only_sign_in_gets_a_warning`, `test_attach_refuses_a_writing_node_a_read_only_sign_in` |
| An agent with `access: full` reads the output of an agent with tools | `wf validate` warns: messages and mail can carry instructions aimed at agents. | `workflow/validate.py:607` | `test_a_full_access_agent_that_reads_what_a_tool_using_agent_wrote_gets_a_warning` |
| An attempt starts | It connects to the node's tools. The session gets each tool's read operations, or all of them with `write: true`. | `workflow/nodes.py:444`, `workflow/tools/relay.py:60` | `test_each_node_gets_its_own_tools_and_only_their_reads_unless_it_opts_in`, `test_every_page_of_a_servers_operations_is_attached` |
| The agent calls an operation its node may not use | wf refuses the call, which never reaches the vendor. | `workflow/tools/relay.py:76` | `test_a_read_only_node_cannot_call_a_write` |
| A tool isn't set up, or its server refuses the sign-in when the attempt connects | The node fails at once, with what to run: a retry would find the same. | `workflow/nodes.py:679` | `test_a_tool_that_is_not_set_up_fails_the_node_without_a_retry`, `test_a_sign_in_the_server_refuses_when_connecting_fails_the_node_without_a_retry` |
| Two tools fail to connect | The failure that setup fixes is the one reported. | `workflow/tools/relay.py:101` | `test_the_failure_that_setup_fixes_wins_when_two_tools_fail` |
| A tool's server doesn't answer | The attempt fails, and `retry` applies. | `workflow/nodes.py:681` | `test_a_server_that_does_not_answer_fails_the_attempt_and_the_retry_tries_again` |
| During the session, a tool's sign-in is refused or its connection is lost | The call's answer says why, and the attempt fails after the session, even when the agent submitted a result. | `workflow/tools/relay.py:154`, `workflow/nodes.py:675` | `test_a_sign_in_refused_in_the_middle_of_a_session_says_what_to_run`, `test_a_node_whose_tool_stopped_working_fails_even_with_a_result` |
| An operation answers with an error | The agent reads the error, as it would any tool's, and nothing fails. | `workflow/tools/relay.py:84` | `test_an_operation_that_answers_with_an_error_doesnt_fail_the_node` |
| A node with tools can run where its commands reach the Keychain, and doesn't set `tools_with_shell: true` | `wf validate` errors, naming each such harness, so the run doesn't start. | `workflow/validate.py:694` | `test_a_node_with_tools_and_a_shell_that_reaches_the_keychain_needs_the_opt_in`, `test_a_harness_list_names_each_harness_whose_shell_reaches_the_keychain` |
| A template picks such a harness at run time, which `wf validate` can't see, and the node doesn't set `tools_with_shell: true` | The run refuses that harness, and the node fails without a session. | `workflow/nodes.py:464` | `test_a_run_refuses_a_harness_whose_shell_reaches_the_keychain_without_the_opt_in` |
| Such a node sets `tools_with_shell: true` | `wf validate` warns, naming the node and its tools, and the run attaches the tools. | `workflow/validate.py:691` | `test_a_node_that_opts_in_gets_a_warning_naming_it_and_its_tools`, `test_a_run_lets_a_node_that_opted_in_have_tools_and_a_shell` |
| `tools_with_shell` isn't `true` or `false`, or is in `defaults` | `wf validate` errors: each such node says so itself. | `workflow/spec.py:194` | `test_tools_with_shell_takes_a_boolean_and_matters_only_with_tools`, `test_tools_with_shell_goes_on_the_node_not_in_defaults` |
| A node lists `tools`, `tools: []` included | Its agent gets those wf tools and no other MCP server or connector, at every access level. Claude Code leaves out the repo's MCP servers, yours, and your claude.ai connectors. Codex switches off the MCP servers and plugins in your config, and never loads your ChatGPT connectors. Cursor's SDK gets `mcpServers: {}`, which replaces the MCP servers Cursor would load, and a call to another server's tool ends the turn. Through `cursor-agent`, wf refuses other servers' tools. | `workflow/nodes.py:498`, `workflow/harnesses/claude_code.py:186`, `workflow/harnesses/codex.py:187`, `workflow/harnesses/cursor_host/src/cursor.ts:152`, `workflow/harnesses/cursor_acp.py:520` | `test_a_node_that_lists_tools_gets_no_other_mcp_server_and_one_without_keeps_them`, `test_claude_code_leaves_out_other_mcp_servers_and_claude_ai_connectors_for_a_node_with_tools`, `test_codex_switches_off_your_mcp_servers_and_plugins_for_a_node_with_tools`, `test_cursors_sdk_gets_a_custom_tool_per_operation_and_its_calls_are_relayed`, `test_a_tool_that_a_read_only_session_was_not_given_stops_the_turn` in `tests/test_cursor.py`, `test_cursor_refuses_other_mcp_servers_tools_for_a_node_with_tools_even_at_full`, and with a real Claude Code, `test_a_real_claude_code_node_with_tools_at_full_access_sees_no_other_mcp_server` |
| A node doesn't list `tools` | Its agent gets the MCP servers and connectors it got before. | `workflow/nodes.py:444` | `test_codex_keeps_your_mcp_servers_for_a_node_without_tools`, `test_claude_code_leaves_out_other_mcp_servers_and_claude_ai_connectors_for_a_node_with_tools` |
| A dry run | No tools are attached, so a dry run works without any set up. | `workflow/nodes.py:444` | `test_a_dry_run_attaches_no_tools` |

## What counts as a read

These are the operations a node gets without `write: true`.
An operation a server offers that isn't listed counts as a write, and so does a new one.
The agent isn't told about it, and wf refuses a call to it.
The lists are in `workflow/tools/catalog.py`, and `test_the_documented_reads_are_the_catalogs` checks that this table matches them.
The Slack, Granola, Linear and Notion lists follow the names each server listed on 2026-10-19.
The Gmail list follows the tools in Google's Gmail MCP reference for the server wf uses, last updated on 2026-07-21.
Doctor checks each list against its server's own list, as the Doctor section says.
So `wf tools <name> setup`, which runs doctor, checks the Gmail and Notion lists at the first sign-in.

| Tool | Read operations | Not reads (examples) |
|---|---|---|
| `slack` | `slack_search_public`, `slack_search_public_and_private`, `slack_search_channels`, `slack_search_users`, `slack_search_emojis`, `slack_read_channel`, `slack_read_thread`, `slack_read_canvas`, `slack_read_file`, `slack_read_list`, `slack_read_user_profile`, `slack_list_user_channels`, `slack_list_channel_members`, `slack_get_reactions` | sending, scheduling and drafting messages, reactions, canvases, lists, file uploads |
| `granola` | `query_granola_meetings`, `list_meetings`, `get_meetings`, `get_meeting_transcript`, `get_meeting_attachment`, `list_meeting_folders`, `get_account_info` | `switch_workspace`, which changes the workspace in the Granola app, and `sign_out` |
| `linear` | `list_issues`, `get_issue`, `list_comments`, `list_issue_statuses`, `get_issue_status`, `list_issue_labels`, `list_projects`, `get_project`, `list_project_labels`, `list_milestones`, `get_milestone`, `list_initiatives`, `get_initiative`, `list_initiative_labels`, `get_status_updates`, `list_documents`, `get_document`, `get_attachment`, `extract_images`, `list_cycles`, `list_teams`, `get_team`, `list_users`, `get_user`, `get_workspace`, `list_custom_views`, `list_templates`, `get_template`, `get_triage_responsibility`, `get_notifications`, `list_releases`, `get_release`, `list_release_notes`, `get_release_note`, `list_release_pipelines`, `list_diffs`, `get_diff`, `get_diff_threads`, `list_agent_skills`, `get_agent_skill`, `search_documentation` | the `save_`, `create_`, `delete_`, `restore_` and `retire_` operations, and the ones that mark, share, merge or review |
| `notion` | `notion-search`, `notion-ai-search`, `notion-fetch`, `notion-get-self`, `notion-get-users`, `notion-get-teams`, `notion-get-groups`, `notion-get-comments`, `notion-get-tool-access`, `notion-get-async-task`, `notion-list-private-pages`, `notion-list-shared-pages`, `notion-list-favorite-pages`, `notion-list-recent-pages`, `notion-list-page-backlinks`, `notion-query-data-sources`, `notion-query-multiple-data-sources`, `notion-query-meeting-notes`, `notion-memory-search`, `notion-get-file-download-urls`, `notion-download-attachment`, `notion-get-enabled-skills`, `notion-download-skill`, `notion-search-agents`, `notion-query-sessions`, `notion-search-sessions`, `notion-get-session-status`, `notion-wait-session`, `notion-list-session-events`, `notion-read-session-event`, `notion-calendar-list-calendars`, `notion-calendar-list-events`, `notion-calendar-list-coworkers-events`, `notion-calendar-list-contacts`, `notion-calendar-list-calendar-resources` | creating, changing, moving, deleting and restoring pages, databases, views, comments and skills, and starting, stopping or messaging a Custom Agent session |
| `gmail` | `search_threads`, `get_thread`, `get_message`, `list_labels`, `list_drafts` | `create_draft`, `create_label`, `label_message`, `label_thread`, `unlabel_message`, `unlabel_thread` |

The Notion list is wf's own choice, made from Notion's tool descriptions.
It has `notion-ai-search`, Notion's older search, which Notion's page calls read-only and which some connections still list.
Notion groups its tools by task, not by reads and writes.
The Custom Agent session operations that only look at a session count as reads: querying, searching, its status, waiting for it, and its events.
Starting, stopping or messaging a session counts as a write.

## How it works

wf is the vendor server's only MCP client.
The agent sees each tool the way it sees `submit_result`: as a server that wf hosts in its own process.
On Claude Code that's an SDK MCP server named after the tool (`mcp__linear__list_issues`).
On Codex it's a namespace of dynamic tools (`linear`).
On Cursor's SDK it's a custom tool of the SDK for each operation (`linear__list_issues`), which the Cursor host passes back to wf.
Through `cursor-agent` it's an MCP server named after the tool, at a URL on 127.0.0.1.
Only this session gets that URL, and it sits next to the one that serves `submit_result`.
Each call comes back to wf, which passes it on to the vendor with the stored token.
This keeps two promises the same way on every harness:

- **The secret stays in wf.**
  It isn't in the agent's environment, its command line, a config file, a workflow file or the run folder.
  Handing the vendor's URL and token to the harness instead would put the token on Claude Code's command line.
  The Agent SDK passes MCP servers there, and any command the agent runs could read them with `ps`.
- **Read-only holds in one place.**
  The agent is only told about the operations its node may use, and wf refuses any other name.
  The harnesses' own filters couldn't do this everywhere.
  Claude Code's `full` access allows every tool, and Cursor's protocol (ACP) has no tool filter.

Transcripts in the run folder keep each call to a wf tool: its operation, its arguments, whether it worked, and the size of the answer.
They don't keep the answer itself, which is other people's messages, notes and mail.
The agent's own messages and its result can still quote what it read.

### Keeping the sign-in fresh

The Keychain item has the service `wf tools` and the tool's name as its account.
It holds the tokens, and what refreshing them needs: the token endpoint, and the client ID and secret.
Some vendors replace the refresh token on every refresh, so two refreshes at once would break the sign-in.
The tests named below are in `tests/test_tools.py`, unless another file is named.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| A session asks for a token that expires within 60 seconds | wf refreshes it first. A token that expires later is used as it is. | `workflow/tools/signin.py:227` | `test_a_token_is_refreshed_within_a_minute_of_expiring` |
| The server refuses a token during a session | wf refreshes it once, and sends the request again. | `workflow/tools/relay.py:186` | `test_a_token_refused_in_the_middle_of_a_session_is_refreshed_once` (`tests/test_tool_sessions.py`) |
| Two sessions need a refresh at once | There's one refresh, and the second session gets the new token. | `workflow/tools/signin.py:206` | `test_two_refreshes_at_once_take_turns` |
| wf processes with different `WF_HOME` folders refresh one tool | They take turns: the lock is in a folder of the user's, in the temporary folder. | `workflow/tools/signin.py:266` | `test_the_refresh_lock_is_the_users_whatever_wf_home_says` |
| The session that asked for a refresh is cancelled, by a timeout for example | The refresh finishes in a task of its own, and saves the new tokens. | `workflow/tools/signin.py:208` | `test_a_refresh_finishes_and_is_saved_when_the_session_that_asked_is_cancelled` |
| The Keychain refuses to save new tokens | wf keeps them in memory, and saves them before the next refresh. | `workflow/tools/signin.py:245` | `test_new_tokens_the_keychain_refuses_are_kept_and_saved_before_another_refresh` |
| The vendor refuses a refresh, and the Keychain now holds a newer sign-in | wf uses the newer one. | `workflow/tools/signin.py:240` | `test_a_refused_refresh_reads_the_keychain_again_for_one_saved_meanwhile` |
| The vendor refuses a refresh | The sign-in is gone, and the message says to run setup. | `workflow/tools/signin.py:257` | `test_doctor_says_to_sign_in_again_when_the_vendor_refuses` |
| The token endpoint answers 5xx or 429 | The refresh can be tried again later: the attempt fails, and `retry` applies. | `workflow/tools/signin.py:259` | `test_a_refresh_the_vendor_is_too_busy_for_can_be_tried_again` |
| The vendor answers a refresh without a new refresh token | wf keeps the one it has, and uses it again. | `workflow/tools/signin.py:261` | `test_a_vendor_that_keeps_the_refresh_token_is_refreshed_with_it_again` |

### Why wf has its own OAuth client

The MCP SDK that wf uses (mcp 2.3.0) has an OAuth client of its own, `OAuthClientProvider`.
It covers discovery, registration, PKCE and refresh, and wf uses its discovery helpers.
But three needs of wf are outside it:

- Google gives a refresh token only to a request with `access_type=offline`, and the provider can't add a parameter.
- Slack's token endpoint answers with `token_type: user`, and with `ok: false` and HTTP 200 for an error.
  The provider's token model only takes `Bearer`.
- The provider refreshes within one process.
  wf runs refresh in several processes at once, and must not lose a rotated refresh token.

### Who can reach the Keychain

The Keychain keeps the secret out of files and out of the agent's view, but it doesn't fence it from other programs you run.
macOS lets the program that saved an item read it again without asking.
So an agent whose commands run as you, outside a sandbox, could read it with the same Python that wf runs on.
Which agents can do that depends on the harness and the access level:

| Access | Claude Code | Codex | Cursor |
|---|---|---|---|
| `read-only` | No: no shell, only file tools and wf's `git` tool | No: commands run in Codex's read-only sandbox, which keeps them from the Keychain | No: no shell, only wf's file tools and `git` tool |
| `edit` | Yes: commands run without a sandbox | No: commands run in Codex's workspace-write sandbox, which keeps them from the Keychain | Yes: through `cursor-agent`, commands run without a sandbox |
| `full` | Yes | Yes: there's no sandbox | Yes |

On 2026-10-19, `security show-keychain-info` failed in Codex's read-only and workspace-write sandboxes, and worked in `danger-full-access`.
In Claude Code, it worked with Claude Code's own Bash sandbox switched on as well.
So wf gives a node with tools one of the "Yes" places only with `tools_with_shell: true`, as In a workflow says.
Setup asks Slack and Linear for read scopes only, unless you give `--write`.
So a Slack or Linear token that an agent read could only read, unless setup had `--write`.
That doesn't hold for the other vendors, whose sign-ins can't be narrowed.
A Gmail token can also draft and send mail through the Gmail API, with the `gmail.compose` scope that Google's server needs.
Google's server itself has no tool that sends.
Granola and Notion tokens can do everything their servers offer.

## Sources

Checked on 2026-10-19.

| Source | What it gives |
|---|---|
| [Slack MCP server](https://docs.slack.dev/ai/mcp-server/) | The endpoint, confidential OAuth clients only, no dynamic client registration, and the scopes per tool |
| [Developing with Slack's MCP server](https://docs.slack.dev/ai/slack-mcp-server/developing) | The Agents toggle |
| [Slack PKCE](https://docs.slack.dev/authentication/using-pkce), [oauth.v2.user.access](https://docs.slack.dev/reference/methods/oauth.v2.user.access) | Slack's token answers |
| [Granola MCP](https://docs.granola.ai/help-center/sharing/integrations/mcp) | The endpoint, OAuth with dynamic client registration, no API keys, plans and tools |
| [Linear MCP server](https://linear.app/docs/mcp) | The endpoint, OAuth 2.1 with dynamic client registration, the `read` scope, and API keys as bearer tokens |
| [Notion MCP](https://developers.notion.com/docs/mcp), [getting started](https://developers.notion.com/docs/get-started-with-mcp) | The endpoint, OAuth, MCP tokens, and the local server no longer maintained |
| [Notion's supported tools](https://developers.notion.com/docs/mcp-supported-tools) | The tools and what each does |
| [Configure the Gmail MCP server](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server) | The endpoint, Developer Preview, the Web application client, the scopes, and `access_type=offline` |
| [Gmail MCP reference](https://developers.google.com/workspace/gmail/api/reference/mcp), last updated on 2026-07-21 | The Gmail operation names, for `https://gmailmcp.googleapis.com/mcp/v1`: the ten tools in its toolset table, and `create_label`, which its contents name |
| [Google OAuth 2.0: refresh token expiration](https://developers.google.com/identity/protocols/oauth2#expiration) | Refresh tokens that expire after 7 days for an External app in Testing |
| [MCP authorization, 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization), [its server discovery](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/authorization-server-discovery) | Protected resource metadata, the discovery order, client registration and its priority, PKCE, `resource`, and `iss` |
| [MCP versioning](https://modelcontextprotocol.io/specification/versioning) | 2026-07-28 is the current revision |
| Each server's metadata, at `/.well-known/oauth-protected-resource` and its authorization server's `/.well-known/oauth-authorization-server` | The endpoints and scopes above |
| The Slack, Granola, Linear and Notion tool lists, as the claude.ai connectors to the same servers listed them on 2026-10-19 | Those servers' operation names. claude.ai's Gmail connector listed other operations than Google's reference does, so its list doesn't give the Gmail names. Doctor checks each list against wf's own connection at the first sign-in. |
