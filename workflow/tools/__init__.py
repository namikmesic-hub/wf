"""Tools: vendor MCP servers (Slack, Granola, Linear, Notion, Gmail) that wf attaches to agent sessions.

A node lists the tools its agent may use (`tools: [slack, notion]`), and gets only their read
operations unless it opts in to writes for one (`{name: slack, write: true}`). You sign in once
per machine with `wf tools <name> setup` and check it with `wf tools <name> doctor`. docs/tools.md
says which server each tool is and how it signs in.

Read the modules in this order:
    catalog.py   the five tools: server, sign-in, which operations only read, doctor's read call
    keychain.py  where sign-ins are kept: the macOS Keychain, through the system keyring
    signin.py    setup's OAuth flow or pasted token, and refreshing tokens that expire
    relay.py     attaching tools to a session: wf is the MCP client, and the agent calls through it
    doctor.py    wf tools doctor: is a tool ready?

Nothing is imported here: relay.py loads the MCP client, which only runs that use tools need.
"""
