# Codex connection example

Start either system's server using the [MCP README](../../README.md).
Configure the task model and authentication in Codex as usual.

Register the already-running HTTP server:

```bash
codex mcp add cloudopsbench --url http://127.0.0.1:8000/mcp
codex mcp list
```

Alternatively, merge [config.toml](config.toml) into the Codex configuration
used for this run. Use one registration method; do not replace your entire
existing configuration with this snippet. Change the URL if using another port.

Launch a fresh Codex session from a separate agent workspace, outside the
benchmark checkout, and supply the incident query, namespace, and case ID
(such as `case_id: boutique/runtime/1` or `case_id: trainticket/runtime/1`).
The same configuration and running server support both systems. Keep the case
ID consistent throughout one diagnosis; it selects the snapshot for each
tool call.

This example registers the diagnostic interface only. It does not configure
Skills, a model provider, a diagnostic budget, or a batch evaluator. The
configuration follows the interface used with Codex CLI v0.139.0.

Reference: [Codex MCP documentation](https://developers.openai.com/codex/mcp/).
