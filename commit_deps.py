#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Analyzes commit ranges and recursively expands DEPS / submodule rolls."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import csv
import dataclasses
import functools
import io
import json
import os
import posixpath
import re
import sys

import gclient_eval
import gclient_paths
import gclient_utils
import subprocess2

NEED_SHELL = sys.platform.startswith("win")
DEPOT_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ZERO_SHA = "0" * 40
_RAW_DIFF_SHA_RE = re.compile(r"(?<!git_revision:)\b[0-9a-fA-F]{40}\b")
_RAW_LINE_RE = re.compile(
    r"^:([0-7]{6}) ([0-7]{6}) ([0-9a-fA-F]+) ([0-9a-fA-F]+) ([A-Z0-9]+)\t(.+)$"
)


class CommitDepsError(Exception):
    """Error raised during DEPS commit analysis."""


@dataclasses.dataclass(frozen=True)
class CommitEntry:
    """Single row in the commit dependency traversal output."""

    path: str
    level: int
    commit: str
    roll: bool = False


@dataclasses.dataclass(frozen=True)
class SubrepoRoll:
    """Represents a dependency roll within a single commit."""

    rel_path: str
    old_rev: str
    new_rev: str


def _run_git(
    repo_dir: str, args: Sequence[str], check: bool = True
) -> str | None:
    """Executes a git command in repo_dir and returns stdout as a string."""
    cmd = ["git", "-C", repo_dir, *args]
    try:
        out = subprocess2.check_output(
            cmd,
            shell=NEED_SHELL,
            stderr=subprocess2.DEVNULL,
        )
        return out.decode("utf-8", "replace")
    except (subprocess2.CalledProcessError, OSError) as exc:
        if check:
            raise CommitDepsError(
                f"Git command failed in {repo_dir}: {' '.join(cmd)}"
            ) from exc
        return None


def _is_git_repo(path: str) -> bool:
    """Returns True if path has a .git directory or gitfile."""
    return os.path.exists(os.path.join(path, ".git"))


def _resolve_candidate_dir(candidate: str) -> str | None:
    """Resolves a directory to a git repository root if valid."""
    if not candidate or not os.path.isdir(candidate):
        return None
    candidate = os.path.abspath(candidate)
    if os.path.isfile(os.path.join(candidate, ".gclient")):
        sol_path = gclient_paths.GetPrimarySolutionPath(candidate)
        if sol_path and os.path.isdir(sol_path):
            candidate = os.path.abspath(sol_path)
    if _is_git_repo(candidate):
        return candidate
    if top := _run_git(
        candidate, ["rev-parse", "--show-toplevel"], check=False
    ):
        top_dir = os.path.abspath(top.strip())
        if os.path.isdir(top_dir):
            return top_dir
    return None


def _subrepo_candidates(sol_dir: str, name: str) -> list[str]:
    """Returns candidate subrepo paths inside a gclient solution directory."""
    return [
        os.path.join(sol_dir, name),
        os.path.join(sol_dir, "third_party", name),
        os.path.join(sol_dir, "third_party", name, "src"),
    ]


def resolve_repo(
    repo_arg: str, cwd: str | None = None
) -> tuple[str, str, str | None]:
    """Resolves --repo (path or short alias) to (repo_dir, display_path, gclient_root)."""
    base_cwd = os.path.abspath(cwd or os.getcwd())
    direct_candidate = os.path.join(base_cwd, os.path.expanduser(repo_arg))
    candidates = [direct_candidate]

    # If repo_arg is a short name like 'chromium' or 'v8', search current
    # gclient workspace and parent workspace directories.
    if not os.path.exists(direct_candidate):
        alias_names = (
            [repo_arg, "chr", "chromium", "chrome", "src"]
            if repo_arg in ("chromium", "chrome")
            else [repo_arg]
        )

        if gclient_root := gclient_paths.FindGclientRoot(base_cwd):
            if repo_arg in ("chromium", "chrome", "src") and (
                chr_src := gclient_paths.GetChromiumSrcPath(base_cwd)
            ):
                candidates.append(chr_src)
            primary_sol = gclient_paths.GetPrimarySolutionPath(base_cwd)
            for name in alias_names:
                candidates.append(os.path.join(gclient_root, name))
                if primary_sol:
                    candidates.extend(_subrepo_candidates(primary_sol, name))

        for root in (
            os.path.dirname(base_cwd),
            os.path.dirname(DEPOT_TOOLS_DIR),
        ):
            if not root or not os.path.isdir(root):
                continue
            for name in alias_names:
                candidates.extend(
                    [
                        os.path.join(root, name),
                        os.path.join(root, name, name),
                        os.path.join(root, name, "src"),
                    ]
                )
            for chr_dir in ("chr", "chromium", "chrome"):
                chr_src_dir = os.path.join(root, chr_dir, "src")
                if os.path.isdir(chr_src_dir):
                    candidates.extend(
                        _subrepo_candidates(chr_src_dir, repo_arg)
                    )

    repo_dir = next(
        (
            resolved
            for cand in candidates
            if (resolved := _resolve_candidate_dir(cand))
        ),
        None,
    )
    if not repo_dir:
        raise CommitDepsError(f"Could not resolve repository: {repo_arg!r}")

    gclient_root = gclient_paths.FindGclientRoot(repo_dir)
    if gclient_root and os.path.abspath(repo_dir) != os.path.abspath(
        gclient_root
    ):
        display_path = os.path.relpath(repo_dir, gclient_root).replace(
            os.sep, "/"
        )
    else:
        display_path = os.path.basename(repo_dir) or "."

    return repo_dir, display_path, gclient_root


def _resolve_commit(
    repo_dir: str, rev: str, allow_fetch: bool = True
) -> str | None:
    """Resolves a revision to a 40-char commit SHA, fetching if needed."""
    verify_cmd = ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"]
    if out := _run_git(repo_dir, verify_cmd, check=False):
        return out.strip()
    if not allow_fetch:
        return None

    for fetch_args in (
        ["fetch", "origin", "--quiet"],
        ["fetch", "origin", rev, "--quiet"],
    ):
        _run_git(repo_dir, fetch_args, check=False)
        if out := _run_git(repo_dir, verify_cmd, check=False):
            return out.strip()
    return None


@functools.lru_cache(maxsize=256)
def _get_blob_content(repo_dir: str, blob_sha: str) -> str | None:
    """Reads a git blob's text content."""
    if not blob_sha or blob_sha == ZERO_SHA:
        return None
    return _run_git(repo_dir, ["cat-file", "blob", blob_sha], check=False)


def _normalize_dep_path(
    dep_key: str,
    use_relative_paths: bool,
    repo_dir: str,
    gclient_root: str | None,
) -> str:
    """Normalizes a DEPS key to a path relative to repo_dir."""
    norm_key = posixpath.normpath(dep_key.replace("\\", "/")).lstrip("/")
    if use_relative_paths:
        return norm_key

    if gclient_root and os.path.abspath(repo_dir) != os.path.abspath(
        gclient_root
    ):
        prefix = (
            os.path.relpath(repo_dir, gclient_root).replace(os.sep, "/") + "/"
        )
        if norm_key.startswith(prefix):
            return norm_key.removeprefix(prefix)

    repo_basename = os.path.basename(os.path.normpath(repo_dir))
    if repo_basename and norm_key.startswith(f"{repo_basename}/"):
        return norm_key.removeprefix(f"{repo_basename}/")

    if norm_key.startswith("src/") and not os.path.exists(
        os.path.join(repo_dir, *norm_key.split("/"))
    ):
        return norm_key.removeprefix("src/")

    return norm_key


@functools.lru_cache(maxsize=256)
def _parse_deps_git_revs(
    repo_dir: str, blob_sha: str, gclient_root: str | None
) -> dict[str, str]:
    """Parses a DEPS blob and returns {rel_dep_path: git_commit_sha}."""
    content = _get_blob_content(repo_dir, blob_sha)
    if not content:
        return {}
    try:
        parsed = gclient_eval.Parse(content, "DEPS")
    except Exception:
        return {}

    if not isinstance(parsed, Mapping):
        return {}

    use_relative_paths = bool(parsed.get("use_relative_paths", False))
    deps_dict = parsed.get("deps", {})
    if not isinstance(deps_dict, Mapping):
        return {}

    git_revs: dict[str, str] = {}
    for dep_key, dep_info in deps_dict.items():
        if (
            not isinstance(dep_info, Mapping)
            or dep_info.get("dep_type", "git") != "git"
            or not isinstance(dep_info.get("url"), str)
        ):
            continue
        rev = gclient_eval.GetRevision(parsed, dep_key)
        if not rev or not gclient_utils.IsFullGitSha(rev):
            continue
        rel_path = _normalize_dep_path(
            dep_key, use_relative_paths, repo_dir, gclient_root
        )
        if rel_path and rel_path != ".":
            git_revs[rel_path] = rev.lower()

    return git_revs


def _deps_blobs_may_have_git_roll(
    repo_dir: str, old_blob: str, new_blob: str
) -> bool:
    """Fast check whether changed lines between two DEPS blobs contain a 40-char SHA."""
    old_content = _get_blob_content(repo_dir, old_blob)
    new_content = _get_blob_content(repo_dir, new_blob)
    if not old_content or not new_content:
        return False
    changed_lines = set(old_content.splitlines()) ^ set(
        new_content.splitlines()
    )
    return any(_RAW_DIFF_SHA_RE.search(line) for line in changed_lines)


def _extract_commit_rolls(
    repo_dir: str,
    raw_lines: Sequence[str],
    gclient_root: str | None,
) -> list[SubrepoRoll]:
    """Extracts all subrepo rolls from a commit's raw diff-tree lines."""
    rolls_by_path: dict[str, SubrepoRoll] = {}
    deps_blob_change: tuple[str, str] | None = None

    for line in raw_lines:
        m = _RAW_LINE_RE.match(line)
        if not m:
            continue
        old_mode, new_mode, old_sha, new_sha, _status, file_path = m.groups()
        old_sha, new_sha = old_sha.lower(), new_sha.lower()
        if old_sha == new_sha or ZERO_SHA in (old_sha, new_sha):
            continue

        if old_mode == "160000" and new_mode == "160000":
            rel_path = posixpath.normpath(file_path.replace("\\", "/"))
            rolls_by_path[rel_path] = SubrepoRoll(rel_path, old_sha, new_sha)
        elif file_path == "DEPS":
            deps_blob_change = (old_sha, new_sha)

    if deps_blob_change and _deps_blobs_may_have_git_roll(
        repo_dir, *deps_blob_change
    ):
        old_revs = _parse_deps_git_revs(
            repo_dir, deps_blob_change[0], gclient_root
        )
        new_revs = _parse_deps_git_revs(
            repo_dir, deps_blob_change[1], gclient_root
        )
        for rel_path, new_rev in new_revs.items():
            old_rev = old_revs.get(rel_path)
            if old_rev and old_rev != new_rev and rel_path not in rolls_by_path:
                rolls_by_path[rel_path] = SubrepoRoll(
                    rel_path, old_rev, new_rev
                )

    return [rolls_by_path[k] for k in sorted(rolls_by_path)]


def _get_commits_with_raw_diffs(
    repo_dir: str,
    start_sha: str,
    end_sha: str,
    include_start: bool = False,
    first_parent: bool = False,
) -> list[tuple[str, list[str]]]:
    """Returns [(commit_sha, [raw_diff_lines])] in oldest-to-newest order."""
    if not include_start and start_sha == end_sha:
        return []

    if include_start:
        has_parent = _run_git(
            repo_dir,
            ["rev-parse", "--verify", "--quiet", f"{start_sha}^^{{commit}}"],
            check=False,
        )
        rev_spec = f"{start_sha}^..{end_sha}" if has_parent else end_sha
    else:
        rev_spec = f"{start_sha}..{end_sha}"

    cmd = [
        "log",
        "--raw",
        "--no-abbrev",
        "--diff-merges=first-parent",
        "--format=COMMIT:%H",
        "--reverse",
        "--topo-order",
        *(["--first-parent"] if first_parent else []),
        rev_spec,
    ]
    output = _run_git(repo_dir, cmd, check=False)
    if not output:
        return []

    commits: list[tuple[str, list[str]]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("COMMIT:"):
            commits.append((line.removeprefix("COMMIT:").strip().lower(), []))
        elif line.startswith(":") and commits:
            commits[-1][1].append(line)

    return commits


def collect_commit_deps(
    repo_dir: str,
    display_path: str,
    start_rev: str,
    end_rev: str,
    gclient_root: str | None = None,
    level: int = 0,
    include_start: bool = False,
    first_parent: bool = False,
    allow_fetch: bool = True,
    max_level: int | None = None,
    _active_stack: set[tuple[str, str, str]] | None = None,
) -> list[CommitEntry]:
    """Recursively collects commits and expands DEPS / submodule rolls."""
    if _active_stack is None:
        _active_stack = set()

    start_sha = _resolve_commit(repo_dir, start_rev, allow_fetch=allow_fetch)
    end_sha = _resolve_commit(repo_dir, end_rev, allow_fetch=allow_fetch)
    if not start_sha or not end_sha:
        if level == 0:
            missing = start_rev if not start_sha else end_rev
            raise CommitDepsError(
                f"Cannot resolve revision {missing!r} in {repo_dir}"
            )
        print(
            f"Warning: skipping {display_path} ({start_rev[:12]}..{end_rev[:12]}): "
            "commits not found locally.",
            file=sys.stderr,
        )
        return []

    stack_key = (os.path.realpath(repo_dir), start_sha, end_sha)
    if stack_key in _active_stack:
        return []

    _active_stack.add(stack_key)
    try:
        commits = _get_commits_with_raw_diffs(
            repo_dir,
            start_sha,
            end_sha,
            include_start=include_start
            or (level == 0 and start_sha == end_sha),
            first_parent=first_parent,
        )
        entries: list[CommitEntry] = []

        for commit_sha, raw_lines in commits:
            rolls = _extract_commit_rolls(repo_dir, raw_lines, gclient_root)
            entries.append(
                CommitEntry(
                    path=display_path,
                    level=level,
                    commit=commit_sha,
                    roll=bool(rolls),
                )
            )

            if max_level is not None and level >= max_level:
                continue

            for roll in rolls:
                subrepo_dir = os.path.join(repo_dir, *roll.rel_path.split("/"))
                if not _is_git_repo(subrepo_dir):
                    continue
                sub_display_path = (
                    f"{display_path}/{roll.rel_path}"
                    if display_path and display_path != "."
                    else roll.rel_path
                )
                entries.extend(
                    collect_commit_deps(
                        repo_dir=subrepo_dir,
                        display_path=sub_display_path,
                        start_rev=roll.old_rev,
                        end_rev=roll.new_rev,
                        gclient_root=gclient_root,
                        level=level + 1,
                        include_start=False,
                        first_parent=first_parent,
                        allow_fetch=allow_fetch,
                        max_level=max_level,
                        _active_stack=_active_stack,
                    )
                )

        return entries
    finally:
        _active_stack.discard(stack_key)


def format_output(entries: Sequence[CommitEntry], fmt: str) -> str:
    """Formats commit entries as table, csv, or json."""
    if fmt == "json":
        return (
            json.dumps([dataclasses.asdict(e) for e in entries], indent=2)
            + "\n"
        )

    headers = ("Path", "Level", "commit hash", "roll")
    rows = [
        (e.path, str(e.level), e.commit, "true" if e.roll else "false")
        for e in entries
    ]

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)
        return buf.getvalue()

    widths = [
        max([len(h), *(len(row[i]) for row in rows)])
        for i, h in enumerate(headers)
    ]

    def _fmt_row(cols: Sequence[str]) -> str:
        cells = " | ".join(f"{col:<{w}}" for col, w in zip(cols, widths))
        return f"| {cells} |"

    out_lines = [
        _fmt_row(headers),
        _fmt_row(["-" * w for w in widths]),
        *(_fmt_row(row) for row in rows),
    ]
    return "\n".join(out_lines) + "\n"


def _parse_range_expr(
    expr: str, repo_dir: str | None = None, allow_fetch: bool = True
) -> tuple[str, str]:
    """Parses a single git log range expression ('A..B', 'A...B', 'A^!', or 'A')."""
    if "..." in expr:
        left, _, right = expr.partition("...")
        left_rev, right_rev = left or "HEAD", right or "HEAD"
        if repo_dir:
            left_sha = _resolve_commit(
                repo_dir, left_rev, allow_fetch=allow_fetch
            )
            right_sha = _resolve_commit(
                repo_dir, right_rev, allow_fetch=allow_fetch
            )
            if left_sha and right_sha:
                if mb := _run_git(
                    repo_dir, ["merge-base", left_sha, right_sha], check=False
                ):
                    return mb.strip(), right_sha
        return left_rev, right_rev

    if ".." in expr:
        left, _, right = expr.partition("..")
        return left or "HEAD", right or "HEAD"

    rev = expr.removesuffix("^!") or "HEAD" if expr.endswith("^!") else expr
    return rev, rev


def resolve_revision_range(
    start_opt: str | None,
    end_opt: str | None,
    positional_revs: Sequence[str],
    repo_dir: str | None = None,
    allow_fetch: bool = True,
) -> tuple[str, str]:
    """Resolves --start/--end flags or positional git log range arguments."""
    if positional_revs:
        if start_opt is not None or end_opt is not None:
            raise CommitDepsError(
                "Cannot combine positional revision arguments with --start or --end."
            )
        if len(positional_revs) == 1:
            return _parse_range_expr(
                positional_revs[0], repo_dir=repo_dir, allow_fetch=allow_fetch
            )
        if len(positional_revs) == 2:
            return positional_revs[0], positional_revs[1]
        raise CommitDepsError(
            f"Expected 1 or 2 positional revision arguments, got {len(positional_revs)}."
        )

    if not start_opt:
        raise CommitDepsError(
            "Must specify a revision range (e.g. 'AAA..BBBB' or --start=AAA [--end=BBBB])."
        )

    if end_opt is None and (".." in start_opt or start_opt.endswith("^!")):
        return _parse_range_expr(
            start_opt, repo_dir=repo_dir, allow_fetch=allow_fetch
        )

    return start_opt, end_opt or "HEAD"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Collects commit hashes for a given range and recursively expands "
            "DEPS / submodule rolls."
        )
    )
    parser.add_argument(
        "revisions",
        nargs="*",
        help=(
            "Git revision range (e.g. 'AAA..BBBB', 'AAA...BBBB', 'AAA BBBB', "
            "or a single commit 'AAA')."
        ),
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Repository path or name (e.g. 'chromium', 'v8', or path to checkout).",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="Start commit revision (exclusive unless --include-start or start == end).",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="End commit revision (inclusive, default: HEAD).",
    )
    parser.add_argument(
        "--format",
        choices=("table", "csv", "json"),
        default="table",
        help="Output format: table (default), csv, or json.",
    )
    parser.add_argument(
        "--include-start",
        action="store_true",
        help="Include the --start commit itself in the top-level range.",
    )
    parser.add_argument(
        "--first-parent",
        action="store_true",
        help="Follow only the first parent on merge commits.",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Do not run 'git fetch' when commits are missing locally.",
    )
    parser.add_argument(
        "--max-level",
        type=int,
        default=None,
        help="Maximum recursion depth (0 = top-level only).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        repo_dir, display_path, gclient_root = resolve_repo(args.repo)
        allow_fetch = not args.no_fetch
        start_rev, end_rev = resolve_revision_range(
            args.start,
            args.end,
            args.revisions,
            repo_dir=repo_dir,
            allow_fetch=allow_fetch,
        )
        entries = collect_commit_deps(
            repo_dir=repo_dir,
            display_path=display_path,
            start_rev=start_rev,
            end_rev=end_rev,
            gclient_root=gclient_root,
            level=0,
            include_start=args.include_start,
            first_parent=args.first_parent,
            allow_fetch=allow_fetch,
            max_level=args.max_level,
        )
        sys.stdout.write(format_output(entries, args.format))
        return 0
    except CommitDepsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
