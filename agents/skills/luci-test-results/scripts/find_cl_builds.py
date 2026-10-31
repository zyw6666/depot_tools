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

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    ),
)

import gerrit_util
import git_cl

DEFAULT_GERRIT_HOST = "chromium-review.googlesource.com"
INTERNAL_GERRIT_HOST = "chrome-internal-review.googlesource.com"


def _normalize_gerrit_host(host):
    """Normalizes a Gerrit host (.git.corp.google.com -> .googlesource.com)."""
    if not host:
        return DEFAULT_GERRIT_HOST
    return host.replace(".git.corp.google.com", ".googlesource.com")


def _parse_cl_arg_with_project(cl_arg):
    """Parses a CL number or Gerrit URL into (host, project, cl_number, ps)."""
    if cl_arg is None:
        return None, None, None, None

    text = str(cl_arg).strip()
    if text.isdigit():
        return None, None, text, None

    text = _normalize_gerrit_host(text)

    # Match full Gerrit or crrev URLs:
    # - https://crrev.com/c/<cl>[/[<base>..]<ps>] or /i/<cl>[/[<base>..]<ps>]
    # - https://<host>/[#/]c/[<project>/+/]<cl>[/[<base>..]<ps>]
    gerrit_match = re.search(
        r"(?:https?://)?([a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+)/"
        r"(?:#/)?(?:([ci])/(?:([^+]+)/\+/)?|\+/)?"
        r"(\d+)(?:/(?:\d+\.\.)?(\d+))?",
        text,
    )
    if gerrit_match:
        host = gerrit_match.group(1)
        short_prefix = gerrit_match.group(2)
        project = gerrit_match.group(3)
        cl_num = gerrit_match.group(4)
        ps = int(gerrit_match.group(5)) if gerrit_match.group(5) else None
        if host in ("crrev.com", "www.crrev.com"):
            if short_prefix not in ("c", "i") or project is not None:
                return None, None, None, None
            host = INTERNAL_GERRIT_HOST if short_prefix == "i" else None
        return host, project, cl_num, ps

    return None, None, None, None


def parse_cl_arg(cl_arg):
    """Parses a CL number or Gerrit URL into (host, cl_number, patchset)."""
    host, _, cl_num, ps = _parse_cl_arg_with_project(cl_arg)
    return host, cl_num, ps


def get_current_branch_cl():
    """Detects the active git branch Gerrit CL via `git cl issue`."""
    try:
        output = (
            subprocess.check_output(
                ["git", "cl", "issue"], stderr=subprocess.DEVNULL
            )
            .decode("utf-8")
            .strip()
        )
        match = re.search(
            r"Issue number:\s*(\d+)(?:\s*\((https?://[^\s)]+)\))?", output
        )
        if match:
            return match.group(2) or match.group(1)
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


def fetch_try_results(
    cl_number,
    patchset=None,
    host=None,
    project=None,
    retries=3,
    retry_delay=1.0,
):
    """Fetches tryjob builds in-process via git_cl Changelist helpers."""
    effective_host = _normalize_gerrit_host(host) if host else None
    last_err = None
    for attempt in range(max(1, retries)):
        try:
            cl = git_cl.Changelist(
                issue=int(cl_number),
                codereview_host=effective_host,
                gerrit_project=project,
            )
            resolved_ps = patchset or cl.GetMostRecentCqPatchset()
            if not resolved_ps:
                raise ValueError(
                    f"Could not resolve patchset for CL {cl_number}"
                )
            builds = git_cl._fetch_tryjobs(
                cl, git_cl.DEFAULT_BUILDBUCKET_HOST, resolved_ps
            )
            if not isinstance(builds, list):
                raise ValueError("Expected list of builds from _fetch_tryjobs")
            return builds
        except (
            gerrit_util.GerritError,
            git_cl.BuildbucketResponseException,
            subprocess.SubprocessError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
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

    parsed_host, parsed_project, parsed_cl, parsed_ps = (
        _parse_cl_arg_with_project(cl_number)
    )
    if not parsed_cl:
        print(f"Error: Invalid CL identifier '{cl_number}'.", file=sys.stderr)
        return error_return
    cl_number = parsed_cl
    raw_host = host or parsed_host
    effective_host = _normalize_gerrit_host(raw_host) if raw_host else None

    explicit_ps = patchset if patchset is not None else parsed_ps
    if explicit_ps is not None:
        try:
            explicit_ps = int(explicit_ps)
            if explicit_ps < 1:
                raise ValueError("patchset must be >= 1")
        except (TypeError, ValueError):
            print(f"Error: Invalid patchset '{explicit_ps}'.", file=sys.stderr)
            return error_return

    raw_builds = fetch_try_results(
        cl_number,
        patchset=explicit_ps,
        host=effective_host,
        project=parsed_project,
    )
    if raw_builds is None:
        return error_return

    if not raw_builds:
        return []

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
