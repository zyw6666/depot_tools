#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Find Buildbucket builds triggered for a Gerrit CL."""

import argparse
import json
import re
import subprocess
import sys
import time

DEFAULT_GERRIT_HOST = "chromium-review.googlesource.com"
INTERNAL_GERRIT_HOST = "chrome-internal-review.googlesource.com"


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


def _extract_patchset_from_tags(tags, default_ps=None):
    """Extracts the Gerrit patchset number from Buildbucket buildset tags."""
    for tag in tags or []:
        if isinstance(tag, dict) and tag.get("key") == "buildset":
            val = str(tag.get("value", ""))
            match = re.search(r"^patch/gerrit/[^/]+/\d+/(\d+)$", val)
            if match:
                return int(match.group(1))
    return default_ps


def _is_experimental_build(build):
    """Returns True if build tags mark the build as cq_experimental=true."""
    for tag in build.get("tags") or []:
        if (
            isinstance(tag, dict)
            and tag.get("key") == "cq_experimental"
            and str(tag.get("value", "")).lower() == "true"
        ):
            return True
    return False


def _deduplicate_latest_builds(builds):
    """Deduplicates builds per builder, keeping the newest Buildbucket ID."""
    latest_by_builder = {}
    for b in builds:
        builder_name = b.get("builder", {}).get("builder")
        if not builder_name:
            continue
        build_id = int(b["id"])
        # Smaller Buildbucket ID means newer build (inverted timestamp).
        if builder_name not in latest_by_builder or build_id < int(
            latest_by_builder[builder_name]["id"]
        ):
            latest_by_builder[builder_name] = b
    return list(latest_by_builder.values())


def fetch_try_results(cl_number, patchset=None, retries=3, retry_delay=1.0):
    """Fetches tryjob builds via `git cl try-results --json -`."""
    cmd = ["git", "cl", "try-results", "-i", str(cl_number), "--json", "-"]
    if patchset is not None:
        cmd.extend(["-p", str(patchset)])

    last_err = None
    for attempt in range(max(1, retries)):
        try:
            raw = subprocess.check_output(cmd).decode("utf-8").strip()
            if not raw:
                raise ValueError("Empty git cl try-results output")
            builds = json.loads(raw)
            if not isinstance(builds, list):
                raise ValueError("Expected JSON list from git cl try-results")
            return builds
        except (
            subprocess.SubprocessError,
            OSError,
            ValueError,
            TypeError,
        ) as err:
            last_err = err
            if attempt < retries - 1 and retry_delay > 0:
                time.sleep(retry_delay * (attempt + 1))

    print(
        f"Error fetching try-results for CL {cl_number}: {last_err}",
        file=sys.stderr,
    )
    return None


def find_cl_builds(
    cl_number,
    patchset=None,
    host=None,
    show_all=False,
    latest_only=True,
    include_experimental=False,
    return_none_on_error=False,
):
    """Finds Buildbucket builds for a CL number or Gerrit/crrev URL.

    Args:
        cl_number: Numeric CL ID or Gerrit/crrev URL string.
        patchset: Optional explicit patchset number (auto-detected if omitted).
        host: Optional Gerrit host override (retained for API compatibility).
        show_all: If True, include SUCCESS and STARTED builds.
        latest_only: If True, deduplicate retry attempts per builder.
        include_experimental: If True, include cq_experimental=true builds.
        return_none_on_error: If True, return None instead of [] on error.
    """
    del host
    error_return = None if return_none_on_error else []

    _, parsed_cl, parsed_ps = parse_cl_arg(cl_number)
    if not parsed_cl:
        print(f"Error: Invalid CL identifier '{cl_number}'.", file=sys.stderr)
        return error_return
    cl_number = parsed_cl

    explicit_ps = patchset if patchset is not None else parsed_ps
    if explicit_ps is not None:
        try:
            explicit_ps = int(explicit_ps)
            if explicit_ps < 1:
                raise ValueError("patchset must be >= 1")
        except (TypeError, ValueError):
            print(f"Error: Invalid patchset '{explicit_ps}'.", file=sys.stderr)
            return error_return

    raw_builds = fetch_try_results(cl_number, patchset=explicit_ps)
    if raw_builds is None:
        return error_return

    if not raw_builds:
        return []

    if not include_experimental:
        raw_builds = [b for b in raw_builds if not _is_experimental_build(b)]

    if latest_only:
        raw_builds = _deduplicate_latest_builds(raw_builds)

    return [
        {
            "builder": b["builder"]["builder"],
            "status": b["status"],
            "id": b["id"],
            "patchset": _extract_patchset_from_tags(
                b.get("tags"), default_ps=explicit_ps
            ),
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
        help="Optional patchset number (defaults to latest CQ run patchset)",
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
        help=(
            "Include superseded build attempts instead of deduplicating per "
            "builder"
        ),
    )
    parser.add_argument(
        "--include-experimental",
        action="store_true",
        help="Include experimental (cq_experimental=true) builds",
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
        latest_only=not args.all_attempts,
        include_experimental=args.include_experimental,
    )
    print(json.dumps(builds, indent=2))


if __name__ == "__main__":
    main()
