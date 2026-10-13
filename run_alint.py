#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import json
import os
import re
import subprocess
import sys
import tempfile
import time


_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Printed by alint (with --verbose) once every finding has been written and
# before it flushes its metrics / Clearcut telemetry. alint colors "INFO:"
# even when not writing to a terminal, so match after stripping ANSI codes.
_END_EVENT_PREFIX = "INFO: endEvent:"
_END_EVENT_EXIT_CODE_RE = re.compile(r"invocation_stopped:\{exit_code:(\d+)")
_POLL_INTERVAL_S = 0.005


def _strip_ansi_codes(text):
    return _ANSI_ESCAPE_RE.sub("", text)


def _format_finding(finding):
    if not finding:
        return finding
    if finding.startswith(("[AyeAye/", "[AyeAye]")):
        return finding
    if finding.startswith("["):
        return f"[AyeAye/{finding[1:]}"
    return f"[AyeAye] {finding}"


def _parse_alint_output(clean_output):
    errors = []
    warnings = []
    for line in clean_output.splitlines():
        clean_line = line.strip()
        if clean_line.startswith("ERROR:"):
            errors.append(_format_finding(clean_line[len("ERROR:") :].strip()))
        elif clean_line.startswith("WARNING:"):
            warnings.append(
                _format_finding(clean_line[len("WARNING:") :].strip())
            )
    return {"errors": errors, "warnings": warnings}


def _strip_info_lines(clean_output):
    """Drops alint's --verbose INFO lines from an execution error report."""
    return "\n".join(
        line
        for line in clean_output.splitlines()
        if not line.strip().startswith("INFO:")
    ).strip()


def _run_alint(cmd, env):
    """Runs alint and returns (exit_code, output).

    Returns as soon as alint prints its endEvent line (after all findings)
    instead of waiting for it to flush telemetry; the detached alint process
    finishes on its own. Output goes to a temp file because closing a pipe
    early would kill alint with SIGPIPE.
    """
    fd, path = tempfile.mkstemp(prefix="alint_", suffix=".log")
    try:
        with os.fdopen(fd, "wb") as out:
            proc = subprocess.Popen(
                cmd,
                stdout=out,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        buf = b""
        scanned = 0  # buf[:scanned] holds complete lines already checked.
        with open(path, "rb") as reader:
            while True:
                chunk = reader.read()
                if chunk:
                    buf += chunk
                    while (newline := buf.find(b"\n", scanned)) != -1:
                        line = _strip_ansi_codes(
                            buf[scanned:newline].decode("utf-8", "ignore")
                        ).strip()
                        scanned = newline + 1
                        if line.startswith(_END_EVENT_PREFIX):
                            match = _END_EVENT_EXIT_CODE_RE.search(line)
                            exit_code = int(match.group(1)) if match else 0
                            return exit_code, buf[:scanned].decode(
                                "utf-8", "ignore"
                            )
                    continue
                if proc.poll() is not None:
                    buf += reader.read()
                    return proc.returncode, buf.decode("utf-8", "ignore")
                time.sleep(_POLL_INTERVAL_S)
    finally:
        os.unlink(path)


def main():
    if len(sys.argv) < 3:
        print("Usage: run_alint.py <alint_path> <repo_root> [alint_args...]")
        return 3

    alint_path = sys.argv[1]
    repo_root = sys.argv[2]
    # Change CWD to the repository root. This is necessary because
    # presubmit_support.py has a workaround when testing depot_tools that
    # changes the process CWD to the parent directory of depot_tools.
    # alint requires running from inside a git repository to detect changes.
    os.chdir(repo_root)

    # git cl requires a clean tree, so lint HEAD directly instead of letting
    # alint scan the worktree to choose between HEAD and uncommitted changes.
    # --verbose makes alint print the endEvent line _run_alint() waits for.
    cmd = [alint_path, "--verbose", "--"] + sys.argv[3:] + ["--commit=HEAD"]
    try:
        env = os.environ.copy()
        git_params = env.get("GIT_CONFIG_PARAMETERS", "").strip()
        params = []
        if not re.search(
            r"'status\.showuntrackedfiles=no'", git_params, re.IGNORECASE
        ):
            params.append("'status.showUntrackedFiles=no'")
        if not re.search(
            r"'diff\.ignoresubmodules=all'", git_params, re.IGNORECASE
        ):
            params.append("'diff.ignoreSubmodules=all'")
        if params:
            new_params = " ".join(params)
            if git_params:
                env["GIT_CONFIG_PARAMETERS"] = f"{git_params} {new_params}"
            else:
                env["GIT_CONFIG_PARAMETERS"] = new_params

        returncode, output_str = _run_alint(cmd, env)
        clean = _strip_ansi_codes(output_str).strip()
        parsed = _parse_alint_output(clean)
        if returncode and not parsed["errors"] and not parsed["warnings"]:
            parsed["execution_error"] = {
                "exit_code": returncode,
                "output": _strip_info_lines(clean),
            }
        print(json.dumps(parsed))
        return 0
    except Exception as e:
        print(
            json.dumps(
                {
                    "errors": [],
                    "warnings": [],
                    "execution_error": {
                        "exit_code": 1,
                        "output": f"Unexpected error in AyeAye: {e}",
                    },
                }
            )
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
