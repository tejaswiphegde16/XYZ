"""Command-line interface for Agent Guardian."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .core import AgentError, run_agent


def read_payload(input_path: str | None) -> Any:
    try:
        if input_path:
            raw = Path(input_path).read_text(encoding="utf-8")
        else:
            raw = sys.stdin.read()
        return json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentError(f"could not read valid JSON input: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a supervised Python test check.")
    parser.add_argument("--input", help="Read one JSON object from this file; stdin is used otherwise.")
    args = parser.parse_args(argv)

    try:
        output = run_agent(read_payload(args.input))
    except AgentError as exc:
        output = {"ok": False, "error": str(exc)}
    print(json.dumps(output, ensure_ascii=False))
    return 0 if output.get("ok") and output.get("status") == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
