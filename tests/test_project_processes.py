import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from assistente.__main__ import invoke_tool
from assistente.project_processes import ProjectProcessManager


class ProjectProcessManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temporary_directory.name)
        self.manager = ProjectProcessManager(self.project_root)

    def tearDown(self):
        self.manager.close()
        self.temporary_directory.cleanup()

    def wait_for_output(self, process_id: str, expected: str, timeout: float = 5) -> str:
        deadline = time.monotonic() + timeout
        output = ""
        while time.monotonic() < deadline:
            output += self.manager.read_output(process_id)
            if expected in output:
                return output
            time.sleep(0.02)
        self.fail(f"Output {expected!r} não recebido do processo {process_id}: {output}")

    def test_process_accepts_input_and_returns_output_incrementally(self):
        result = self.manager.start(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys; print('ready', flush=True); "
                "print('received:' + sys.stdin.readline().strip(), flush=True)",
            ]
        )
        process_id = result.splitlines()[0].split(": ", 1)[1]

        initial_output = self.wait_for_output(process_id, "ready")
        self.manager.send_input(process_id, "hello")
        output = self.wait_for_output(process_id, "received:hello")

        self.assertIn("ready", initial_output)
        self.assertIn("received:hello", output)
        self.assertEqual(self.manager.list_processes()[0]["status"], "finished")

    def test_output_buffer_is_bounded_and_reports_discarded_bytes(self):
        result = self.manager.start(
            [
                sys.executable,
                "-c",
                "print('x' * 100000)",
            ]
        )
        process_id = result.splitlines()[0].split(": ", 1)[1]
        deadline = time.monotonic() + 5
        while (
            time.monotonic() < deadline and self.manager.list_processes()[0]["status"] == "running"
        ):
            time.sleep(0.02)
        time.sleep(0.05)
        output = self.manager.read_output(process_id)

        self.assertIn("bytes antigos descartados", output)
        self.assertLess(len(output.encode("utf-8")), 70 * 1024)

    def test_rejects_paths_that_escape_project(self):
        with self.assertRaises(ValueError):
            self.manager.start([sys.executable, "-c", "pass"], cwd="../")
        external = self.project_root.parent / f"{self.project_root.name}-outside"
        external.mkdir()
        (self.project_root / "linked").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.manager.start([sys.executable, "-c", "pass"], cwd="linked")

    def test_stop_terminates_only_processes_owned_by_manager(self):
        result = self.manager.start([sys.executable, "-c", "import time; time.sleep(60)"])
        process_id = result.splitlines()[0].split(": ", 1)[1]
        managed = self.manager._get(process_id)

        self.manager.stop(process_id)

        self.assertIsNotNone(managed.process.poll())
        with self.assertRaises(KeyError):
            self.manager.stop("f" * 32)


class ProjectProcessApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_approved_process_lifecycle_runs_through_agent_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProjectProcessManager(directory)
            approve = AsyncMock(return_value=True)
            started = await invoke_tool(
                "terminal_start",
                {
                    "command": [
                        sys.executable,
                        "-u",
                        "-c",
                        "import time; print('service-ready', flush=True); time.sleep(60)",
                    ]
                },
                {},
                approve=approve,
                process_manager=manager,
            )
            process_id = started.splitlines()[0].split(": ", 1)[1]

            output = ""
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and "service-ready" not in output:
                output += await invoke_tool(
                    "terminal_read",
                    {"process_id": process_id},
                    {},
                    process_manager=manager,
                )
                if "service-ready" not in output:
                    await asyncio.sleep(0.02)

            stopped = await invoke_tool(
                "terminal_stop",
                {"process_id": process_id},
                {},
                approve=approve,
                process_manager=manager,
            )

            self.assertIn("service-ready", output)
            self.assertIn("encerrado", stopped)
            self.assertEqual(approve.await_count, 2)
            manager.close()

    async def test_process_changes_require_approval_but_list_and_read_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProjectProcessManager(directory)
            denied = AsyncMock(return_value=False)
            start_result = await invoke_tool(
                "terminal_start",
                {"command": [sys.executable, "-c", "import time; time.sleep(60)"]},
                {},
                approve=denied,
                process_manager=manager,
            )
            self.assertEqual(start_result, "Ação cancelada pelo usuário.")
            self.assertEqual(manager.list_processes(), [])

            started = manager.start([sys.executable, "-c", "import time; time.sleep(60)"])
            process_id = started.splitlines()[0].split(": ", 1)[1]
            send_result = await invoke_tool(
                "terminal_send",
                {"process_id": process_id, "text": "input"},
                {},
                approve=denied,
                process_manager=manager,
            )
            stop_result = await invoke_tool(
                "terminal_stop",
                {"process_id": process_id},
                {},
                approve=denied,
                process_manager=manager,
            )
            listed = await invoke_tool(
                "terminal_list",
                {},
                {},
                approve=AsyncMock(side_effect=AssertionError("list must not need approval")),
                process_manager=manager,
            )
            read = await invoke_tool(
                "terminal_read",
                {"process_id": process_id},
                {},
                approve=AsyncMock(side_effect=AssertionError("read must not need approval")),
                process_manager=manager,
            )

            self.assertEqual(send_result, "Ação cancelada pelo usuário.")
            self.assertEqual(stop_result, "Ação cancelada pelo usuário.")
            self.assertIn(process_id, listed)
            self.assertIn(process_id, read)
            self.assertIsNone(manager._get(process_id).process.poll())
            self.assertEqual(denied.await_count, 3)
            manager.close()


class RemoteTerminalApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_terminal_rejects_invalid_arguments_before_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProjectProcessManager(directory)
            approve = AsyncMock(side_effect=AssertionError("invalid requests need no approval"))

            with self.assertRaisesRegex(ValueError, "provedor"):
                await invoke_tool(
                    "remote_terminal_start",
                    {"provider": None},
                    {},
                    approve=approve,
                    process_manager=manager,
                )

            self.assertEqual(manager.list_processes(), [])
            approve.assert_not_awaited()
            manager.close()


if __name__ == "__main__":
    unittest.main()
