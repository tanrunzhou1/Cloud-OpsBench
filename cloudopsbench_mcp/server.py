from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, Type

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from pydantic import BaseModel, Field, ValidationError

from agents.cloudops_agent.tools.cloudops import (
    build_tool_registry,
    create_k8s_tools,
)

SERVER_NAME = "cloudopsbench"
SERVER_VERSION = "0.1.0"
TOOL_NAMES = (
    "GetResources",
    "DescribeResource",
    "GetAppYAML",
    "GetRecentLogs",
    "GetErrorLogs",
    "CheckServiceConnectivity",
    "GetServiceDependencies",
    "GetClusterConfiguration",
    "GetAlerts",
    "CheckNodeServiceStatus",
    "ListCodeFiles",
    "GetSourceCode",
)
CASE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class GetResourcesArguments(BaseModel):
    resource_type: str = Field(
        description="Resource type to list, such as pods, services, deployments, or nodes."
    )
    namespace: Optional[str] = Field(default=None, description="Namespace for namespaced resources.")
    name: Optional[str] = Field(default=None, description="Optional exact resource name.")
    show_labels: Optional[bool] = Field(
        default=None, description="Show resource labels when supported by this case."
    )
    output_wide: Optional[bool] = Field(
        default=None, description="Show wide resource columns when supported by this case."
    )
    label_selector: Optional[str] = Field(
        default=None, description="Simple key=value selector where supported by this case."
    )
    output_yaml: Optional[bool] = Field(
        default=None, description="Return supported resource YAML when available for this case."
    )


class DescribeResourceArguments(BaseModel):
    resource_type: str = Field(description="Resource type to describe, such as pod, node, or service.")
    name: str = Field(description="Exact resource name.")
    namespace: Optional[str] = Field(default=None, description="Namespace for namespaced resources.")


class GetAppYAMLArguments(BaseModel):
    app_name: str = Field(description="Application service name in the selected case.")


class GetRecentLogsArguments(BaseModel):
    namespace: str = Field(description="Kubernetes namespace.")
    service_name: str = Field(description="Application service name in the selected case.")


class GetErrorLogsArguments(BaseModel):
    namespace: str = Field(description="Kubernetes namespace.")
    service_name: str = Field(description="Application service name in the selected case.")


class CheckServiceConnectivityArguments(BaseModel):
    service_name: str = Field(description="Target internal service name in the selected case.")
    port: int = Field(description="Target TCP port.")
    namespace: str = Field(description="Kubernetes namespace.")


class GetServiceDependenciesArguments(BaseModel):
    service_name: str = Field(description="Application service name in the selected case.")


class NoArguments(BaseModel):
    pass


class CheckNodeServiceStatusArguments(BaseModel):
    node_name: Literal["master", "worker-01", "worker-02", "worker-03"] = Field(
        description="Target node name."
    )
    service_name: Literal["kube-scheduler", "kubelet", "kube-proxy", "containerd"] = Field(
        description="Infrastructure component name."
    )


class ListCodeFilesArguments(BaseModel):
    app_name: str = Field(description="Boutique application service name.")


class GetSourceCodeArguments(BaseModel):
    app_name: str = Field(description="Boutique application service name.")
    file_path: str = Field(
        description="Exact service-relative file path returned by ListCodeFiles."
    )


@dataclass(frozen=True)
class ToolDefinition:
    description: str
    arguments: Type[BaseModel]


TOOL_DEFINITIONS = {
    "GetResources": ToolDefinition(
        "Lists resources and current status from the selected case. The accepted optional filters depend on the case backend.",
        GetResourcesArguments,
    ),
    "DescribeResource": ToolDefinition(
        "Retrieves events, conditions, and detailed runtime state for one resource in the selected case.",
        DescribeResourceArguments,
    ),
    "GetAppYAML": ToolDefinition(
        "Returns the YAML configuration of an application service in the selected case.",
        GetAppYAMLArguments,
    ),
    "GetRecentLogs": ToolDefinition(
        "Returns recent raw logs for an application service in the selected case.",
        GetRecentLogsArguments,
    ),
    "GetErrorLogs": ToolDefinition(
        "Returns grouped error-log summaries for a service in the selected case.",
        GetErrorLogsArguments,
    ),
    "CheckServiceConnectivity": ToolDefinition(
        "Checks in-cluster TCP connectivity to a target service port in the selected case.",
        CheckServiceConnectivityArguments,
    ),
    "GetServiceDependencies": ToolDefinition(
        "Returns upstream and downstream service dependencies in the selected case.",
        GetServiceDependenciesArguments,
    ),
    "GetClusterConfiguration": ToolDefinition(
        "Returns cluster-wide node and configuration state for the selected case.",
        NoArguments,
    ),
    "GetAlerts": ToolDefinition(
        "Retrieves alert signals and abnormal metric summaries for the selected case.",
        NoArguments,
    ),
    "CheckNodeServiceStatus": ToolDefinition(
        "Checks infrastructure component status on a node in the selected case.",
        CheckNodeServiceStatusArguments,
    ),
    "ListCodeFiles": ToolDefinition(
        "Lists source files for an application service in cases that include Boutique source code.",
        ListCodeFilesArguments,
    ),
    "GetSourceCode": ToolDefinition(
        "Returns a source file selected from ListCodeFiles in cases that include Boutique source code.",
        GetSourceCodeArguments,
    ),
}


@dataclass(frozen=True)
class ServerSettings:
    """Trusted service-wide configuration; individual cases are selected per tool call."""

    dataset_root: Path
    trace_path: Path
    run_id: str
    host: str = "127.0.0.1"
    port: int = 8000
    max_tool_calls: int | None = None

    @classmethod
    def from_env(cls) -> ServerSettings:
        dataset_root = os.environ.get("CLOUDOPSBENCH_DATASET_ROOT", "").strip()
        if not dataset_root:
            raise ValueError("CLOUDOPSBENCH_DATASET_ROOT is required")
        resolved_dataset_root = Path(dataset_root).expanduser().resolve()
        if not resolved_dataset_root.is_dir():
            raise ValueError("CLOUDOPSBENCH_DATASET_ROOT must point to the benchmark dataset directory")

        call_limit = os.environ.get("CLOUDOPSBENCH_MAX_TOOL_CALLS", "").strip()
        max_tool_calls = int(call_limit) if call_limit else None
        if max_tool_calls is not None and max_tool_calls < 1:
            raise ValueError("CLOUDOPSBENCH_MAX_TOOL_CALLS must be positive")

        return cls(
            dataset_root=resolved_dataset_root,
            trace_path=Path(
                os.environ.get(
                    "CLOUDOPSBENCH_TRACE_PATH",
                    "/tmp/cloudopsbench-mcp-trajectory.jsonl",
                )
            ).expanduser(),
            run_id=os.environ.get("CLOUDOPSBENCH_RUN_ID", "").strip()
            or str(uuid.uuid4()),
            host=os.environ.get("CLOUDOPSBENCH_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("CLOUDOPSBENCH_MCP_PORT", "8000")),
            max_tool_calls=max_tool_calls,
        )


@dataclass(frozen=True)
class CaseSelection:
    case_id: str
    case_path: Path
    system: str
    fault_category: str


class TrajectoryRecorder:
    """Append-only JSONL audit log for MCP diagnostic tool calls."""

    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self._sequence = 0
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        output: str,
        is_error: bool,
        duration_ms: float,
        run_id: str | None = None,
        case_id: str | None = None,
    ) -> None:
        with self._lock:
            self._sequence += 1
            event = {
                "run_id": run_id or self.run_id,
                "case_id": case_id,
                "sequence": self._sequence,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tool_name": tool_name,
                "arguments": arguments,
                "output": output,
                "is_error": is_error,
                "duration_ms": round(duration_ms, 3),
            }
            with self.path.open("a", encoding="utf-8") as trace_file:
                trace_file.write(json.dumps(event, ensure_ascii=False) + "\n")


class CloudOpsMCP:
    """MCP adapter exposing one stable 12-tool catalog over multiple cases."""

    def __init__(self, settings: ServerSettings):
        self.settings = settings
        self._admitted_calls = 0
        self._budget_lock = threading.Lock()
        self._registry_lock = threading.Lock()
        self._case_registries: dict[str, tuple[CaseSelection, dict[str, Any]]] = {}
        self.recorder = TrajectoryRecorder(settings.trace_path, settings.run_id)
        self.server = Server(
            SERVER_NAME,
            version=SERVER_VERSION,
            instructions=(
                "Diagnose CloudOpsBench cases using these read-only tools. Every tool call "
                "must include the case_id selected by the benchmark runner. Use the same "
                "case_id throughout one diagnosis. Do not request or provide a filesystem path. "
                "All twelve tools are listed for every case; source-code tools return an explicit "
                "tool error when the selected case has no Boutique source-code snapshot."
            ),
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )

    async def _list_tools(self, _ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[self._to_mcp_tool(name, TOOL_DEFINITIONS[name]) for name in TOOL_NAMES]
        )

    async def _call_tool(
        self, _ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        # Reserve slots before backend execution so parallel requests cannot
        # exceed the optional process-wide limit.
        with self._budget_lock:
            if (
                self.settings.max_tool_calls is not None
                and self._admitted_calls >= self.settings.max_tool_calls
            ):
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text="Run tool-call limit reached.")],
                    isError=True,
                )
            self._admitted_calls += 1
        started = time.perf_counter()
        raw_arguments = dict(params.arguments or {})
        case_id = raw_arguments.get("case_id")
        run_id = raw_arguments.get("run_id")
        effective_run_id = self.settings.run_id

        if params.name not in TOOL_DEFINITIONS:
            return self._error_result(
                params.name,
                raw_arguments,
                f"Unknown diagnostic tool: {params.name}",
                started,
                run_id=effective_run_id,
                case_id=case_id if isinstance(case_id, str) else None,
            )

        arguments = dict(raw_arguments)
        arguments.pop("case_id", None)
        arguments.pop("run_id", None)
        try:
            if not isinstance(case_id, str) or not case_id.strip():
                raise ValueError("case_id is required and must identify one benchmark case")
            case_id = case_id.strip()
            if run_id is not None:
                if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id.strip()):
                    raise ValueError("run_id must contain 1 to 128 safe identifier characters")
                effective_run_id = run_id.strip()

            definition = TOOL_DEFINITIONS[params.name]
            self._reject_unexpected_arguments(definition.arguments, arguments)
            public_model = definition.arguments.model_validate(arguments)
            public_arguments = public_model.model_dump(exclude_unset=True)
            selection, registry = self._registry_for_case(case_id)
            trace_arguments = {
                **public_arguments,
                "case_id": selection.case_id,
                "run_id": effective_run_id,
            }

            if params.name in {"ListCodeFiles", "GetSourceCode"} and selection.system != "boutique":
                return self._error_result(
                    params.name,
                    trace_arguments,
                    f"{params.name} is unavailable for TrainTicket case {selection.case_id}; this case has no Boutique source-code snapshot.",
                    started,
                    run_id=effective_run_id,
                    case_id=selection.case_id,
                )

            tool = registry.get(params.name)
            if tool is None:
                return self._error_result(
                    params.name,
                    trace_arguments,
                    f"{params.name} is unavailable for case {selection.case_id}.",
                    started,
                    run_id=effective_run_id,
                    case_id=selection.case_id,
                )

            validated_arguments = self._validate_arguments(tool, public_arguments)
            output = tool._run(**validated_arguments)
            output_text = "" if output is None else str(output)
            trace_arguments = {
                **validated_arguments,
                "case_id": selection.case_id,
                "run_id": effective_run_id,
            }
        except (TypeError, ValueError, ValidationError, FileNotFoundError) as exc:
            return self._error_result(
                params.name,
                raw_arguments,
                f"Invalid case or tool arguments: {exc}",
                started,
                run_id=effective_run_id,
                case_id=case_id if isinstance(case_id, str) else None,
            )
        # Keep backend exceptions at the MCP boundary as explicit tool errors.
        except Exception as exc:  # noqa: BLE001
            return self._error_result(
                params.name,
                raw_arguments,
                f"Tool execution failed: {exc}",
                started,
                run_id=effective_run_id,
                case_id=case_id if isinstance(case_id, str) else None,
            )

        duration_ms = (time.perf_counter() - started) * 1000
        self.recorder.record(
            tool_name=params.name,
            arguments=trace_arguments,
            output=output_text,
            is_error=False,
            duration_ms=duration_ms,
            run_id=effective_run_id,
            case_id=selection.case_id,
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=output_text)]
        )

    def _resolve_case(self, case_id: str) -> CaseSelection:
        parts = case_id.split("/")
        if len(parts) != 3 or any(not CASE_SEGMENT_PATTERN.fullmatch(part) for part in parts):
            raise ValueError("case_id must have the form <system>/<fault-category>/<case-id>")

        system_directory, fault_category, case_name = parts
        if system_directory not in {"boutique", "trainticket"}:
            raise ValueError("case_id system must be boutique or trainticket")
        backend_system = "train-ticket" if system_directory == "trainticket" else "boutique"
        dataset_root = self.settings.dataset_root.resolve()
        case_path = (dataset_root / system_directory / fault_category / case_name).resolve()
        try:
            case_path.relative_to(dataset_root)
        except ValueError as exc:
            raise ValueError("case_id resolves outside CLOUDOPSBENCH_DATASET_ROOT") from exc
        if not case_path.is_dir():
            raise FileNotFoundError(f"Benchmark case does not exist: {case_id}")
        if not (case_path / "tool_cache.json").is_file():
            raise FileNotFoundError(f"Benchmark case has no diagnostic tool cache: {case_id}")

        return CaseSelection(
            case_id=case_id,
            case_path=case_path,
            system=backend_system,
            fault_category=fault_category,
        )

    def _registry_for_case(self, case_id: str) -> tuple[CaseSelection, dict[str, Any]]:
        selection = self._resolve_case(case_id)
        with self._registry_lock:
            cached = self._case_registries.get(selection.case_id)
            if cached is not None:
                return cached
            tools = create_k8s_tools(
                str(selection.case_path),
                system=selection.system,
                fault_category=selection.fault_category,
            )
            registry = build_tool_registry(tools)
            expected_tools = set(TOOL_NAMES if selection.system == "boutique" else TOOL_NAMES[:10])
            if set(registry) != expected_tools:
                raise RuntimeError(
                    f"Unexpected diagnostic tools for case {selection.case_id}: "
                    f"expected {sorted(expected_tools)}, got {sorted(registry)}"
                )
            result = (selection, registry)
            self._case_registries[selection.case_id] = result
            return result

    def _error_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        message: str,
        started: float,
        *,
        run_id: str | None = None,
        case_id: str | None = None,
    ) -> types.CallToolResult:
        self.recorder.record(
            tool_name=tool_name,
            arguments=arguments,
            output=message,
            is_error=True,
            duration_ms=(time.perf_counter() - started) * 1000,
            run_id=run_id,
            case_id=case_id,
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=message)],
            isError=True,
        )

    @staticmethod
    def _reject_unexpected_arguments(schema: Type[BaseModel], arguments: dict[str, Any]) -> None:
        allowed = set(schema.model_fields)
        unexpected = sorted(set(arguments) - allowed)
        if unexpected:
            raise ValueError(f"unexpected argument(s): {', '.join(unexpected)}")

    @staticmethod
    def _validate_arguments(tool: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        schema = getattr(tool, "args_schema", None)
        if schema is None:
            if arguments:
                raise ValueError("this tool takes no arguments")
            return {}

        allowed = set(schema.model_fields)
        unexpected = sorted(set(arguments) - allowed)
        unsupported_values = [
            argument for argument in unexpected
            if arguments[argument] not in (None, False)
        ]
        if unsupported_values:
            raise ValueError(
                "arguments are not supported by this case: "
                + ", ".join(sorted(unsupported_values))
            )
        compatible_arguments = {
            key: value for key, value in arguments.items() if key in allowed
        }
        return schema.model_validate(compatible_arguments).model_dump()

    @staticmethod
    def _to_mcp_tool(name: str, definition: ToolDefinition) -> types.Tool:
        input_schema = definition.arguments.model_json_schema()
        properties = input_schema.setdefault("properties", {})
        properties["case_id"] = {
            "type": "string",
            "description": "Required benchmark case ID, for example boutique/runtime/1 or trainticket/runtime/1.",
        }
        properties["run_id"] = {
            "type": "string",
            "description": "Optional caller run ID used to correlate trace entries; defaults to the server run ID.",
        }
        required = input_schema.setdefault("required", [])
        if "case_id" not in required:
            required.append("case_id")
        input_schema["additionalProperties"] = False
        return types.Tool(
            name=name,
            description=definition.description,
            inputSchema=input_schema,
            annotations=types.ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )

    def http_app(self):
        return self.server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
            host=self.settings.host,
        )


def main() -> None:
    settings = ServerSettings.from_env()
    adapter = CloudOpsMCP(settings)
    uvicorn.run(adapter.http_app(), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
