---
description: Connect an MCP client to the hosted Précis Finance MCP demo and explore read-only tools over synthetic finance data.
---

# Connect to the public demo

The public Précis Finance MCP demo is available at
`https://mcp.precis.finance/mcp`. It serves a synthetic finance model through
the same read-only MCP tools used by the self-hosted package. You do not need an
account, an API key, Docker, or a local copy of the repository.

This is a shared evaluation server. Every caller uses the same restricted demo
profile and dataset. It cannot access your warehouse, and you cannot load your
own data into it. Avoid putting private information in MCP tool arguments.
The endpoint is rate-limited and may be unavailable during maintenance.

## Connect with Claude Code

```sh
claude mcp add --transport http precis-demo https://mcp.precis.finance/mcp
```

No authorization header is needed. Once connected, ask:

- “What can the Précis demo do?” — discovers the available tools and model.
- “What metrics and scenarios are available?” — lists the demo catalogue.
- “Show the P&L for 2025 with comparatives.” — runs a financial statement.

## Connect with Claude.ai

In Claude, open **Customize → Connectors**, select **+ → Add custom
connector**, name it `Précis demo`, and enter
`https://mcp.precis.finance/mcp`. Leave the optional OAuth settings empty.
Select **Add**, then enable the connector for a conversation using
**+ → Connectors** in the chat box. On Team or Enterprise plans, an
organization owner must add the custom connector before members can enable
it. See [Claude's custom connector guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

## Connect with ChatGPT

Open **Settings → Security and login** and enable **Developer mode**. In
**ChatGPT Plugins**, select **+**, create a personal plugin named
`Précis demo`, and use `https://mcp.precis.finance/mcp` as its public
MCP connection URL. The demo needs no authentication. Install the plugin
from your personal plugins, start a **Work** chat, and select it with
`@` before asking a demo question. Developer mode availability depends on
your account or workspace settings. See the
[official OpenAI documentation](https://developers.openai.com/plugins/quickstart).

## Connect with GitHub Copilot in VS Code

Add this entry to your VS Code user MCP configuration or to a project's
`.vscode/mcp.json`:

```json
{
  "servers": {
    "precis-demo": {
      "type": "http",
      "url": "https://mcp.precis.finance/mcp"
    }
  }
}
```

Start the server from the configuration file if VS Code prompts you. Open
**Copilot Chat → Agent** and check that the Précis tools appear in the
tool picker. A Copilot Business or Enterprise organization may need to
enable its MCP policy. See
[GitHub's Copilot MCP guide](https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp-in-your-ide/extend-copilot-chat-with-mcp).

If you use the GitHub Copilot CLI, the equivalent command is
`copilot mcp add --transport http precis-demo https://mcp.precis.finance/mcp`
([Copilot CLI instructions](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers)).

## Connect with another MCP client

Choose a **remote Streamable HTTP** connection and enter
`https://mcp.precis.finance/mcp` as the server URL. Select no authentication
if the client asks. For clients that accept an MCP server configuration file,
the equivalent entry is:

```json
{
  "mcpServers": {
    "precis-demo": {
      "type": "http",
      "url": "https://mcp.precis.finance/mcp"
    }
  }
}
```

Client configuration formats vary. After connecting, check that the tool list
includes `precis_orientation`, `list_scenarios`, `list_kpis`, and
`run_statement`. The URL is an MCP endpoint, not a page to browse directly.

To evaluate Précis Finance MCP with your own model and data, follow the
[local quickstart](quickstart.md) or the
[multi-user deployment guide](../deployment/oauth-keycloak.md). Those
deployments use their own authentication and data stores.
