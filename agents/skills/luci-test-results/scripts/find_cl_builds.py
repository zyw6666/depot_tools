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
REUSED_BUILD_THRESHOLD = 5
_PATCHSET_CACHE = {}


def _normalize_gerrit_host(host):
    """Normalizes a Gerrit host name, mapping .git.corp.google.com to .googlesource.com."""
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
    """Detects the Gerrit CL number of the active git branch via `git cl issue`."""
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


def _deduplicate_latest_builds(builds):
    """Deduplicates builds per builder, keeping the newest Buildbucket ID."""
    latest_by_builder = {}
    for b in builds:
        builder_name = b.get("builder", {}).get("builder")
        if not builder_name:
            continue
        build_id = int(b["id"])
        # Smaller Buildbucket ID means newer build (inverted timestamp snowflake).
        if builder_name not in latest_by_builder or build_id < int(
            latest_by_builder[builder_name]["id"]
        ):
            latest_by_builder[builder_name] = b
    return list(latest_by_builder.values())


def _unique_builder_count(builds):
    """Returns the number of distinct builders in a build list."""
    return len(
        {
            b.get("builder", {}).get("builder")
            for b in builds
            if b.get("builder", {}).get("builder")
        }
    )


def find_cl_builds(
    cl_number,
    patchset=None,
    host=None,
    show_all=False,
    latest_only=True,
    merge_reused=False,
    return_none_on_error=False,
):
    """Finds Buildbucket builds for a CL number or Gerrit/crrev URL.

    Args:
        cl_number: Numeric CL ID or Gerrit/crrev URL string.
        patchset: Optional explicit patchset number (auto-detected if omitted).
        host: Optional Gerrit host override.
        show_all: If True, include SUCCESS and STARTED builds.
        latest_only: If True, deduplicate retry attempts per builder.
        merge_reused: If True, merge reused SUCCESS builds from prior patchsets
            when the target patchset has 1..REUSED_BUILD_THRESHOLD unique builders.
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
    if (auto_patchset or merge_reused) and current_ps > 1:
        candidate_patchsets.extend(
            range(
                current_ps - 1,
                max(0, current_ps - 1 - PRIOR_PATCHSET_LOOKBACK),
                -1,
            )
        )

    raw_builds = []
    for idx, ps in enumerate(candidate_patchsets):
        res = query_ps(ps)
        if res is None:
            return error_return
        ps_builds = res.get("builds", [])
        if not ps_builds:
            # If the target patchset had builds and we are merging reused prior
            # patchsets, continue scanning older patchsets; if the target
            # patchset was explicitly requested without merge_reused, stop.
            if not auto_patchset and not raw_builds:
                break
            continue

        unique_count = _unique_builder_count(ps_builds)

        if not raw_builds:
            # First patchset with builds (either current_ps or fallback ps).
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
            # Only scan prior patchsets for reused builds if merge_reused is
            # enabled on the target patchset and it has <= REUSED_BUILD_THRESHOLD
            # unique builders.
            if (
                not merge_reused
                or idx > 0
                or unique_count > REUSED_BUILD_THRESHOLD
            ):
                break
        else:
            # Merging reused builds from a prior patchset: LUCI CV only reuses
            # successful builds across trivial patchsets.
            for b in ps_builds:
                if b.get("status") == "SUCCESS":
                    b = dict(b)
                    b["_patchset"] = ps
                    raw_builds.append(b)
            if unique_count > REUSED_BUILD_THRESHOLD:
                break

    if not raw_builds:
        return []

    if latest_only:
        raw_builds = _deduplicate_latest_builds(raw_builds)

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
    parser.add_argument(
        "--all-attempts",
        action="store_true",
        help="Include superseded build attempts instead of deduplicating per builder",
    )
    parser.add_argument(
        "--merge-reused",
        action="store_true",
        help=(
            "Merge reused CQ builds from up to 3 preceding patchsets when the "
            "latest patchset has <= 5 builds"
        ),
    )

    args = parser.parse_args()
    cl_target = args.cl or get_current_branch_cl()
    if not cl_target:
        parser.error(
            "No --cl specified and no active branch CL detected via `git cl issue`."
        )

    builds = find_cl_builds(
        cl_target,
        patchset=args.patchset,
        host=args.host,
        show_all=args.all,
        latest_only=not args.all_attempts,
        merge_reused=args.merge_reused,
    )
    print(json.dumps(builds, indent=2))


if __name__ == "__main__":
    main()
