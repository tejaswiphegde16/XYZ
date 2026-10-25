"""Evidence-based, bounded recovery for coding-agent test commands."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable


class AgentError(ValueError):
    """Raised when a Guardian request does not match its input contract."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
MAX_INSPECTED_PATHS = 500
TIMEOUT_SECONDS = 45


def validate_input(payload: Any) -> tuple[str, list[str], str, list[str], list[str]]:
    if not isinstance(payload, dict):
        raise AgentError("input must be a JSON object")
    task, command, cwd = payload.get("task"), payload.get("command"), payload.get("cwd", ".")
    expected_tests = payload.get("expected_tests", [])
    required_output = payload.get("required_output", [])
    if not isinstance(task, str) or not task.strip():
        raise AgentError("field 'task' must be a nonempty string")
    if (not isinstance(command, list) or not command
            or not all(isinstance(part, str) and part for part in command)):
        raise AgentError("field 'command' must be a nonempty list of command arguments")
    if not isinstance(cwd, str) or not cwd:
        raise AgentError("field 'cwd' must be a nonempty string")
    for field_name, value in (("expected_tests", expected_tests), ("required_output", required_output)):
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise AgentError(f"field '{field_name}' must be a list of nonempty strings")
    if not _is_test_command(command):
        raise AgentError("only Python unittest or pytest commands are allowed")
    return task.strip(), command, cwd, expected_tests, required_output


def _is_test_command(command: list[str]) -> bool:
    """Allow test runners only; argv is passed directly without a shell."""
    executable = Path(command[0]).name.lower()
    if executable not in {"python", "python3", "pytest", "pytest.exe"} and not executable.startswith("python"):
        return False
    if executable.startswith("python"):
        return len(command) >= 3 and command[1:3] == ["-m", "unittest"]
    return True


def _test_files(workspace: Path) -> list[Path]:
    ignored = {".git", ".venv", "venv", "__pycache__"}
    found = []
    for path in workspace.rglob("*.py"):
        if any(part in ignored for part in path.relative_to(workspace).parts):
            continue
        if path.name.startswith("test") or path.name.endswith("_test.py"):
            found.append(path)
    return sorted(found)


def _inspect_dependency_manifests(workspace: Path) -> list[str]:
    """Inspect only known top-level dependency declarations for import failures."""
    known = {"requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "environment.yml"}
    return sorted(path.name for path in workspace.iterdir() if path.is_file() and path.name in known)


def _inspect_test_files(workspace: Path) -> list[str]:
    """Inspect test-named Python files only when a test-path failure needs it."""
    return [path.relative_to(workspace).as_posix() for path in _test_files(workspace)][:MAX_INSPECTED_PATHS]


def _find_project_root(start: Path, workspace: Path,
                       test_files: list[Path] | None = None) -> Path | None:
    test_files = _test_files(workspace) if test_files is None else test_files
    for candidate in [start, *start.parents]:
        if candidate == workspace.parent:
            break
        if candidate.is_relative_to(workspace) and any(
                p.parent.name == "tests" and p.parent.parent == candidate for p in test_files):
            return candidate
    roots = {p.parent.parent for p in test_files if p.parent.name == "tests"}
    return next(iter(roots)) if len(roots) == 1 else None


def _run(command: list[str], cwd: Path, runner: Runner) -> subprocess.CompletedProcess[str]:
    try:
        return runner(command, cwd=str(cwd), capture_output=True, text=True,
                       timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 124, "", str(exc))


def _verify(result: subprocess.CompletedProcess[str], expected_tests: list[str],
            required_output: list[str]) -> dict[str, Any]:
    """A passing exit is insufficient unless output confirms tests ran and passed."""
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        return {"status": "failed", "criteria": {"exit_code_zero": False,
                "tests_observed": None, "tests_passed": False,
                "expected_tests_present": None, "required_output_present": None},
                "evidence": result.stderr or result.stdout or f"exit code {result.returncode}"}

    expected_present = all(test_name in output for test_name in expected_tests)
    output_present = all(required in output for required in required_output)
    if not expected_present or not output_present:
        missing = []
        if not expected_present:
            missing.append("expected test output is missing")
        if not output_present:
            missing.append("required output is missing")
        return {"status": "inconclusive", "criteria": {"exit_code_zero": True,
                "tests_observed": None, "tests_passed": None,
                "expected_tests_present": expected_present,
                "required_output_present": output_present},
                "evidence": "; ".join(missing) + ". Command output: " + (output.strip() or "<empty>")}

    unittest_match = re.search(r"Ran\s+(\d+)\s+tests?", output, re.IGNORECASE)
    pytest_passed = re.search(r"(?:^|\s)(\d+)\s+passed(?:\s|,|$)", output, re.IGNORECASE)
    if unittest_match:
        count = int(unittest_match.group(1))
        if re.search(r"\b(FAIL|FAILED|ERROR)\b", output, re.IGNORECASE):
            return {"status": "failed", "criteria": {"exit_code_zero": True,
                    "tests_observed": count > 0, "tests_passed": False,
                    "expected_tests_present": expected_present,
                    "required_output_present": output_present}, "evidence": output.strip()}
        passed = count > 0 and bool(re.search(r"\bOK\b", output))
        return {"status": "passed" if passed else "inconclusive",
                "criteria": {"exit_code_zero": True, "tests_observed": count > 0,
                             "tests_passed": passed, "expected_tests_present": expected_present,
                             "required_output_present": output_present}, "evidence": output.strip()}
    if pytest_passed:
        count = int(pytest_passed.group(1))
        return {"status": "passed" if count > 0 else "inconclusive",
                "criteria": {"exit_code_zero": True, "tests_observed": count > 0,
                             "tests_passed": count > 0, "expected_tests_present": expected_present,
                             "required_output_present": output_present}, "evidence": output.strip()}
    if re.search(r"\b(FAIL|FAILED|ERROR)\b", output, re.IGNORECASE):
        return {"status": "failed", "criteria": {"exit_code_zero": True,
                "tests_observed": True, "tests_passed": False,
                "expected_tests_present": expected_present,
                "required_output_present": output_present}, "evidence": output.strip()}
    return {"status": "inconclusive", "criteria": {"exit_code_zero": True,
            "tests_observed": None, "tests_passed": None,
            "expected_tests_present": expected_present,
            "required_output_present": output_present},
            "evidence": output.strip() or "Exit code was zero but no test-run/pass summary was present."}


def _classify_failure(result: subprocess.CompletedProcess[str], cwd: Path,
                      workspace: Path, command: list[str]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    output = f"{result.stdout}\n{result.stderr}".strip()
    lower = output.lower()

    if any(marker in lower for marker in ("permission denied", "permissionerror", "operation not permitted")):
        return ({"category": "permission_error", "evidence": output,
                 "reason": "The tool reported a permissions failure; changing permissions is not authorized."}, None)

    module_match = re.search(r"(?:ModuleNotFoundError|ImportError):\s*No module named ['\"]?([^'\"\s]+)", output)
    if module_match:
        manifests = _inspect_dependency_manifests(workspace)
        diagnosis = {"category": "missing_dependency", "module": module_match.group(1),
                     "evidence": output,
                     "reason": "The interpreter cannot import the named module. Guardian inspected only top-level dependency manifests and will not install packages.",
                     "dependency_manifests": manifests}
        inspection = {"action": "inspect_dependency_manifests", "observed_paths": manifests}
        return diagnosis, inspection

    if any(marker in lower for marker in ("assertionerror", "assertion error", " failed", "failures:", "\nfail:")):
        return ({"category": "test_assertion_failure", "evidence": output,
                 "reason": "The test code ran and reported a failing assertion; changing source or expected values is unsafe."}, None)

    missing_markers = ("start directory", "no tests found", "no such file or directory", "not found",
                       "file not found", "filenotfounderror")
    if any(marker in lower for marker in missing_markers):
        test_files = _inspect_test_files(workspace)
        tests_missing = (any(marker in lower for marker in ("start directory", "no tests found"))
                         or ("tests" in lower and any(marker in lower for marker in
                             ("no such file", "not found", "does not exist"))))
        project_root = _find_project_root(cwd, workspace, [workspace / p for p in test_files])
        inspection = {"action": "inspect_test_files", "observed_paths": test_files}
        if project_root is not None and project_root != cwd and test_files and tests_missing:
            return ({"category": "wrong_working_directory", "evidence": output,
                     "project_root": str(project_root),
                     "reason": f"The command reports test discovery failure; targeted test-file inspection found {len(test_files)} test file(s) at the project root."}, inspection)

        missing_match = (re.search(r"(?:no such file or directory|file not found|filenotfounderror)[^\n]*?['\"]([^'\"]+)['\"]", output, re.IGNORECASE)
                         or re.search(r"(?:file or directory not found|file not found):?\s*([^\s]+)", output, re.IGNORECASE))
        missing_path = missing_match.group(1) if missing_match else None
        return ({"category": "missing_test_file", "missing_path": missing_path,
                 "evidence": output,
                 "reason": "A requested test path is absent. Guardian inspected test-named Python files before deciding whether a replacement is safe.",
                 "discovered_test_files": test_files}, inspection)

    return ({"category": "unknown_error", "evidence": output or f"exit code {result.returncode}",
             "reason": "Available command output does not identify a safe correction."}, None)


def _missing_target_recovery(command: list[str], discovered_files: list[str]) -> list[str] | None:
    """Replace explicit missing pytest file arguments only with discovered test files."""
    if Path(command[0]).name.lower() not in {"pytest", "pytest.exe"}:
        return None
    if not discovered_files:
        return None
    explicit_targets = [arg for arg in command[1:] if arg.endswith(".py") or ".py::" in arg]
    if not explicit_targets:
        return None
    existing = []
    for arg in command[1:]:
        if arg in explicit_targets:
            continue
        existing.append(arg)
    # A deterministic discovered file is safer than guessing a nonexistent target.
    return [command[0], *existing, *discovered_files]


def _failure_signature(result: subprocess.CompletedProcess[str]) -> tuple[int, str]:
    return result.returncode, re.sub(r"\s+", " ", f"{result.stdout}\n{result.stderr}").strip()


def run_agent(payload: Any, *, workspace: str | Path | None = None,
              runner: Runner = subprocess.run,
              max_recovery_attempts: int = 1) -> dict[str, Any]:
    if (not isinstance(max_recovery_attempts, int) or isinstance(max_recovery_attempts, bool)
            or max_recovery_attempts < 0):
        raise ValueError("max_recovery_attempts must be a nonnegative integer")
    task, command, requested_cwd, expected_tests, required_output = validate_input(payload)
    root = Path(workspace or Path.cwd()).resolve()
    current = (root / requested_cwd).resolve()
    if not current.is_relative_to(root):
        raise AgentError("cwd must stay inside the workspace")

    attempts: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    recovery_count = 0
    previous_failure: subprocess.CompletedProcess[str] | None = None
    diagnosis: dict[str, Any] | None = None

    while True:
        result = _run(command, current, runner)
        verification = _verify(result, expected_tests, required_output)
        attempts.append({"command": command, "cwd": str(current), "returncode": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr,
                         "verification": verification})
        trace.append({"type": "tool_call", "action": "run_command", "command": command,
                      "cwd": str(current), "returncode": result.returncode,
                      "stdout": result.stdout, "stderr": result.stderr})

        if verification["status"] == "passed":
            trace.append({"type": "verification", **verification})
            return {"ok": True, "task": task, "status": "verified", "attempts": attempts,
                    "diagnosis": diagnosis, "trace": trace, "verification": verification,
                    "summary": "The requested tests passed; Guardian stopped."}

        if verification["status"] == "inconclusive":
            diagnosis = {"category": "inconclusive_verification", "evidence": verification["evidence"],
                         "reason": "The process exited zero, but its output did not establish that the requested tests passed."}
            trace.append({"type": "diagnosis", **diagnosis})
            trace.append({"type": "action", "action": "stop", "reason": "No recovery action is supported by the inconclusive result."})
            trace.append({"type": "verification", **verification})
            return {"ok": True, "task": task, "status": "failed", "attempts": attempts,
                    "diagnosis": diagnosis, "trace": trace, "verification": verification,
                    "summary": "Verification is inconclusive; Guardian stopped without claiming success."}

        if previous_failure is not None and _failure_signature(result) == _failure_signature(previous_failure):
            diagnosis = {"category": "repeated_failure", "evidence": result.stderr or result.stdout,
                         "reason": "The same command failure repeated after a recovery action."}
            trace.append({"type": "diagnosis", **diagnosis})
            trace.append({"type": "action", "action": "stop", "reason": "Repeated failure detected; no further command was issued."})
            trace.append({"type": "verification", "status": "failed", "criteria": verification["criteria"],
                          "evidence": verification["evidence"]})
            return {"ok": True, "task": task, "status": "failed", "attempts": attempts,
                    "diagnosis": diagnosis, "trace": trace,
                    "verification": {**verification, "status": "failed"},
                    "summary": "Stopped after the same failure repeated; success was not verified."}

        previous_failure = result
        diagnosis, inspection = _classify_failure(result, current, root, command)
        if inspection is not None:
            trace.append({"type": "tool_call", "root": str(root), **inspection})
        trace.append({"type": "diagnosis", **diagnosis})

        next_command, next_cwd, action_name, action_reason = command, current, "", ""
        if diagnosis["category"] == "wrong_working_directory":
            project_root = Path(diagnosis["project_root"])
            if project_root is not None and project_root != current:
                next_cwd, action_name = project_root, "rerun_from_project_root"
                action_reason = "Filesystem inspection located the requested test suite at the project root."
        elif diagnosis["category"] == "missing_test_file":
            replacement = _missing_target_recovery(command, diagnosis.get("discovered_test_files", []))
            if replacement is not None:
                next_command, action_name = replacement, "run_discovered_test_file"
                action_reason = "The requested file is absent; the replacement path exists in the inspected test-file listing."
        elif diagnosis["category"] == "missing_dependency":
            action_name = "report_dependency_blocker"
            action_reason = "Report the exact missing import and inspected manifests; do not install or modify the environment."

        can_retry = bool(action_name and action_name != "report_dependency_blocker"
                         and recovery_count < max_recovery_attempts)
        changed = next_command != command or next_cwd != current
        if can_retry and changed:
            recovery_count += 1
            trace.append({"type": "action", "action": action_name, "reason": action_reason,
                          "command": next_command, "cwd": str(next_cwd),
                          "environment_change": ("command arguments changed" if next_command != command
                                                  else "working directory changed"),
                          "attempt": recovery_count})
            command, current = next_command, next_cwd
            continue

        if action_name == "report_dependency_blocker":
            trace.append({"type": "action", "action": action_name, "reason": action_reason})
            diagnosis["blocker"] = (f"Missing dependency '{diagnosis['module']}'. Declare or provide it using the project's approved setup; Guardian did not install it.")
        elif action_name and not can_retry:
            diagnosis["category"] = "recovery_limit_reached"
            diagnosis["blocker"] = "The configured recovery-attempt limit was reached."
            trace.append({"type": "action", "action": "stop", "reason": diagnosis["blocker"]})
        else:
            diagnosis["blocker"] = diagnosis.get("reason", "No safe correction was established from the evidence.")
            trace.append({"type": "action", "action": "stop", "reason": diagnosis["blocker"]})

        final_verification = verification
        trace.append({"type": "verification", **final_verification})
        return {"ok": True, "task": task, "status": "failed", "attempts": attempts,
                "diagnosis": diagnosis, "trace": trace, "verification": final_verification,
                "summary": "Verification did not pass; Guardian stopped without claiming success."}
