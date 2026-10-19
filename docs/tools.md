# Tools

An agent node can read Slack, Granola meeting notes, Linear, Notion and Gmail through tools that wf brings itself.
Each tool is the vendor's own MCP server.
You sign in to it once per machine with `wf tools <name> setup`, which keeps the secret in the macOS Keychain.
A node lists the tools its agent may use, and wf attaches them to that agent's session on Claude Code or Codex.
Your own Claude Code connectors don't matter here: wf's headless sessions don't load them, and Codex has none.

```sh
wf tools list              # each tool, how it signs in, and whether it's set up
wf tools linear setup      # sign in once
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

Notion also publishes a local server (`notion-mcp-server`), but Notion says it's no longer actively maintained and points to the remote one.
Google's Gmail server is a Developer Preview.

## Setting up

`wf tools <name> setup` signs in with OAuth's authorization code flow and PKCE, the way MCP's authorization spec describes:

1. It reads the MCP server's metadata (RFC 9728), which names the vendor's authorization server.
   That server's metadata (RFC 8414) gives the endpoints and scopes.
2. It registers wf as an OAuth client (RFC 7591) with Granola, Linear and Notion.
   Slack and Google don't allow that, so for those it asks for the ID and secret of a client you made.
3. It opens your browser at the vendor's sign-in page, and waits for the redirect back on `http://localhost:47152/callback`.
   `--port` picks another port.
4. It trades the code for tokens, keeps them in the macOS Keychain, and runs doctor.

With `--token`, setup asks you to paste a token instead, for the vendors that give one out.
A pasted token has no refresh: when the vendor stops taking it, run setup again.

Setup needs a terminal, and you.
Run it again to replace a sign-in.

### Slack

Slack's MCP server only takes OAuth clients that are Slack apps, and only internal apps or apps in the Slack Marketplace.
So make an internal app for wf once:

1. At https://api.slack.com/apps, create a new app from scratch in your workspace.
2. Under Agents, turn on "Slack Model Context Protocol (MCP) Server".
3. Under OAuth & Permissions, add the redirect URL `http://localhost:47152/callback`, and the user token scopes that setup prints.
   They're the scopes Slack's server lists:
   `canvases:read canvases:write channels:history channels:read channels:write chat:write emoji:read files:read files:write groups:history groups:read groups:write im:history im:read im:write lists:read lists:write mpim:history mpim:read mpim:write reactions:read reactions:write search:read.files search:read.im search:read.mpim search:read.private search:read.public search:read.users users:read users:read.email`.
4. Under Basic Information, copy the Client ID and the Client Secret.
   Setup asks for them.

`wf tools slack setup --token` takes a user token (`xoxp-...`) instead.
For one, install the app to your workspace, and copy the User OAuth Token from OAuth & Permissions.

### Gmail

Google's Gmail MCP server takes OAuth clients that you make, in a Google Cloud project:

1. Join the Google Workspace Developer Preview Program, which the Gmail MCP server is part of.
2. In a Google Cloud project, enable the Gmail API and the Gmail MCP API.
3. Set up the OAuth consent screen: Internal, or External with you as a test user.
   Give it the scopes `https://www.googleapis.com/auth/gmail.readonly` and `https://www.googleapis.com/auth/gmail.compose`.
4. Create an OAuth client ID of type Web application.
   Tick "Use this client for an AI-powered agent", and add the authorized redirect URI `http://localhost:47152/callback`.
5. Copy its Client ID and Client Secret.
   Setup asks for them.

Google gives a refresh token only to a request with `access_type=offline`, so setup asks for one, with `prompt=consent`.

### Granola, Linear and Notion

There's nothing to make: wf registers itself.
For Granola, your plan decides what the server shows.
On the free plan, that's notes from the last 30 days, and some operations are for paid plans only.

- `wf tools linear setup --token` takes a Linear API key (Settings > Security & access).
  A key with only the Read permission is enough for read-only nodes.
- `wf tools notion setup --token` takes a Notion MCP token, which workspace owners on the Business and Enterprise plans make in Notion's Developer portal.

## Doctor

`wf tools <name> doctor` asks three questions, and stops at the first that fails:

1. Is there a sign-in in the Keychain?
2. Is it still good? Connecting refreshes an expired token, and the server must take it.
3. Does a harmless read call work? The table above lists each tool's.

```
linear: pass
  secret     an OAuth sign-in in the macOS Keychain, set up 2026-10-19
  sign-in    accepted by https://mcp.linear.app/mcp, which offers 41 read operations
  read call  `get_user` answered

slack: fail
  secret     an OAuth sign-in in the macOS Keychain, set up 2026-10-19
  problem    Slack didn't refresh the sign-in (invalid_grant)
  next step  run `wf tools slack setup`
```

`wf tools doctor` checks every tool.
A tool that isn't set up is reported, and fails the command only when you asked about that one tool.

## In a workflow

`tools` is an agent node field, and can go in `defaults` like the others.
Each entry is a tool's name, which gives the agent that tool's read operations.
Or it's `{name: ..., write: true}`, which gives the agent every operation the server offers.
Posting, sending and creating reach other people, so a node only gets them when it asks for them by name.

`wf validate` checks that each tool exists and is listed once, and warns about one that isn't set up on this machine.
A node with tools can't run on Cursor yet.
So validate refuses a node that could, and a run skips Cursor for it as it would an unavailable harness.

When a node runs, each attempt connects to its tools first.
A tool that isn't set up, or whose sign-in the vendor refuses, fails the node at once with what to run, since a retry would find the same.
A server that doesn't answer fails just the attempt, and `retry` applies.
Dry runs attach no tools, so they work without any being set up.

## What counts as a read

These are the operations a node gets without `write: true`.
An operation a server offers that isn't listed, a new one included, counts as a write: the agent isn't told about it, and wf refuses a call to it.
The lists are in `workflow/tools/catalog.py`.

- **slack**: `slack_search_public`, `slack_search_public_and_private`, `slack_search_channels`, `slack_search_users`, `slack_search_emojis`, `slack_read_channel`, `slack_read_thread`, `slack_read_canvas`, `slack_read_file`, `slack_read_list`, `slack_read_user_profile`, `slack_list_user_channels`, `slack_list_channel_members`, `slack_get_reactions`.
  Writes include sending, scheduling and drafting messages, reactions, canvases, lists and file uploads.
- **granola**: `query_granola_meetings`, `list_meetings`, `get_meetings`, `get_meeting_transcript`, `get_meeting_attachment`, `list_meeting_folders`, `get_account_info`.
  Not reads: `switch_workspace` (it changes the workspace in the Granola app), `sign_out`, and the folder operations.
- **linear**: `list_issues`, `get_issue`, `list_comments`, `list_issue_statuses`, `get_issue_status`, `list_issue_labels`, `list_projects`, `get_project`, `list_project_labels`, `list_milestones`, `get_milestone`, `list_initiatives`, `get_initiative`, `list_initiative_labels`, `get_status_updates`, `list_documents`, `get_document`, `get_attachment`, `extract_images`, `list_cycles`, `list_teams`, `get_team`, `list_users`, `get_user`, `get_workspace`, `list_custom_views`, `list_templates`, `get_template`, `get_triage_responsibility`, `get_notifications`, `list_releases`, `get_release`, `list_release_notes`, `get_release_note`, `list_release_pipelines`, `list_diffs`, `get_diff`, `get_diff_threads`, `list_agent_skills`, `get_agent_skill`, `search_documentation`.
  Writes are the `save_`, `create_`, `delete_`, `restore_` and `retire_` operations, and the ones that mark, share, merge or review.
- **notion**: `notion-search`, `notion-fetch`, `notion-get-self`, `notion-get-users`, `notion-get-teams`, `notion-get-groups`, `notion-get-comments`, `notion-get-tool-access`, `notion-get-async-task`, `notion-list-private-pages`, `notion-list-shared-pages`, `notion-list-favorite-pages`, `notion-list-recent-pages`, `notion-list-page-backlinks`, `notion-query-data-sources`, `notion-query-multiple-data-sources`, `notion-query-meeting-notes`, `notion-memory-search`, `notion-get-file-download-urls`, `notion-download-attachment`, `notion-get-enabled-skills`, `notion-download-skill`, `notion-search-agents`, `notion-query-sessions`, `notion-search-sessions`, `notion-get-session-status`, `notion-wait-session`, `notion-list-session-events`, `notion-read-session-event`, `notion-calendar-list-calendars`, `notion-calendar-list-events`, `notion-calendar-list-coworkers-events`, `notion-calendar-list-contacts`, `notion-calendar-list-calendar-resources`.
  These follow Notion's own split into read-only and write tools.
- **gmail**: `search_threads`, `get_thread`, `get_message`, `list_labels`, `list_drafts`, `get_draft`.
  Writes include drafts, sending, replying, forwarding, labels, trash and spam.

## How it works

wf is the vendor server's only MCP client.
The agent sees each tool the way it sees `submit_result`: as a server that wf hosts in its own process.
On Claude Code that's an SDK MCP server named after the tool (`mcp__linear__list_issues`).
On Codex it's a namespace of dynamic tools (`linear`).
Each call comes back to wf, which passes it on to the vendor with the stored token.
This keeps two promises the same way on every harness:

- **The secret stays in wf.**
  It isn't in the agent's environment, its command line, a config file, a workflow file or the run folder.
  Handing the vendor's URL and token to the harness instead would put the token on Claude Code's command line, because the Agent SDK passes MCP servers there.
  Any command the agent runs could then read it with `ps`.
- **Read-only holds in one place.**
  The agent is only told about the operations its node may use, and wf refuses any other name.
  The harnesses' own filters couldn't do this everywhere: Claude Code's `full` access allows every tool, and Cursor's protocol (ACP) has no tool filter.

The Keychain item has the service `wf tools` and the tool's name as its account.
It holds the tokens, and what refreshing them needs: the token endpoint, and the client ID and secret.
Before an access token expires, wf refreshes it.
Linear and Notion replace the refresh token on every refresh, so two refreshes at once would break the sign-in.
A lock file in `~/.wf/tools/` makes them take turns, across wf processes too.

The Keychain keeps the secret out of files and out of the agent's view, but it doesn't fence it from other programs you run.
macOS lets the program that saved an item read it again without asking.
So an agent with `edit` or `full` access, which can run commands as you, could read it with the same Python that wf runs on.
A `read-only` agent has no shell.

## Sources

Checked on 2026-10-19.

- Slack: [Slack MCP server](https://docs.slack.dev/ai/mcp-server/) (endpoint, confidential OAuth clients only, no dynamic client registration, scopes per tool), [developing with it](https://docs.slack.dev/ai/slack-mcp-server/developing) (the Agents toggle), [PKCE](https://docs.slack.dev/authentication/using-pkce), [oauth.v2.user.access](https://docs.slack.dev/reference/methods/oauth.v2.user.access).
- Granola: [Granola MCP](https://docs.granola.ai/help-center/sharing/integrations/mcp) (endpoint, OAuth with dynamic client registration, no API keys, plans, tools).
- Linear: [Linear MCP server](https://linear.app/docs/mcp) (endpoint, OAuth 2.1 with dynamic client registration, API keys as bearer tokens).
- Notion: [Notion MCP](https://developers.notion.com/docs/mcp), [getting started](https://developers.notion.com/docs/get-started-with-mcp) (endpoint, OAuth, MCP tokens, the local server no longer maintained), [supported tools](https://developers.notion.com/docs/mcp-supported-tools) (read-only and write tools).
- Gmail: [configure the Gmail MCP server](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server) (endpoint, Developer Preview, Web application client, scopes, `access_type=offline`).
- MCP: [authorization](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization) (protected resource metadata, PKCE, `resource`).
- Each server's metadata gave the endpoints and scopes above.
  It's at `/.well-known/oauth-protected-resource`, and at its authorization server's `/.well-known/oauth-authorization-server`, read on the same day.
- The operation names are the ones each server listed on the same day.
