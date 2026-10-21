from __future__ import annotations

import asyncio
import json
import shutil
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import uvicorn
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from cloudopsbench_mcp.server import CloudOpsMCP, ServerSettings

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TOOLS = [
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
]
CASE_IDS = ("boutique/runtime/1", "trainticket/service/25")


def benchmark_case(case_id: str) -> Path:
    return REPO_ROOT / "benchmark" / case_id


class CloudOpsMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.trace_path = Path(self.temp_dir.name) / "trajectory.jsonl"
        self.settings = ServerSettings(
            dataset_root=REPO_ROOT / "benchmark",
            trace_path=self.trace_path,
            run_id="test-server-session",
        )
        self.adapter = CloudOpsMCP(self.settings)

    async def test_exposes_same_twelve_tools_for_both_systems(self) -> None:
        async with Client(self.adapter.server) as client:
            result = await client.list_tools()

        self.assertEqual([tool.name for tool in result.tools], EXPECTED_TOOLS)
        for tool in result.tools:
            properties = tool.input_schema.get("properties", {})
            self.assertIn("case_id", properties)
            self.assertIn("run_id", properties)
            self.assertIn("case_id", tool.input_schema["required"])
            self.assertFalse(tool.input_schema["additionalProperties"])

    async def test_switches_case_per_tool_call_and_correlates_trace(self) -> None:
        expected = {
            case_id: str(self.adapter._registry_for_case(case_id)[1]["GetAlerts"]._run())
            for case_id in CASE_IDS
        }
        async with Client(self.adapter.server) as client:
            results = {
                case_id: await client.call_tool(
                    "GetAlerts", {"case_id": case_id, "run_id": f"run-{index}"}
                )
                for index, case_id in enumerate(CASE_IDS, start=1)
            }

        for case_id, result in results.items():
            self.assertFalse(result.is_error)
            self.assertEqual(result.content[0].text, expected[case_id])
        events = [json.loads(line) for line in self.trace_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["case_id"] for event in events], list(CASE_IDS))
        self.assertEqual([event["run_id"] for event in events], ["run-1", "run-2"])

    async def test_rejects_filesystem_paths_and_records_the_call(self) -> None:
        async with Client(self.adapter.server) as client:
            result = await client.call_tool(
                "GetAlerts",
                {"case_id": CASE_IDS[0], "case_path": "/tmp/other-case"},
            )

        self.assertTrue(result.is_error)
        event = json.loads(self.trace_path.read_text(encoding="utf-8").strip())
        self.assertEqual(event["case_id"], CASE_IDS[0])
        self.assertEqual(event["tool_name"], "GetAlerts")
        self.assertEqual(event["arguments"]["case_path"], "/tmp/other-case")
        self.assertTrue(event["is_error"])

    async def test_train_ticket_keeps_source_tools_visible_but_returns_errors(self) -> None:
        async with Client(self.adapter.server) as client:
            listed = await client.list_tools()
            code_result = await client.call_tool(
                "ListCodeFiles", {"case_id": CASE_IDS[1], "app_name": "ts-auth-service"}
            )
            alerts = await client.call_tool("GetAlerts", {"case_id": CASE_IDS[1]})

        self.assertEqual([tool.name for tool in listed.tools], EXPECTED_TOOLS)
        self.assertTrue(code_result.is_error)
        self.assertIn("unavailable for TrainTicket", code_result.content[0].text)
        self.assertFalse(alerts.is_error)

    async def test_catalog_schema_is_stable_across_case_types(self) -> None:
        catalogs = []
        for case_id in ("boutique/runtime/1", "boutique/codedefect/1", CASE_IDS[1]):
            # Tool discovery does not require selecting or loading a case.
            async with Client(self.adapter.server) as client:
                result = await client.list_tools()
            catalogs.append([(tool.name, tool.description, tool.input_schema) for tool in result.tools])

        self.assertEqual(catalogs[0], catalogs[1])
        self.assertEqual(catalogs[0], catalogs[2])

    async def test_resource_queries_use_selected_case_backend(self) -> None:
        async with Client(self.adapter.server) as client:
            for case_id, namespace in ((CASE_IDS[0], "boutique"), (CASE_IDS[1], "train-ticket")):
                registry = self.adapter._registry_for_case(case_id)[1]
                arguments = {"resource_type": "pods", "namespace": namespace}
                expected = registry["GetResources"]._run(**arguments)
                result = await client.call_tool(
                    "GetResources", {"case_id": case_id, **arguments}
                )
                self.assertFalse(result.is_error)
                self.assertEqual(result.content[0].text, expected)

    async def test_boutique_source_code_tools_match_existing_backend(self) -> None:
        case_id = "boutique/codedefect/1"
        registry = self.adapter._registry_for_case(case_id)[1]
        cache = json.loads((benchmark_case(case_id) / "tool_cache.json").read_text())
        listing_key = next(
            key for key, value in cache.items()
            if key.startswith("ListCodeFiles:")
            and isinstance(value, dict) and value.get("files")
        )
        arguments = json.loads(listing_key.split(":", 1)[1])
        app_name = arguments["app_name"]
        file_path = cache[listing_key]["files"][0]["path"]
        source_arguments = {"app_name": app_name, "file_path": file_path}
        expected = registry["GetSourceCode"]._run(**source_arguments)
        self.assertFalse(expected.startswith("Error:"))

        async with Client(self.adapter.server) as client:
            listing = await client.call_tool(
                "ListCodeFiles", {"case_id": case_id, "app_name": app_name}
            )
            source = await client.call_tool(
                "GetSourceCode", {"case_id": case_id, **source_arguments}
            )
        self.assertFalse(listing.is_error)
        self.assertFalse(source.is_error)
        self.assertEqual(json.loads(listing.content[0].text), cache[listing_key])
        self.assertEqual(source.content[0].text, expected)

    async def test_missing_case_id_and_required_tool_arguments_return_errors(self) -> None:
        async with Client(self.adapter.server) as client:
            missing_case = await client.call_tool("GetAlerts", {})
            missing_tool_argument = await client.call_tool(
                "GetResources", {"case_id": CASE_IDS[0]}
            )
        self.assertTrue(missing_case.is_error)
        self.assertTrue(missing_tool_argument.is_error)
        events = [json.loads(line) for line in self.trace_path.read_text().splitlines()]
        self.assertEqual([event["sequence"] for event in events], [1, 2])
        self.assertTrue(all(event["is_error"] for event in events))

    async def test_optional_limit_counts_invalid_requests_and_blocks_backend(self) -> None:
        adapter = CloudOpsMCP(replace(self.settings, max_tool_calls=2))
        async with Client(adapter.server) as client:
            first = await client.call_tool("GetAlerts", {"case_id": CASE_IDS[0]})
            invalid = await client.call_tool("GetResources", {"case_id": CASE_IDS[0]})
            blocked = await client.call_tool("GetAlerts", {"case_id": CASE_IDS[1]})
        self.assertFalse(first.is_error)
        self.assertTrue(invalid.is_error)
        self.assertTrue(blocked.is_error)
        self.assertIn("limit reached", blocked.content[0].text)
        events = [json.loads(line) for line in self.trace_path.read_text().splitlines()]
        self.assertEqual([event["sequence"] for event in events], [1, 2])
        self.assertEqual([event["tool_name"] for event in events], ["GetAlerts", "GetResources"])

    async def test_restricted_dataset_root_needs_no_metadata_or_labels(self) -> None:
        dataset_root = Path(self.temp_dir.name) / "dataset"
        view = dataset_root / "boutique" / "runtime" / "1"
        view.mkdir(parents=True)
        source = benchmark_case(CASE_IDS[0])
        shutil.copyfile(source / "tool_cache.json", view / "tool_cache.json")
        logs = source / "raw_data" / "logs.json"
        if logs.is_file():
            (view / "raw_data").mkdir()
            shutil.copyfile(logs, view / "raw_data" / "logs.json")
        adapter = CloudOpsMCP(replace(self.settings, dataset_root=dataset_root))
        expected = adapter._registry_for_case(CASE_IDS[0])[1]["GetAlerts"]._run()
        async with Client(adapter.server) as client:
            result = await client.call_tool("GetAlerts", {"case_id": CASE_IDS[0]})
        self.assertFalse((view / "metadata.json").exists())
        self.assertFalse(result.is_error)
        self.assertEqual(result.content[0].text, str(expected))

    async def test_streamable_http_switches_cases_without_restart(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            self.adapter.http_app(), host="127.0.0.1", port=port,
            log_level="critical", lifespan="on",
        ))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async def wait_ready():
                while not server.started:
                    if task.done():
                        await task
                        self.fail("HTTP server exited before becoming ready")
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_ready(), timeout=5)
            async with httpx2.AsyncClient(trust_env=False) as http_client:
                transport = streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp", http_client=http_client,
                )
                async with Client(transport) as client:
                    catalog = await client.list_tools()
                    results = [
                        await client.call_tool("GetAlerts", {"case_id": case_id})
                        for case_id in CASE_IDS
                    ]
            self.assertEqual([tool.name for tool in catalog.tools], EXPECTED_TOOLS)
            self.assertTrue(all(not result.is_error for result in results))
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=5)


if __name__ == "__main__":
    unittest.main()
