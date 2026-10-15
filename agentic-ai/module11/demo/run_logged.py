# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run a command with its output streamed into a log, ending the log with `M11_EXIT <code> <ts>`.

    python -m module11.demo.run_logged --log module11/.local/drift-revert-<ts>.log -- \
        python -m module11 drift revert --stage staging

The demo driver starts this as a detached child for 2e's background revert. When the command ends, the log's
last line is `M11_EXIT <code> <ISO 8601 UTC timestamp>` and this process exits with the same code, so `d 6` can
report the revert's final state from the log alone. A command that cannot be started ends with code 127.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

EXIT_PREFIX = "M11_EXIT"


def exit_line(code: int, at: datetime | None = None) -> str:
    return f"{EXIT_PREFIX} {code} {(at or datetime.now(timezone.utc)).isoformat()}"


def parse_exit(log_text: str) -> tuple[int, str] | None:
    """(code, timestamp) from the log's last non-empty line, or None when the command has not ended."""
    lines = [line for line in log_text.splitlines() if line.strip()]
    if not lines:
        return None
    parts = lines[-1].split()
    if len(parts) == 3 and parts[0] == EXIT_PREFIX:
        try:
            return int(parts[1]), parts[2]
        except ValueError:
            return None
    return None


def run(command: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        try:
            # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; a log wrapper; argv is its caller's (the demo's fixed drift-revert command)
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as exc:
            log.write(f"cannot start {command[0]}: {exc}\n")
            code = 127
        else:
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                log.flush()
            code = proc.wait()
        log.write(exit_line(code) + "\n")
        log.flush()
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- command [args...]")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    return run(command, args.log)


if __name__ == "__main__":
    sys.exit(main())
