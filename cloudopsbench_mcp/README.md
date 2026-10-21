# Cloud-OpsBench MCP Server

Connect an MCP-compatible agent to Cloud-OpsBench incident snapshots without
using the bundled ReAct agent. The server exposes the existing diagnostic
backend through Streamable HTTP and serves both benchmark systems from one
process.

| System | Dataset directory | Tools exposed |
| --- | --- | ---: |
| Online Boutique | `benchmark/boutique/` | 12 |
| TrainTicket | `benchmark/trainticket/` | 12 |

Every tool call selects its case with a `case_id` argument such as
`boutique/runtime/1` or `trainticket/runtime/1`. The server resolves that ID
under the configured dataset root and loads the corresponding existing tool
backend. It does not read the case's `metadata.json`, process labels, or golden
trajectories. No LLM API key, model configuration, or live cluster is required.

The public tool catalog and schemas stay stable across cases. Boutique-only
source tools remain visible for TrainTicket so agents can be evaluated on
their tool choice; those calls return an explicit MCP tool error because
TrainTicket has no Boutique source-code snapshot. `GetResources` publishes a
union schema for the backend variants. Arguments unsupported by the selected
case return an explicit validation error.

## Installation

Use Python 3.10 or newer. Run these commands from the repository root:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r cloudopsbench_mcp/requirements.txt
```

## Start the multi-case server

From the repository root:

```bash
export CLOUDOPSBENCH_DATASET_ROOT="$PWD/benchmark"
export CLOUDOPSBENCH_RUN_ID="manual-mcp-session"
export CLOUDOPSBENCH_TRACE_PATH="$PWD/.runs/manual-mcp-session/mcp.jsonl"
export CLOUDOPSBENCH_MCP_HOST="127.0.0.1"
export CLOUDOPSBENCH_MCP_PORT="8000"
unset CLOUDOPSBENCH_MAX_TOOL_CALLS
.venv/bin/python -m cloudopsbench_mcp
```

Leave the process running at `http://127.0.0.1:8000/mcp`. Each tool call can
select a different case, so switching between Boutique and TrainTicket does
not require a server restart or a new port. Do not pass filesystem paths in
tool arguments; the server accepts only case IDs beneath
`CLOUDOPSBENCH_DATASET_ROOT`.

For an isolated evaluation, make the configured dataset root contain only the
case directories that the server is allowed to access. Each case directory
must include `tool_cache.json`; `raw_data/logs.json` and `code/` are read when
available. Keep `process-label/` and `golden-trajectory/` outside the server's
accessible directory tree.

## Connect an agent

Start the server first, then follow one of the minimal client examples:

- [Codex](examples/codex/README.md), with a `config.toml` snippet.
- [OpenCode](examples/opencode/README.md), with an `opencode.json` example.

The diagnosis request must include the case's `case_id` along with the query
and namespace. For example:

```text
case_id: boutique/runtime/1
Diagnose the reported incident in namespace boutique.
Incident report: <query from metadata.json>
Use the cloudopsbench MCP tools to collect evidence.
Identify the faulty component and root cause, and explain the supporting evidence.
```

For a TrainTicket case, use an ID such as `trainticket/runtime/1` and its
namespace. MCP clients that call tools directly must include the same
`case_id` in every tool call. A benchmark runner may inject the case ID and a
per-run `run_id` automatically. The case ID in the call selects the snapshot;
the trace stores both IDs to correlate tool calls.

This is a connection example, not a full batch-evaluation runner. The client
owns reasoning and final-answer generation; the server supplies tool results.
An evaluator must separately capture the client's final diagnosis. The MCP
JSONL trace alone is not the bundled agent's evaluation input format.

## Tool catalog

All cases expose these twelve tools:

```text
GetResources                 DescribeResource
GetAppYAML                   GetRecentLogs
GetErrorLogs                 CheckServiceConnectivity
GetServiceDependencies       GetClusterConfiguration
GetAlerts                    CheckNodeServiceStatus
ListCodeFiles                GetSourceCode
```

The schemas are stable across cases. `GetResources` accepts the union of the
current backend parameters; `label_selector` is supported by the Boutique
runtime schema, while `output_yaml` is supported by TrainTicket and Boutique
code-defect schemas. Passing a parameter unsupported by the selected case
returns a tool error rather than silently ignoring it. The source-code tools
work when the selected case is Boutique and return an explicit error for
TrainTicket.

## Server configuration

Configuration is supplied by the trusted launcher through environment
variables:

| Variable | Required / default | Meaning |
| --- | --- | --- |
| `CLOUDOPSBENCH_DATASET_ROOT` | Required | Dataset directory containing `boutique/` and/or `trainticket/` |
| `CLOUDOPSBENCH_MCP_HOST` | `127.0.0.1` | Bind address |
| `CLOUDOPSBENCH_MCP_PORT` | `8000` | HTTP port |
| `CLOUDOPSBENCH_RUN_ID` | Generated UUID | Default trace run identifier, overridden per call when provided |
| `CLOUDOPSBENCH_TRACE_PATH` | `/tmp/cloudopsbench-mcp-trajectory.jsonl` | Append-only JSONL trace path |
| `CLOUDOPSBENCH_MAX_TOOL_CALLS` | Unset: unlimited | Optional process-wide limit on admitted tool requests |

The accepted case ID format is `<system>/<fault-category>/<case-id>`, where
`system` is `boutique` or `trainticket`. The server resolves and checks the
resulting path against the configured dataset root before loading a backend.

By default, there is no tool-call limit. A configured limit applies to the
whole server process across all cases and evaluation runs. Requests beyond
the limit return an MCP tool error without executing the backend or adding a
trace entry.

## Tool-call traces

Each admitted request is appended to the trace with `run_id`, `case_id`,
`sequence`, UTC `timestamp`, `tool_name`, validated `arguments`, complete
`output`, `is_error`, and `duration_ms`. Invalid requests retain their original
arguments for diagnosis. Rejected over-budget requests are not appended. Use
a fresh trace file per server session when you need a clean log; reusing a
path appends to the existing log.

Native backend messages that are already returned as text remain text results.
Errors raised at the MCP boundary, including unsupported TrainTicket
source-code calls and invalid case/tool arguments, are returned with
`isError=true`.
