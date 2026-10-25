"""Paired baseline-emulation and Guardian evaluation on shared fixtures."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.core import _verify, run_agent


def _result_spec(code: int, out: str = "", err: str = "") -> tuple[int, str, str]:
    return code, out, err


def _runner_for(specs: tuple[tuple[int, str, str], ...]) -> Mock:
    used_specs = []

    def execute(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        spec = specs[len(used_specs)]
        used_specs.append(spec)
        return subprocess.CompletedProcess(list(command), *spec)

    runner = Mock(side_effect=execute)
    runner.configured_specs = specs
    runner.used_specs = used_specs
    return runner


def _legacy_success(proc: subprocess.CompletedProcess[str]) -> bool:
    """The prior implementation's permissive success rule, retained for emulation."""
    output = f"{proc.stdout}\n{proc.stderr}"
    return proc.returncode == 0 and "Ran 0 tests" not in output and "collected 0 items" not in output


def _shared_success(proc: subprocess.CompletedProcess[str], payload: dict) -> bool:
    """Common adjudication oracle applied to both policies' final command result."""
    verification = _verify(proc, payload.get("expected_tests", []), payload.get("required_output", []))
    return verification["status"] == "passed"


def _legacy_agent(payload: dict, root: Path, runner: Mock) -> dict:
    """Emulate the old logic and expose its commands, inspection, and retry action."""
    command = list(payload["command"])
    cwd = (root / payload.get("cwd", ".")).resolve()
    attempts = []
    actions = []
    inspections = 0

    first = runner(command, cwd=str(cwd), capture_output=True, text=True, timeout=45, check=False)
    attempts.append({"command": command, "cwd": str(cwd), "result": first})
    if _legacy_success(first):
        return {"input": copy.deepcopy(payload), "claimed_success": True,
                "attempts": attempts, "actions": actions,
                "inspection_count": inspections}

    output = f"{first.stdout}\n{first.stderr}".lower()
    wrong_directory_signal = "tests" in output and any(marker in output for marker in
        ("no such file", "not found", "could not find", "does not exist", "start directory"))
    # Old baseline inspected only root/tests, after every failed command.
    inspections += 1
    tests_dir = root / "tests"
    has_tests = tests_dir.is_dir() and any(tests_dir.glob("test*.py"))
    if cwd != root and has_tests and wrong_directory_signal:
        reason = "The old policy saw a test-discovery error and a tests directory at workspace root."
        actions.append({"command": command, "cwd": str(root), "reason": reason})
        second = runner(command, cwd=str(root), capture_output=True, text=True, timeout=45, check=False)
        attempts.append({"command": command, "cwd": str(root), "result": second})
        return {"input": copy.deepcopy(payload), "claimed_success": _legacy_success(second), "attempts": attempts,
                "actions": actions, "inspection_count": inspections}
    return {"input": copy.deepcopy(payload), "claimed_success": False,
            "attempts": attempts, "actions": actions,
            "inspection_count": inspections}


def _scenario(name: str, category: str, recoverability: str,
              files: dict[str, str], payload: dict,
              results: tuple[tuple[int, str, str], ...]) -> dict:
    expected_success = recoverability == "recoverable" or category == "successful_command"
    return {"name": name, "category": category, "recoverability": recoverability,
            "expected_success": expected_success, "files": files,
            "payload": payload, "results": results}


def scenarios() -> list[dict]:
    py = sys.executable
    root_tests = {"tests/test_real.py": "pass\n"}
    cases = [
        _scenario("wrong_dir_src", "wrong_working_directory", "recoverable", root_tests,
                  {"task": "run tests", "command": [py, "-m", "unittest", "discover", "-s", "tests"], "cwd": "src"},
                  (_result_spec(2, err="ImportError: Start directory is not importable: 'tests'"), _result_spec(0, err="Ran 1 test\nOK"))),
        _scenario("wrong_dir_app", "wrong_working_directory", "recoverable", root_tests,
                  {"task": "run tests", "command": [py, "-m", "unittest", "discover", "-s", "tests"], "cwd": "app"},
                  (_result_spec(2, err="ImportError: Start directory is not importable: 'tests'"), _result_spec(0, err="Ran 2 tests\nOK"))),
        _scenario("wrong_dir_nested_project", "wrong_working_directory", "recoverable",
                  {"projects/demo/tests/test_demo.py": "pass\n"},
                  {"task": "run tests", "command": [py, "-m", "unittest", "discover", "-s", "tests"], "cwd": "scratch"},
                  (_result_spec(2, err="Start directory not found: tests"), _result_spec(0, err="Ran 1 test\nOK"))),
        _scenario("missing_target_recovered_a", "missing_test_file", "recoverable", root_tests,
                  {"task": "run tests", "command": ["pytest", "tests/missing_a.py"]},
                  (_result_spec(4, err="ERROR: file or directory not found: tests/missing_a.py"), _result_spec(0, out="1 passed in 0.01s"))),
        _scenario("missing_target_recovered_b", "missing_test_file", "recoverable", root_tests,
                  {"task": "run tests", "command": ["pytest", "tests/missing_b.py"]},
                  (_result_spec(4, err="ERROR: file or directory not found: tests/missing_b.py"), _result_spec(0, out="2 passed in 0.02s"))),
        _scenario("missing_target_no_candidate", "missing_test_file", "unrecoverable", {},
                  {"task": "run tests", "command": ["pytest", "tests/missing.py"]},
                  (_result_spec(4, err="ERROR: file or directory not found: tests/missing.py"),)),
        _scenario("missing_target_replacement_fails", "missing_test_file", "unrecoverable", root_tests,
                  {"task": "run tests", "command": ["pytest", "tests/missing.py"]},
                  (_result_spec(4, err="ERROR: file or directory not found: tests/missing.py"),
                   _result_spec(1, out="FAILED tests/test_real.py::test_value", err="AssertionError: expected 1 got 2"))),
    ]

    for index, message in enumerate(("expected 1, got 2", "expected true", "value mismatch"), 1):
        cases.append(_scenario(f"assertion_{index}", "test_assertion_failure", "unrecoverable", root_tests,
            {"task": "run tests", "command": ["pytest"]},
            (_result_spec(1, out=f"FAILED tests/test_real.py::test_{index}", err=f"AssertionError: {message}"),)))
    for index, module in enumerate(("alpha_dep", "beta_dep", "gamma_dep"), 1):
        cases.append(_scenario(f"dependency_{index}", "missing_dependency", "unrecoverable",
            {"requirements.txt": "alpha\n", "tests/test_real.py": "pass\n"},
            {"task": "run tests", "command": [py, "-m", "unittest", "discover"]},
            (_result_spec(1, err=f"ModuleNotFoundError: No module named '{module}'"),)))
    for index, message in enumerate(("Permission denied", "PermissionError: [Errno 13]", "Operation not permitted"), 1):
        cases.append(_scenario(f"permission_{index}", "permission_error", "unrecoverable", {},
            {"task": "run tests", "command": ["pytest"]}, (_result_spec(1, err=message),)))

    cases.extend([
        _scenario("repeat_wrong_dir", "repeated_failure", "unrecoverable", root_tests,
                  {"task": "run tests", "command": [py, "-m", "unittest", "discover", "-s", "tests"], "cwd": "src"},
                  (_result_spec(2, err="ImportError: Start directory is not importable: 'tests'"),
                   _result_spec(2, err="ImportError: Start directory is not importable: 'tests'"))),
        _scenario("repeat_missing_target", "repeated_failure", "unrecoverable", root_tests,
                  {"task": "run tests", "command": ["pytest", "tests/missing.py"]},
                  (_result_spec(4, err="ERROR: file or directory not found: tests/missing.py"),
                   _result_spec(4, err="ERROR: file or directory not found: tests/missing.py"))),
    ])
    for index, output in enumerate(("1 passed in 0.01s", "3 passed in 0.05s", "Ran 1 test\nOK"), 1):
        cases.append(_scenario(f"success_{index}", "successful_command", "not_applicable", {},
            {"task": "run tests", "command": ["pytest"] if "passed" in output else [py, "-m", "unittest", "discover"]},
            (_result_spec(0, out=output),)))
    for index, output in enumerate(("", "command completed", "0 passed, 1 skipped"), 1):
        cases.append(_scenario(f"inconclusive_{index}", "inconclusive_verification", "unrecoverable", {},
            {"task": "run tests", "command": ["pytest"]}, (_result_spec(0, out=output),)))
    for index, message in enumerate(("runner crashed with code 91", "unexpected executor state", "unclassified failure"), 1):
        cases.append(_scenario(f"unknown_{index}", "unknown_error", "unrecoverable", {},
            {"task": "run tests", "command": ["pytest"]}, (_result_spec(1, err=message),)))
    return cases


def _reset_workspace(root: Path, case: dict) -> tuple[tuple[str, str], ...]:
    for child in root.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    for relative, content in case["files"].items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    cwd = case["payload"].get("cwd")
    if cwd:
        (root / cwd).mkdir(parents=True, exist_ok=True)
    state = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        state.append((relative, "directory" if path.is_dir() else path.read_text(encoding="utf-8")))
    return tuple(sorted(state))


def _guardian_run(payload: dict, root: Path, case: dict) -> dict:
    fake_runner = _runner_for(case["results"])
    outcome = run_agent(payload, workspace=root, runner=fake_runner)
    final_attempt = outcome["attempts"][-1]
    last_result = subprocess.CompletedProcess(final_attempt["command"], final_attempt["returncode"],
                                              final_attempt["stdout"], final_attempt["stderr"])
    return {"input": copy.deepcopy(payload),
            "claimed_success": outcome["status"] == "verified",
            "verified_success": _shared_success(last_result, payload),
            "attempts": outcome["attempts"], "trace": outcome["trace"],
            "configured_results": fake_runner.configured_specs,
            "results_consumed": tuple(fake_runner.used_specs),
            "recovery_actions": [event for event in outcome["trace"]
                                 if event.get("type") == "action" and "attempt" in event],
            "command_executions": len(outcome["attempts"]),
            "inspections": sum(event.get("type") == "tool_call" and
                                event.get("action") != "run_command" for event in outcome["trace"])}


def _baseline_run(payload: dict, root: Path, case: dict) -> dict:
    fake_runner = _runner_for(case["results"])
    outcome = _legacy_agent(payload, root, fake_runner)
    last_spec = case["results"][len(outcome["attempts"]) - 1]
    last_result = subprocess.CompletedProcess(outcome["attempts"][-1]["command"], *last_spec)
    return {"input": copy.deepcopy(payload),
            "claimed_success": outcome["claimed_success"],
            "verified_success": _shared_success(last_result, payload),
            "attempts": outcome["attempts"], "recovery_actions": outcome["actions"],
            "configured_results": fake_runner.configured_specs,
            "results_consumed": tuple(fake_runner.used_specs),
            "command_executions": len(outcome["attempts"]),
            "inspections": outcome["inspection_count"]}


def _retry_audit(outcome: dict) -> list[dict]:
    """A retry is unnecessary only if it changes neither argv nor working directory."""
    audit = []
    actions = outcome["recovery_actions"]
    attempts = outcome["attempts"]
    for index, action in enumerate(actions):
        previous = attempts[index]
        old_command = previous["command"]
        old_cwd = previous["cwd"]
        new_command = action.get("command", old_command)
        new_cwd = action.get("cwd", old_cwd)
        changed = new_command != old_command or str(new_cwd) != str(old_cwd)
        documented = bool(action.get("reason")) and (bool(action.get("environment_change")) or changed)
        audit.append({"action": action.get("action", "baseline_retry"),
                      "reason": action.get("reason", ""),
                      "command_changed": new_command != old_command,
                      "working_directory_changed": str(new_cwd) != str(old_cwd),
                      "documented": documented,
                      "unnecessary": not changed or not documented})
    return audit


def _summarize(cases: list[dict], outcomes: dict[str, dict]) -> dict:
    category_counts = Counter(case["category"] for case in cases)
    recoverability_counts = Counter(case["recoverability"] for case in cases)
    recoverable = [case for case in cases if case["recoverability"] == "recoverable"]
    unrecoverable = [case for case in cases if case["recoverability"] == "unrecoverable"]
    recoveries = sum(outcomes[case["name"]]["verified_success"] for case in recoverable)
    recovery_failures = len(recoverable) - recoveries
    false_claims = sum(outcome["claimed_success"] and not outcome["verified_success"]
                       for outcome in outcomes.values())
    safe_stops = sum(not outcomes[case["name"]]["claimed_success"] for case in unrecoverable)
    retry_audits = {name: _retry_audit(outcome) for name, outcome in outcomes.items()}
    unnecessary = sum(item["unnecessary"] for audits in retry_audits.values() for item in audits)
    commands = sum(outcome["command_executions"] for outcome in outcomes.values())
    inspections = sum(outcome["inspections"] for outcome in outcomes.values())
    recovery_attempts = sum(max(0, outcome["command_executions"] - 1)
                            for outcome in outcomes.values())
    tasks = len(cases)
    scenario_report = {}
    for case in cases:
        outcome = outcomes[case["name"]]
        scenario_report[case["name"]] = {
            "category": case["category"], "recoverability": case["recoverability"],
            "expected_success": case["expected_success"],
            "claimed_success": outcome["claimed_success"],
            "verified_success": outcome["verified_success"],
            "command_executions": outcome["command_executions"],
            "inspections": outcome["inspections"],
            "recovery_attempts": max(0, outcome["command_executions"] - 1),
            "recovery_action_audit": retry_audits[case["name"]],
        }
    return {"recovery_success_rate": recoveries / len(recoverable) if recoverable else 0,
            "recovered_scenarios": recoveries, "recoverable_scenarios": len(recoverable),
            "recovery_failures": recovery_failures,
            "safely_stopped_unrecoverable_scenarios": safe_stops,
            "unnecessary_retries": unnecessary, "false_success_claims": false_claims,
            "command_executions": commands, "inspections": inspections,
            "recovery_attempts": recovery_attempts,
            "tool_call_count": commands + inspections,
            "average_tool_calls_per_task": (commands + inspections) / tasks,
            "category_counts": dict(sorted(category_counts.items())),
            "recoverability_counts": dict(sorted(recoverability_counts.items())),
            "scenarios": scenario_report}


def _validate_scenario_contracts(cases: list[dict]) -> None:
    """Refuse to score mislabeled cases whose fixture output contradicts its label."""
    for case in cases:
        payload = case["payload"]
        first_code, first_out, first_err = case["results"][0]
        last_code, last_out, last_err = case["results"][-1]
        first_passes = _shared_success(subprocess.CompletedProcess(payload["command"], first_code,
                                                                   first_out, first_err), payload)
        last_passes = _shared_success(subprocess.CompletedProcess(payload["command"], last_code,
                                                                  last_out, last_err), payload)
        if last_passes != case["expected_success"]:
            raise ValueError(f"scenario {case['name']} expected_success conflicts with its final fixture output")
        if case["recoverability"] == "recoverable" and first_passes:
            raise ValueError(f"scenario {case['name']} is labeled recoverable but has no initial failure")
        if case["recoverability"] == "unrecoverable" and first_passes:
            raise ValueError(f"scenario {case['name']} is labeled unrecoverable but starts successful")


def evaluate() -> dict:
    cases = scenarios()
    _validate_scenario_contracts(cases)
    paired = {"baseline_emulation": {}, "guardian": {}}
    exact_pairing = True
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        for case in cases:
            payload = copy.deepcopy(case["payload"])
            baseline_state = _reset_workspace(root, case)
            baseline = _baseline_run(payload, root, case)
            guardian_state = _reset_workspace(root, case)
            guardian_payload = copy.deepcopy(case["payload"])
            guardian = _guardian_run(guardian_payload, root, case)
            exact_pairing &= (baseline["input"] == guardian["input"] == case["payload"]
                              and baseline_state == guardian_state
                              and baseline["configured_results"] == guardian["configured_results"]
                              and baseline["results_consumed"] == baseline["configured_results"][:len(baseline["results_consumed"])]
                              and guardian["results_consumed"] == guardian["configured_results"][:len(guardian["results_consumed"])]
                              and case["expected_success"] == (case["recoverability"] == "recoverable"
                                                               or case["category"] == "successful_command"))
            paired["baseline_emulation"][case["name"]] = baseline
            paired["guardian"][case["name"]] = guardian

    return {"scenario_count": len(cases), "paired_inputs_identical": exact_pairing,
            "scenario_contract_valid": True,
            "shared_success_criterion": "same _verify criteria (zero exit, observed passing tests, and requested output present) applied to each policy's final command result",
            "baseline_emulation": _summarize(cases, paired["baseline_emulation"]),
            "guardian": _summarize(cases, paired["guardian"])}


if __name__ == "__main__":
    print(json.dumps(evaluate(), indent=2, sort_keys=True))
