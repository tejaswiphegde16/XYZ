"""Deterministic tests for diagnosis, bounded recovery, and verification."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent.core import AgentError, run_agent


def completed(code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, stderr)


class GuardianTests(unittest.TestCase):
    def make_project(self, root: Path) -> None:
        tests = root / "tests"
        tests.mkdir(parents=True, exist_ok=True)
        (tests / "test_example.py").write_text(
            "import unittest\nclass Example(unittest.TestCase):\n def test_ok(self): self.assertEqual(2, 1 + 1)\n",
            encoding="utf-8",
        )

    def test_wrong_directory_is_diagnosed_corrected_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            (root / "src").mkdir()
            runner = Mock(side_effect=[
                completed(2, stderr="ImportError: Start directory is not importable: 'tests'"),
                completed(0, stderr="Ran 1 test\nOK"),
            ])
            result = run_agent({"task": "Run tests", "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"], "cwd": "src"}, workspace=root, runner=runner)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["diagnosis"]["category"], "wrong_working_directory")
        self.assertEqual(Path(runner.call_args_list[0].kwargs["cwd"]), root / "src")
        self.assertEqual(Path(runner.call_args_list[1].kwargs["cwd"]), root)
        self.assertEqual(runner.call_count, 2)
        recovery_action = next(e for e in result["trace"] if e.get("action") == "rerun_from_project_root")
        self.assertEqual(recovery_action["environment_change"], "working directory changed")
        self.assertTrue(any(event.get("action") == "inspect_test_files" for event in result["trace"]))
        self.assertEqual(result["verification"]["status"], "passed")

    def test_locates_nested_project_root_from_workspace_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "projects" / "app"
            self.make_project(project)
            runner = Mock(side_effect=[
                completed(2, stderr="Start directory not found: tests"),
                completed(0, stderr="Ran 1 test\nOK"),
            ])
            result = run_agent({"task": "Run tests", "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"]}, workspace=root, runner=runner)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(Path(runner.call_args_list[1].kwargs["cwd"]), project)

    def test_missing_test_file_recovers_using_discovered_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            runner = Mock(side_effect=[
                completed(4, stderr="ERROR: file or directory not found: tests/missing.py"),
                completed(0, stdout="1 passed in 0.01s"),
            ])
            result = run_agent({"task": "Run tests", "command": ["pytest", "tests/missing.py"]}, workspace=root, runner=runner)
        self.assertEqual(result["diagnosis"]["category"], "missing_test_file")
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[1].args[0], ["pytest", "tests/test_example.py"])
        file_action = next(e for e in result["trace"] if e.get("action") == "run_discovered_test_file")
        self.assertEqual(file_action["environment_change"], "command arguments changed")
        self.assertEqual(result["status"], "verified")

    def test_missing_test_file_without_candidate_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = Mock(return_value=completed(4, stderr="ERROR: file or directory not found: tests/missing.py"))
            result = run_agent({"task": "Run tests", "command": ["pytest", "tests/missing.py"]}, workspace=root, runner=runner)
        self.assertEqual(result["diagnosis"]["category"], "missing_test_file")
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(result["verification"]["status"], "failed")

    def test_does_not_retry_identical_command_in_unchanged_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            command = ["pytest", "tests/test_example.py"]
            runner = Mock(return_value=completed(4, stderr="ERROR: file or directory not found: tests/test_example.py"))
            result = run_agent({"task": "Run tests", "command": command}, workspace=root, runner=runner)
        self.assertEqual(runner.call_count, 1)
        self.assertFalse(any(event.get("action") == "run_discovered_test_file" and
                             event.get("type") == "action" for event in result["trace"]))
        self.assertTrue(any(event.get("action") == "stop" for event in result["trace"]))

    def test_missing_dependency_is_named_and_never_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("requests\n", encoding="utf-8")
            runner = Mock(return_value=completed(1, stderr="ModuleNotFoundError: No module named 'example_dep'"))
            result = run_agent({"task": "Run tests", "command": [sys.executable, "-m", "unittest", "discover" ]}, workspace=root, runner=runner)
        self.assertEqual(result["diagnosis"]["category"], "missing_dependency")
        self.assertEqual(result["diagnosis"]["module"], "example_dep")
        self.assertIn("requirements.txt", result["diagnosis"]["dependency_manifests"])
        self.assertEqual(runner.call_count, 1)
        self.assertTrue(any(event.get("action") == "report_dependency_blocker" for event in result["trace"]))
        inspections = [event["action"] for event in result["trace"] if event.get("type") == "tool_call" and event.get("action") != "run_command"]
        self.assertEqual(inspections, ["inspect_dependency_manifests"])

    def test_assertion_failure_is_diagnosed_and_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            runner = Mock(return_value=completed(1, stdout="FAILED tests/test_example.py::test_value", stderr="AssertionError: expected 2, got 3"))
            result = run_agent({"task": "Run tests", "command": ["pytest"]}, workspace=root, runner=runner)
        self.assertEqual(result["diagnosis"]["category"], "test_assertion_failure")
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(sum(event.get("type") == "tool_call" for event in result["trace"]), 1)

    def test_permission_failure_is_diagnosed_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Mock(return_value=completed(1, stderr="PermissionError: [Errno 13] Permission denied"))
            result = run_agent({"task": "Run tests", "command": ["pytest"]}, workspace=tmp, runner=runner)
        self.assertEqual(result["diagnosis"]["category"], "permission_error")
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(sum(event.get("type") == "tool_call" for event in result["trace"]), 1)

    def test_unknown_error_is_explicit_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Mock(return_value=completed(1, stderr="runner crashed with an unrecognized condition"))
            result = run_agent({"task": "Run tests", "command": ["pytest"]}, workspace=tmp, runner=runner)
        self.assertEqual(result["diagnosis"]["category"], "unknown_error")
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(result["verification"]["status"], "failed")
        self.assertEqual(sum(event.get("type") == "tool_call" for event in result["trace"]), 1)

    def test_success_verifies_and_stops_after_one_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Mock(return_value=completed(0, stdout="2 passed in 0.02s"))
            result = run_agent({"task": "Run tests", "command": ["pytest"]}, workspace=tmp, runner=runner)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["verification"]["status"], "passed")
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(sum(e.get("action") == "run_command" for e in result["trace"]), 1)

    def test_zero_exit_without_test_evidence_is_inconclusive_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Mock(return_value=completed(0, stdout=""))
            result = run_agent({"task": "Run tests", "command": ["pytest"]}, workspace=tmp, runner=runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["verification"]["status"], "inconclusive")
        self.assertEqual(sum(event.get("type") == "tool_call" for event in result["trace"]), 1)

    def test_success_exit_with_reported_unittest_failure_is_not_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Mock(return_value=completed(0, stderr="Ran 1 test\nFAILED (failures=1)"))
            result = run_agent({"task": "Run tests", "command": [sys.executable, "-m", "unittest", "discover"]}, workspace=tmp, runner=runner)
        self.assertNotEqual(result["status"], "verified")
        self.assertEqual(result["verification"]["status"], "failed")

    def test_missing_expected_test_or_required_output_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Mock(return_value=completed(0, stdout="1 passed in 0.02s"))
            result = run_agent({"task": "Run required checks", "command": ["pytest"],
                                "expected_tests": ["test_required_case"],
                                "required_output": ["REPORT_WRITTEN"]}, workspace=tmp, runner=runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["verification"]["status"], "inconclusive")
        self.assertFalse(result["verification"]["criteria"]["expected_tests_present"])
        self.assertFalse(result["verification"]["criteria"]["required_output_present"])

    def test_identical_repeated_failure_stops_and_respects_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            (root / "src").mkdir()
            repeat = completed(2, stderr="ImportError: Start directory is not importable: 'tests'")
            runner = Mock(side_effect=[repeat, repeat])
            result = run_agent({"task": "Run tests", "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"], "cwd": "src"}, workspace=root, runner=runner, max_recovery_attempts=3)
        self.assertEqual(result["diagnosis"]["category"], "repeated_failure")
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(len(result["attempts"]), 2)

    def test_zero_recovery_limit_prevents_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            (root / "src").mkdir()
            runner = Mock(return_value=completed(2, stderr="Start directory not found: tests"))
            result = run_agent({"task": "Run tests", "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"], "cwd": "src"}, workspace=root, runner=runner, max_recovery_attempts=0)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(result["diagnosis"]["category"], "recovery_limit_reached")

    def test_rejects_arbitrary_commands_and_workspace_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AgentError):
                run_agent({"task": "x", "command": ["rm", "-rf", "."]}, workspace=tmp)
            with self.assertRaises(AgentError):
                run_agent({"task": "x", "command": ["pytest"], "cwd": "../outside"}, workspace=tmp)

    def test_rejects_malformed_input_and_invalid_limit(self):
        for payload in (None, {}, {"task": "x", "command": "pytest"}):
            with self.subTest(payload=payload), self.assertRaises(AgentError):
                run_agent(payload)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            run_agent({"task": "x", "command": ["pytest"]}, workspace=tmp, max_recovery_attempts=-1)


if __name__ == "__main__":
    unittest.main()
