"""Catalog: the tools wf can attach to an agent session, one vendor MCP server each.

Each entry says which server wf connects to, how `wf tools <name> setup` signs in, which of the
server's operations are reads, and the harmless read call that `wf tools <name> doctor` makes.
docs/tools.md has the sources for each choice.

`reads` is the whole of what a node gets unless it opts in to writes for that tool. An operation
the server offers that isn't listed, a new one included, counts as a write: the agent isn't told
about it and wf refuses calls to it.
"""

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Tool:
    name: str
    title: str  # the vendor's product, for messages
    url: str  # the vendor's MCP server (Streamable HTTP)
    reads: frozenset[str]
    probe: tuple[str, dict]  # doctor's read call: (operation, arguments)
    # OAuth client: "registered" means wf registers itself with the vendor (RFC 7591);
    # "yours" means you make one (a Slack app, a Google Cloud OAuth client) and paste its id and secret
    client: Literal["registered", "yours"] = "registered"
    client_help: str = ""  # how to make your client: {redirect} and {scopes} are filled in
    scopes: tuple[str, ...] = ()  # none: the scopes the server's metadata lists
    authorize: dict = field(default_factory=dict)  # extra parameters for the authorization request
    resource: bool = True  # send RFC 8707's `resource`, as MCP clients do (Google's OAuth predates it)
    token: str = ""  # a token the vendor gives out that `setup --token` takes instead, in words

    @property
    def sign_in(self) -> str:
        """How setup signs in, in words."""
        how = "OAuth in your browser" + (" with an OAuth client of yours" if self.client == "yours" else "")
        return f"{how}, or a pasted {self.token.split(' (')[0]}" if self.token else how


SLACK_HELP = """\
Slack's MCP server only takes OAuth clients that are Slack apps, so wf needs one (once):
1. At https://api.slack.com/apps, create a new app from scratch in your workspace.
2. Under Agents, turn on "Slack Model Context Protocol (MCP) Server".
3. Under OAuth & Permissions, add the redirect URL {redirect}
   and these user token scopes: {scopes}
4. Under Basic Information, copy the Client ID and the Client Secret."""

GMAIL_HELP = """\
Google's Gmail MCP server is a Developer Preview, and it takes OAuth clients that you make (once):
1. Join the Google Workspace Developer Preview Program, which the Gmail MCP server is part of.
2. In a Google Cloud project of yours, enable the Gmail API and the Gmail MCP API.
3. Set up the OAuth consent screen: Internal, or External with you as a test user,
   with the scopes {scopes}
4. Create an OAuth client ID of type Web application, tick "Use this client for an AI-powered
   agent", and add the authorized redirect URI {redirect}
5. Copy its Client ID and Client Secret."""

TOOLS: dict[str, Tool] = {tool.name: tool for tool in [
    Tool(
        name="slack",
        title="Slack",
        url="https://mcp.slack.com/mcp",
        client="yours",
        client_help=SLACK_HELP,
        token="Slack user token (xoxp-..., from your Slack app's OAuth & Permissions page once it's installed)",
        probe=("slack_read_user_profile", {"response_format": "concise"}),
        reads=frozenset({
            "slack_search_public", "slack_search_public_and_private", "slack_search_channels",
            "slack_search_users", "slack_search_emojis", "slack_read_channel", "slack_read_thread",
            "slack_read_canvas", "slack_read_file", "slack_read_list", "slack_read_user_profile",
            "slack_list_user_channels", "slack_list_channel_members", "slack_get_reactions",
        }),
    ),
    Tool(
        name="granola",
        title="Granola",
        url="https://mcp.granola.ai/mcp",
        probe=("get_account_info", {}),
        reads=frozenset({
            "query_granola_meetings", "list_meetings", "get_meetings", "get_meeting_transcript",
            "get_meeting_attachment", "list_meeting_folders", "get_account_info",
        }),
    ),
    Tool(
        name="linear",
        title="Linear",
        url="https://mcp.linear.app/mcp",
        token="Linear API key (Settings > Security & access; a key with only the Read permission is enough for reads)",
        probe=("get_user", {"query": "me"}),
        reads=frozenset({
            "list_issues", "get_issue", "list_comments", "list_issue_statuses", "get_issue_status",
            "list_issue_labels", "list_projects", "get_project", "list_project_labels", "list_milestones",
            "get_milestone", "list_initiatives", "get_initiative", "list_initiative_labels",
            "get_status_updates", "list_documents", "get_document", "get_attachment", "extract_images",
            "list_cycles", "list_teams", "get_team", "list_users", "get_user", "get_workspace",
            "list_custom_views", "list_templates", "get_template", "get_triage_responsibility",
            "get_notifications", "list_releases", "get_release", "list_release_notes", "get_release_note",
            "list_release_pipelines", "list_diffs", "get_diff", "get_diff_threads", "list_agent_skills",
            "get_agent_skill", "search_documentation",
        }),
    ),
    Tool(
        name="notion",
        title="Notion",
        url="https://mcp.notion.com/mcp",
        token="Notion MCP token (Business and Enterprise plans: a workspace owner makes one in Notion's Developer portal)",
        probe=("notion-get-self", {}),
        reads=frozenset({
            "notion-search", "notion-fetch", "notion-get-self", "notion-get-users", "notion-get-teams",
            "notion-get-groups", "notion-get-comments", "notion-get-tool-access", "notion-get-async-task",
            "notion-list-private-pages", "notion-list-shared-pages", "notion-list-favorite-pages",
            "notion-list-recent-pages", "notion-list-page-backlinks", "notion-query-data-sources",
            "notion-query-multiple-data-sources", "notion-query-meeting-notes", "notion-memory-search",
            "notion-get-file-download-urls", "notion-download-attachment", "notion-get-enabled-skills",
            "notion-download-skill", "notion-search-agents", "notion-query-sessions", "notion-search-sessions",
            "notion-get-session-status", "notion-wait-session", "notion-list-session-events",
            "notion-read-session-event", "notion-calendar-list-calendars", "notion-calendar-list-events",
            "notion-calendar-list-coworkers-events", "notion-calendar-list-contacts",
            "notion-calendar-list-calendar-resources",
        }),
    ),
    Tool(
        name="gmail",
        title="Gmail",
        url="https://gmailmcp.googleapis.com/mcp/v1",
        client="yours",
        client_help=GMAIL_HELP,
        scopes=("https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose"),
        authorize={"access_type": "offline", "prompt": "consent"},  # Google gives a refresh token only then
        resource=False,
        probe=("list_labels", {}),
        reads=frozenset({"search_threads", "get_thread", "get_message", "list_labels", "list_drafts", "get_draft"}),
    ),
]}
