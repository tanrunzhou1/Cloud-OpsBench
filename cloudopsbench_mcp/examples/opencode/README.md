# OpenCode connection example

Start either system's server using the [MCP README](../../README.md).
Configure the task model and authentication in OpenCode as usual.

Merge [opencode.json](opencode.json) into the configuration of a separate agent
workspace, outside the benchmark checkout. If the workspace has no configuration,
copy the example there as `opencode.json`. It uses the OpenCode 1.x configuration
format used with v1.18.31. Change the URL if using another port.

From that workspace, check the connection:

```bash
opencode mcp list
```

Launch an OpenCode session and supply the incident query, namespace, and case
ID (such as `case_id: boutique/runtime/1` or `case_id: trainticket/runtime/1`).
The same configuration and running server support both systems. Keep the case
ID consistent throughout one diagnosis; it selects the snapshot for each
tool call.

This example registers the diagnostic interface only. It does not configure
Skills, a model provider, a diagnostic budget, or a batch evaluator.

Reference: [OpenCode MCP documentation](https://opencode.ai/docs/mcp-servers/).
