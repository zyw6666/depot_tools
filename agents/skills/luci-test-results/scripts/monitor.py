#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Monitor the status of CQ try jobs across one or more Gerrit CLs."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from find_cl_builds import (
    DEFAULT_GERRIT_HOST,
    find_cl_builds,
    get_current_branch_cl,
    parse_cl_arg,
)

EXIT_ALL_PASSED = 0
EXIT_SOME_FAILED = 1
EXIT_STILL_RUNNING = 2
EXIT_ERROR = 3

FAILED_STATUSES = ("FAILURE", "INFRA_FAILURE", "CANCELED")
RUNNING_STATUSES = ("STARTED", "SCHEDULED")


def monitor_cls(cl_list, patchset=None, host=None, as_json=False):
    """Monitors CQ try job status for a list of CLs and returns an exit code.

    Multi-CL exit code precedence:
    - EXIT_SOME_FAILED (1): Any CL has at least one failed/canceled/infra-failed build.
    - EXIT_STILL_RUNNING (2): No builds have failed yet, and at least one CL has
      running/scheduled builds (even if another CL in the stack has NO_JOBS).
    - EXIT_ALL_PASSED (0): Every CL in cl_list has status == "PASSED".
    - EXIT_ERROR (3): Any CL has ERROR/NO_JOBS/UNKNOWN and no builds are running
      or failed, or cl_list is empty.
    """
    summaries = []

    if not as_json:
        print(
            f"{'CL':<10} | {'Status':<10} | "
            f"{'Jobs (Total/Success/Failed/Running)':<40}"
        )
        print("-" * 70)

    for cl_item in cl_list:
        parsed_host, parsed_cl, parsed_ps = parse_cl_arg(cl_item)
        cl_display = parsed_cl or str(cl_item)
        effective_host = host or parsed_host
        effective_ps = patchset if patchset is not None else parsed_ps

        if not parsed_cl:
            jobs = None
        else:
            jobs = find_cl_builds(
                parsed_cl,
                patchset=effective_ps,
                host=effective_host,
                show_all=True,
                latest_only=True,
                merge_reused=True,
                return_none_on_error=True,
            )

        if not jobs:
            status = "ERROR" if jobs is None else "NO_JOBS"
            table_label = "ERROR" if jobs is None else "NO JOBS"
            if not as_json:
                print(f"{cl_display:<10} | {table_label:<10} | {'-':<40}")
            summaries.append(
                {
                    "cl": cl_display,
                    "status": status,
                    "total": 0,
                    "success": 0,
                    "failed": 0,
                    "running": 0,
                    "builds": [],
                }
            )
            continue

        total = len(jobs)
        success = sum(1 for j in jobs if j["status"] == "SUCCESS")
        failed = sum(1 for j in jobs if j["status"] in FAILED_STATUSES)
        running = sum(1 for j in jobs if j["status"] in RUNNING_STATUSES)

        if failed > 0:
            status = "FAILED"
        elif running > 0:
            status = "RUNNING"
        elif success == total:
            status = "PASSED"
        else:
            status = "UNKNOWN"

        summaries.append(
            {
                "cl": cl_display,
                "status": status,
                "total": total,
                "success": success,
                "failed": failed,
                "running": running,
                "builds": jobs,
            }
        )

        if not as_json:
            counts_str = f"{total}/{success}/{failed}/{running}"
            print(f"{cl_display:<10} | {status:<10} | {counts_str:<40}")
            if status != "PASSED":
                for j in jobs:
                    if j["status"] != "SUCCESS":
                        print(f"  - {j['builder']}: {j['status']} ({j['id']})")

    statuses = [s["status"] for s in summaries]
    if "FAILED" in statuses:
        overall = "SOME JOBS FAILED"
        exit_code = EXIT_SOME_FAILED
    elif "RUNNING" in statuses:
        overall = "SOME JOBS STILL RUNNING"
        exit_code = EXIT_STILL_RUNNING
    elif summaries and all(st == "PASSED" for st in statuses):
        overall = "ALL JOBS PASSED"
        exit_code = EXIT_ALL_PASSED
    else:
        overall = "UNKNOWN STATE"
        exit_code = EXIT_ERROR

    if as_json:
        print(
            json.dumps(
                {
                    "result": overall,
                    "exit_code": exit_code,
                    "cls": summaries,
                },
                indent=2,
            )
        )
    else:
        print("-" * 70)
        print(f"Result: {overall}")

    return exit_code


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Monitor the status of CQ try jobs for one or more Gerrit CLs."
        )
    )
    parser.add_argument(
        "cls",
        metavar="CL",
        nargs="*",
        help=(
            "Gerrit CL numbers or URLs to monitor. If omitted, uses the "
            "active branch's CL via `git cl issue`."
        ),
    )
    parser.add_argument(
        "--patchset",
        type=int,
        help="Optional patchset number (defaults to latest with CQ reuse fallback)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help=f"Gerrit host (default: {DEFAULT_GERRIT_HOST})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON summary instead of ASCII table",
    )

    args = parser.parse_args()
    cl_list = list(args.cls)
    if not cl_list:
        current_cl = get_current_branch_cl()
        if current_cl:
            cl_list = [current_cl]
            if not args.json:
                print(
                    f"No CLs specified. Monitoring active branch CL: "
                    f"{current_cl}"
                )
        else:
            if args.json:
                print(
                    json.dumps(
                        {
                            "result": "UNKNOWN STATE",
                            "exit_code": EXIT_ERROR,
                            "cls": [],
                        },
                        indent=2,
                    )
                )
            else:
                print(
                    "Error: No CLs specified and no active branch CL found.",
                    file=sys.stderr,
                )
            sys.exit(EXIT_ERROR)

    sys.exit(
        monitor_cls(
            cl_list,
            patchset=args.patchset,
            host=args.host,
            as_json=args.json,
        )
    )


if __name__ == "__main__":
    main()
