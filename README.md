# Agent Guardian

Agent Guardian supervises Python test commands for coding agents. It captures command output, classifies known error signatures, chooses a category-specific diagnostic, and records its decisions. Test-file discovery runs only for missing-path/test-discovery errors; top-level dependency manifests are checked only for missing-import errors. Assertion, permission, unknown, and inconclusive outcomes stop without a workspace scan. It never installs dependencies or edits files.

## Diagnosis and recovery policy

| Category | Evidence | Policy |
| --- | --- | --- |
| `wrong_working_directory` | Test-discovery error plus test files located under another project root | Rerun the same command at that root, within the recovery limit |
| `missing_test_file` | Test runner says the explicit target is missing | If an explicit pytest target is missing and test files exist, replace it with discovered test paths; otherwise stop |
| `missing_dependency` | Import error names a module | Inspect known dependency manifest paths and report the missing module; never install it |
| `test_assertion_failure` | Assertion or test failure output | Stop and preserve the failure evidence |
| `permission_error` | Permission-denied output | Stop; do not change permissions |
| `unknown_error` | Output does not support another category | Stop without guessing |
| `repeated_failure` | Same exit code and normalized output after a corrective action | Stop immediately |

The recovery-attempt limit defaults to one. A retry must change command arguments or working directory, and its trace records which changed and why. An identical command in an unchanged environment is never retried. Outputs include command attempts, any targeted inspection, diagnosis, selected action or stop reason, and verification criteria.

## Verification contract

A zero exit code is not sufficient. `unittest` output must show `Ran N tests` for `N > 0` and `OK`; pytest output must show at least one passed test. A nonzero exit is `failed`. A zero exit without a recognizable test summary is `inconclusive`, and Guardian does not report task success. Requests can add `expected_tests` and `required_output` string lists; every requested item must appear in captured output for verification to pass.

## Input and run

Provide a JSON object on stdin or use `--input`:

```json
{
  "task": "Run unit tests",
  "command": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
  "cwd": "src",
  "expected_tests": ["test_agent.GuardianTests.test_success_verifies_and_stops_after_one_call"]
}
```

`cwd` defaults to the current workspace. `expected_tests` and `required_output` are optional lists of exact output substrings. Only Python `unittest` and `pytest` commands are accepted; argv is executed without a shell. The working directory must remain inside the workspace, and each invocation has a 45-second timeout.

```sh
printf '{"task":"Run unit tests","command":["python3","-m","unittest","discover","-s","tests","-v"]}\n' | python3 -m agent.cli
```

Exit status is `0` only when verification passes. Invalid input or failed/inconclusive verification exits nonzero.

## Tests and evaluation

Run the complete test suite:

```sh
python3 -m unittest discover -s tests -v
```

Run the deterministic before/after evaluation:

```sh
python3 tests/evaluate.py
```

The harness runs the same 27 scenario inputs, reset filesystem state, mocked command-result sequence, and shared strict success oracle against a test-only emulation of the prior implementation and Guardian. Its category mix is: 3 wrong-directory, 4 missing-test-file, 3 assertion, 3 dependency, 3 permission, 2 repeated-failure, 3 successful, 3 inconclusive, and 3 unknown-error cases. Five are recoverable, 19 unrecoverable, and 3 successful-command cases are not recovery cases. Unrecoverable cases are reported as safe stops or false claims; they are excluded from the recovery-success denominator. Measured results:

| Metric | Prior behavior emulation | Current Guardian |
| --- | ---: | ---: |
| Recovery success rate | 2/5 (40%) | 5/5 (100%) |
| Unnecessary retries | 0 | 0 |
| False success claims | 3 | 0 |
| Command executions | 30 | 35 |
| Diagnostic inspections | 21 | 12 |
| Recovery attempts | 3 | 8 |
| Total counted operations | 51 (1.89/task) | 47 (1.74/task) |

One counted operation is either a command execution or one logical diagnostic inspection. The baseline emulation's inspection is its root `tests/` existence/glob check after a failed command; Guardian inspections are targeted test-file or dependency-manifest checks. This operation count does not measure how many paths each inspection reads. A recovery attempt is every command execution after the initial one. An unnecessary retry is a retry without a documented reason and a change in command arguments or working directory; the three retries previously labeled unnecessary were all evidence-based state/command changes, so the corrected count is zero for both policies. Against the prior Guardian policy of inspecting after every non-passing result, targeted inspection uses 47 rather than 57 operations on Guardian's outcomes. The baseline remains an emulation, not a separately executed checkout. These synthetic scenarios do not establish statistical significance.

## Files and limitations

- `agent/core.py` validates commands, inspects paths, diagnoses known error categories, recovers from wrong directories and missing explicit pytest targets, and verifies reported test output.
- `agent/cli.py` reads JSON and emits the result.
- `tests/test_agent.py` covers each diagnosis category, recovery/stopping behavior, verification, loops, and safety boundaries.
- `tests/evaluate.py` and `tests/test_evaluation.py` implement and assert the baseline comparison.

Guardian does not repair assertion failures, grant permissions, install missing dependencies, or infer arbitrary fixes. It currently supports only Python test commands and relies on recognizable test-runner output. A discovered test-file replacement runs all discovered Python test files, which can be broader than the original intended target; inspect the proposed command and use `expected_tests` when the target matters.
