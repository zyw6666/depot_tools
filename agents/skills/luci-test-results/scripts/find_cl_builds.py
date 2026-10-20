#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Find Buildbucket builds triggered for a Gerrit CL."""

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from luci_client import run_prpc

DEFAULT_GERRIT_HOST = "chromium-review.googlesource.com"
INTERNAL_GERRIT_HOST = "chrome-internal-review.googlesource.com"
PRIOR_PATCHSET_LOOKBACK = 3
_PATCHSET_CACHE = {}


def _normalize_gerrit_host(host):
    """Normalizes a Gerrit host (.git.corp.google.com -> .googlesource.com)."""
    if not host:
        return DEFAULT_GERRIT_HOST
    return host.replace(".git.corp.google.com", ".googlesource.com")


def parse_cl_arg(cl_arg):
    """Parses a CL number or Gerrit URL into (host, cl_number, patchset)."""
    if cl_arg is None:
        return None, None, None

    text = str(cl_arg).strip()
    if text.isdigit():
        return None, text, None

    text = _normalize_gerrit_host(text)

    # Match full Gerrit or crrev URLs:
    # - https://crrev.com/c/<cl>[/<ps>] or https://crrev.com/i/<cl>[/<ps>]
    # - https://<host>/[#/]c/[<project>/+/]<cl>[/<ps>]
    gerrit_match = re.search(
        r"(?:https?://)?([a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+)/"
        r"(?:#/)?(?:([ci])/(?:[^+]+/\+/)?|\+/)?(\d+)(?:/(\d+))?",
        text,
    )
    if gerrit_match:
        host = gerrit_match.group(1)
        short_prefix = gerrit_match.group(2)
        cl_num = gerrit_match.group(3)
        ps = int(gerrit_match.group(4)) if gerrit_match.group(4) else None
        if host in ("crrev.com", "www.crrev.com"):
            host = INTERNAL_GERRIT_HOST if short_prefix == "i" else None
        return host, cl_num, ps

    return None, None, None


def get_current_branch_cl():
    """Detects the active git branch Gerrit CL number via `git cl issue`."""
    try:
        output = (
            subprocess.check_output(
                ["git", "cl", "issue"], stderr=subprocess.DEVNULL
            )
            .decode("utf-8")
            .strip()
        )
        match = re.search(r"Issue number:\s*(\d+)", output)
        if match:
            return match.group(1)
    except (subprocess.SubprocessError, ValueError, OSError):
        return None
    return None


def get_latest_patchset(
    cl_number, host=DEFAULT_GERRIT_HOST, retries=5, retry_delay=1.0
):
    """Gets the latest patchset number for a CL via Gerrit REST API."""
    host = _normalize_gerrit_host(host)
    cache_key = (host, str(cl_number))
    base_url = f"https://{host}/changes"
    cmd = ["curl", "-s", "-L", f"{base_url}/{cl_number}?o=CURRENT_REVISION"]

    last_err = None
    for attempt in range(max(1, retries)):
        try:
            raw = subprocess.check_output(cmd).decode("utf-8")
            if raw.startswith(")]}'"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else ""
            raw = raw.strip()
            if not raw:
                raise ValueError("Empty Gerrit response")
            data = json.loads(raw)
            ps = int(data["revisions"][data["current_revision"]]["_number"])
            _PATCHSET_CACHE[cache_key] = ps
            return ps
        except (
            subprocess.SubprocessError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as e:
            last_err = e
            if attempt < retries - 1 and retry_delay > 0:
                time.sleep(retry_delay * (attempt + 1))

    if cache_key in _PATCHSET_CACHE:
        return _PATCHSET_CACHE[cache_key]

    print(
        f"Error getting latest patchset for CL {cl_number}: {last_err}",
        file=sys.stderr,
    )
    return None


def find_cl_builds(
    cl_number,
    patchset=None,
    host=None,
    show_all=False,
    return_none_on_error=False,
):
    """Finds Buildbucket builds for a CL number or Gerrit/crrev URL.

    Args:
        cl_number: Numeric CL ID or Gerrit/crrev URL string.
        patchset: Optional explicit patchset number (auto-detected if omitted).
        host: Optional Gerrit host override.
        show_all: If True, include SUCCESS and STARTED builds.
        return_none_on_error: If True, return None instead of [] on error.
    """
    error_return = None if return_none_on_error else []

    parsed_host, parsed_cl, parsed_ps = parse_cl_arg(cl_number)
    if not parsed_cl:
        print(f"Error: Invalid CL identifier '{cl_number}'.", file=sys.stderr)
        return error_return
    cl_number = parsed_cl
    host = _normalize_gerrit_host(host or parsed_host)

    auto_patchset = patchset is None and parsed_ps is None
    if patchset is None and parsed_ps is not None:
        patchset = parsed_ps

    if auto_patchset:
        patchset = get_latest_patchset(cl_number, host=host)
        if not patchset:
            return error_return

    try:
        current_ps = int(patchset)
        if current_ps < 1:
            raise ValueError("patchset must be >= 1")
    except (TypeError, ValueError):
        print(f"Error: Invalid patchset '{patchset}'.", file=sys.stderr)
        return error_return

    def query_ps(ps):
        payload = {
            "predicate": {
                "gerritChanges": [
                    {
                        "host": host,
                        "change": int(cl_number),
                        "patchset": int(ps),
                    }
                ]
            }
        }
        return run_prpc(
            "cr-buildbucket.appspot.com",
            "buildbucket.v2.Builds.SearchBuilds",
            payload,
        )

    candidate_patchsets = [current_ps]
    if auto_patchset and current_ps > 1:
        candidate_patchsets.extend(
            range(
                current_ps - 1,
                max(0, current_ps - 1 - PRIOR_PATCHSET_LOOKBACK),
                -1,
            )
        )

    raw_builds = []
    for ps in candidate_patchsets:
        res = query_ps(ps)
        if res is None:
            return error_return
        ps_builds = res.get("builds", [])
        if not ps_builds:
            if not auto_patchset:
                break
            continue

        if ps != current_ps:
            print(
                f"Notice: No builds on patchset {current_ps}; "
                f"using patchset {ps}.",
                file=sys.stderr,
            )
        for b in ps_builds:
            b = dict(b)
            b["_patchset"] = ps
            raw_builds.append(b)
        break

    if not raw_builds:
        return []

    return [
        {
            "builder": b["builder"]["builder"],
            "status": b["status"],
            "id": b["id"],
            "patchset": b.get("_patchset", current_ps),
        }
        for b in raw_builds
        if show_all or b["status"] not in ("SUCCESS", "STARTED")
    ]


def main():
    parser = argparse.ArgumentParser(
        description="Find Buildbucket builds triggered for a Gerrit CL."
    )
    parser.add_argument(
        "--cl",
        help=(
            "Gerrit CL number or URL (e.g. 8260911). If omitted, auto-detects "
            "the active branch CL via `git cl issue`."
        ),
    )
    parser.add_argument(
        "--patchset",
        type=int,
        help="Optional patchset number (defaults to latest)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help=f"Gerrit host (default: {DEFAULT_GERRIT_HOST})",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Show all builds, not just failures/scheduled builds",
    )

    args = parser.parse_args()
    cl_target = args.cl or get_current_branch_cl()
    if not cl_target:
        parser.error(
            "No --cl specified and no active branch CL detected via "
            "`git cl issue`."
        )

    builds = find_cl_builds(
        cl_target,
        patchset=args.patchset,
        host=args.host,
        show_all=args.all,
    )
    print(json.dumps(builds, indent=2))


if __name__ == "__main__":
    main()
