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
import enum
import io
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import sys

import gclient_eval
import gclient_paths
import gclient_utils
import subprocess2

NEED_SHELL = sys.platform.startswith("win")
DEPOT_TOOLS_DIR = Path(__file__).resolve().parent
ZERO_SHA = "0" * 40
_RAW_DIFF_SHA_RE = re.compile(r"(?<!git_revision:)\b[0-9a-fA-F]{40}\b")
_RAW_LINE_RE = re.compile(
    r"^:([0-7]{6}) ([0-7]{6}) ([0-9a-fA-F]+) ([0-9a-fA-F]+) ([A-Z0-9]+)\t(.+)$"
)


class CommitDepsError(Exception):
    """Error raised during DEPS commit analysis."""


class OutputFormat(enum.StrEnum):
    """Supported CLI output formats."""

    TABLE = "table"
    CSV = "csv"
    JSON = "json"


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


class CommitDepsCollector:
    """Resolves repos, parses DEPS/gitlink rolls, and collects commits."""

    def __init__(
        self,
        repo: str | Path = ".",
        *,
        cwd: str | Path | None = None,
        first_parent: bool = False,
        allow_fetch: bool = True,
        max_level: int | None = None,
    ) -> None:
        self.repo_dir, self.display_path, self.gclient_root = (
            self._resolve_repo(repo, cwd=cwd)
        )
        self.first_parent = first_parent
        self.allow_fetch = allow_fetch
        self.max_level = max_level
        self._active_stack: set[tuple[Path, str, str]] = set()
        self._blob_cache: dict[tuple[Path, str], str | None] = {}
        self._deps_cache: dict[tuple[Path, str], dict[str, str]] = {}

    @staticmethod
    def _run_git(
        repo_dir: Path, args: Sequence[str], *, check: bool = True
    ) -> str | None:
        """Executes a git command in repo_dir and returns stdout as a string."""
        cmd = ["git", "-C", str(repo_dir), *args]
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

    @staticmethod
    def _is_git_repo(path: Path) -> bool:
        """Returns True if path has a .git directory or gitfile."""
        return (path / ".git").exists()

    @classmethod
    def _resolve_candidate_dir(cls, candidate: Path) -> Path | None:
        """Resolves a directory to a git repository root if valid."""
        if not candidate.is_dir():
            return None
        candidate = candidate.resolve()
        if (candidate / ".gclient").is_file():
            sol_path = gclient_paths.GetPrimarySolutionPath(str(candidate))
            if sol_path and Path(sol_path).is_dir():
                candidate = Path(sol_path).resolve()
        if cls._is_git_repo(candidate):
            return candidate
        if top := cls._run_git(
            candidate, ["rev-parse", "--show-toplevel"], check=False
        ):
            top_dir = Path(top.strip()).resolve()
            if top_dir.is_dir():
                return top_dir
        return None

    @staticmethod
    def _subrepo_candidates(sol_dir: Path, name: str) -> list[Path]:
        """Returns candidate subrepo paths inside a gclient solution dir."""
        return [
            sol_dir / name,
            sol_dir / "third_party" / name,
            sol_dir / "third_party" / name / "src",
        ]

    @classmethod
    def _resolve_repo(
        cls, repo_arg: str | Path, cwd: str | Path | None = None
    ) -> tuple[Path, str, Path | None]:
        """Resolves --repo to (repo_dir, display_path, gclient_root)."""
        base_cwd = Path(cwd or Path.cwd()).resolve()
        direct_candidate = base_cwd / Path(repo_arg).expanduser()
        candidates = [direct_candidate]

        # If repo_arg is a short name like 'chromium' or 'v8', search current
        # gclient workspace and parent workspace directories.
        repo_str = str(repo_arg)
        if not direct_candidate.exists():
            alias_names = (
                [repo_str, "chr", "chromium", "chrome", "src"]
                if repo_str in ("chromium", "chrome")
                else [repo_str]
            )

            if gclient_root_str := gclient_paths.FindGclientRoot(str(base_cwd)):
                gclient_root_dir = Path(gclient_root_str)
                if repo_str in ("chromium", "chrome", "src") and (
                    chr_src := gclient_paths.GetChromiumSrcPath(str(base_cwd))
                ):
                    candidates.append(Path(chr_src))
                primary_sol = gclient_paths.GetPrimarySolutionPath(
                    str(base_cwd)
                )
                for name in alias_names:
                    candidates.append(gclient_root_dir / name)
                    if primary_sol:
                        candidates.extend(
                            cls._subrepo_candidates(Path(primary_sol), name)
                        )

            for root in (base_cwd.parent, DEPOT_TOOLS_DIR.parent):
                if not root.is_dir():
                    continue
                for name in alias_names:
                    candidates.extend(
                        [
                            root / name,
                            root / name / name,
                            root / name / "src",
                        ]
                    )
                for chr_dir in ("chr", "chromium", "chrome"):
                    chr_src_dir = root / chr_dir / "src"
                    if chr_src_dir.is_dir():
                        candidates.extend(
                            cls._subrepo_candidates(chr_src_dir, repo_str)
                        )

        repo_dir = next(
            (
                resolved
                for cand in candidates
                if (resolved := cls._resolve_candidate_dir(cand))
            ),
            None,
        )
        if not repo_dir:
            raise CommitDepsError(f"Could not resolve repository: {repo_arg!r}")

        gclient_root_found = gclient_paths.FindGclientRoot(str(repo_dir))
        gclient_root = (
            Path(gclient_root_found).resolve() if gclient_root_found else None
        )
        if gclient_root and repo_dir != gclient_root:
            display_path = repo_dir.relative_to(gclient_root).as_posix()
        else:
            display_path = repo_dir.name or "."

        return repo_dir, display_path, gclient_root

    def _resolve_commit(self, repo_dir: Path, rev: str) -> str | None:
        """Resolves a revision to a 40-char commit SHA, fetching if needed."""
        verify_cmd = ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"]
        if out := self._run_git(repo_dir, verify_cmd, check=False):
            return out.strip()
        if not self.allow_fetch:
            return None

        for fetch_args in (
            ["fetch", "origin", "--quiet"],
            ["fetch", "origin", rev, "--quiet"],
        ):
            self._run_git(repo_dir, fetch_args, check=False)
            if out := self._run_git(repo_dir, verify_cmd, check=False):
                return out.strip()
        return None

    def _get_blob_content(self, repo_dir: Path, blob_sha: str) -> str | None:
        """Reads and caches a git blob's text content."""
        if not blob_sha or blob_sha == ZERO_SHA:
            return None
        key = (repo_dir, blob_sha)
        if key not in self._blob_cache:
            self._blob_cache[key] = self._run_git(
                repo_dir, ["cat-file", "blob", blob_sha], check=False
            )
        return self._blob_cache[key]

    def _normalize_dep_path(
        self,
        repo_dir: Path,
        dep_key: str,
        use_relative_paths: bool,
    ) -> str:
        """Normalizes a DEPS key to a path relative to repo_dir."""
        norm_key = (
            PurePosixPath(dep_key.replace("\\", "/")).as_posix().lstrip("/")
        )
        if use_relative_paths:
            return norm_key

        if self.gclient_root and repo_dir != self.gclient_root:
            prefix = f"{repo_dir.relative_to(self.gclient_root).as_posix()}/"
            if norm_key.startswith(prefix):
                return norm_key.removeprefix(prefix)

        repo_basename = repo_dir.name
        if repo_basename and norm_key.startswith(f"{repo_basename}/"):
            return norm_key.removeprefix(f"{repo_basename}/")

        if norm_key.startswith("src/") and not (repo_dir / norm_key).exists():
            return norm_key.removeprefix("src/")

        return norm_key

    def _parse_deps_git_revs(
        self, repo_dir: Path, blob_sha: str
    ) -> dict[str, str]:
        """Parses a DEPS blob and returns {rel_dep_path: git_commit_sha}."""
        key = (repo_dir, blob_sha)
        if key in self._deps_cache:
            return self._deps_cache[key]

        content = self._get_blob_content(repo_dir, blob_sha)
        if not content:
            self._deps_cache[key] = {}
            return {}
        try:
            parsed = gclient_eval.Parse(content, "DEPS")
        except Exception:
            self._deps_cache[key] = {}
            return {}

        if not isinstance(parsed, Mapping):
            self._deps_cache[key] = {}
            return {}

        use_relative_paths = bool(parsed.get("use_relative_paths", False))
        deps_dict = parsed.get("deps", {})
        if not isinstance(deps_dict, Mapping):
            self._deps_cache[key] = {}
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
            rel_path = self._normalize_dep_path(
                repo_dir, dep_key, use_relative_paths
            )
            if rel_path and rel_path != ".":
                git_revs[rel_path] = rev.lower()

        self._deps_cache[key] = git_revs
        return git_revs

    def _deps_blobs_may_have_git_roll(
        self, repo_dir: Path, old_blob: str, new_blob: str
    ) -> bool:
        """Checks if changed lines between two DEPS blobs contain a SHA."""
        old_content = self._get_blob_content(repo_dir, old_blob)
        new_content = self._get_blob_content(repo_dir, new_blob)
        if not old_content or not new_content:
            return False
        changed_lines = set(old_content.splitlines()) ^ set(
            new_content.splitlines()
        )
        return any(_RAW_DIFF_SHA_RE.search(line) for line in changed_lines)

    def _extract_commit_rolls(
        self, repo_dir: Path, raw_lines: Sequence[str]
    ) -> list[SubrepoRoll]:
        """Extracts all subrepo rolls from a commit's raw diff-tree lines."""
        rolls_by_path: dict[str, SubrepoRoll] = {}
        deps_blob_change: tuple[str, str] | None = None

        for line in raw_lines:
            match = _RAW_LINE_RE.match(line)
            if not match:
                continue
            old_mode, new_mode, old_sha, new_sha, _status, file_path = (
                match.groups()
            )
            old_sha, new_sha = old_sha.lower(), new_sha.lower()
            if old_sha == new_sha or ZERO_SHA in (old_sha, new_sha):
                continue

            if old_mode == "160000" and new_mode == "160000":
                rel_path = PurePosixPath(
                    file_path.replace("\\", "/")
                ).as_posix()
                rolls_by_path[rel_path] = SubrepoRoll(
                    rel_path, old_sha, new_sha
                )
            elif file_path == "DEPS":
                deps_blob_change = (old_sha, new_sha)

        if deps_blob_change and self._deps_blobs_may_have_git_roll(
            repo_dir, *deps_blob_change
        ):
            old_revs = self._parse_deps_git_revs(repo_dir, deps_blob_change[0])
            new_revs = self._parse_deps_git_revs(repo_dir, deps_blob_change[1])
            for rel_path, new_rev in new_revs.items():
                old_rev = old_revs.get(rel_path)
                if (
                    old_rev
                    and old_rev != new_rev
                    and rel_path not in rolls_by_path
                ):
                    rolls_by_path[rel_path] = SubrepoRoll(
                        rel_path, old_rev, new_rev
                    )

        return [rolls_by_path[k] for k in sorted(rolls_by_path)]

    def _get_commits_with_raw_diffs(
        self,
        repo_dir: Path,
        start_sha: str,
        end_sha: str,
        *,
        include_start: bool = False,
    ) -> list[tuple[str, list[str]]]:
        """Returns [(commit_sha, [raw_diff_lines])] oldest-to-newest."""
        if not include_start and start_sha == end_sha:
            return []

        if include_start:
            has_parent = self._run_git(
                repo_dir,
                [
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"{start_sha}^^{{commit}}",
                ],
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
            *(["--first-parent"] if self.first_parent else []),
            rev_spec,
        ]
        output = self._run_git(repo_dir, cmd, check=False)
        if not output:
            return []

        commits: list[tuple[str, list[str]]] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if line.startswith("COMMIT:"):
                commits.append(
                    (line.removeprefix("COMMIT:").strip().lower(), [])
                )
            elif line.startswith(":") and commits:
                commits[-1][1].append(line)

        return commits

    def _parse_range_expr(self, expr: str) -> tuple[str, str]:
        """Parses a git range expression ('A..B', 'A...B', 'A^!', or 'A')."""
        if "..." in expr:
            left, _, right = expr.partition("...")
            left_rev, right_rev = left or "HEAD", right or "HEAD"
            left_sha = self._resolve_commit(self.repo_dir, left_rev)
            right_sha = self._resolve_commit(self.repo_dir, right_rev)
            if left_sha and right_sha:
                if merge_base := self._run_git(
                    self.repo_dir,
                    ["merge-base", left_sha, right_sha],
                    check=False,
                ):
                    return merge_base.strip(), right_sha
            return left_rev, right_rev

        if ".." in expr:
            left, _, right = expr.partition("..")
            return left or "HEAD", right or "HEAD"

        rev = expr.removesuffix("^!") or "HEAD" if expr.endswith("^!") else expr
        return rev, rev

    def resolve_revision_range(
        self,
        start_opt: str | None = None,
        end_opt: str | None = None,
        positional_revs: Sequence[str] = (),
    ) -> tuple[str, str]:
        """Resolves --start/--end flags or positional git range arguments."""
        if positional_revs:
            if start_opt is not None or end_opt is not None:
                raise CommitDepsError(
                    "Cannot combine positional revisions with --start or --end."
                )
            if len(positional_revs) == 1:
                return self._parse_range_expr(positional_revs[0])
            if len(positional_revs) == 2:
                return positional_revs[0], positional_revs[1]
            raise CommitDepsError(
                "Expected 1 or 2 positional revision arguments, "
                f"got {len(positional_revs)}."
            )

        if not start_opt:
            raise CommitDepsError(
                "Must specify a revision range "
                "(e.g. 'AAA..BBBB' or --start=AAA [--end=BBBB])."
            )

        if end_opt is None and (".." in start_opt or start_opt.endswith("^!")):
            return self._parse_range_expr(start_opt)

        return start_opt, end_opt or "HEAD"

    def collect_range(
        self,
        start_opt: str | None = None,
        end_opt: str | None = None,
        positional_revs: Sequence[str] = (),
        *,
        include_start: bool = False,
    ) -> list[CommitEntry]:
        """Resolves revision arguments and collects all commit entries."""
        start_rev, end_rev = self.resolve_revision_range(
            start_opt, end_opt, positional_revs
        )
        return self.collect(start_rev, end_rev, include_start=include_start)

    def collect(
        self,
        start_rev: str,
        end_rev: str,
        *,
        include_start: bool = False,
    ) -> list[CommitEntry]:
        """Recursively collects commits from the root repository."""
        return self._collect_repo(
            repo_dir=self.repo_dir,
            display_path=self.display_path,
            start_rev=start_rev,
            end_rev=end_rev,
            level=0,
            include_start=include_start,
        )

    def _collect_repo(
        self,
        repo_dir: Path,
        display_path: str,
        start_rev: str,
        end_rev: str,
        *,
        level: int = 0,
        include_start: bool = False,
    ) -> list[CommitEntry]:
        """Recursively collects commits for a repository and its rolls."""
        repo_path = repo_dir.resolve()
        start_sha = self._resolve_commit(repo_path, start_rev)
        end_sha = self._resolve_commit(repo_path, end_rev)
        if not start_sha or not end_sha:
            if level == 0:
                missing = start_rev if not start_sha else end_rev
                raise CommitDepsError(
                    f"Cannot resolve revision {missing!r} in {repo_path}"
                )
            print(
                f"Warning: skipping {display_path} "
                f"({start_rev[:12]}..{end_rev[:12]}): "
                "commits not found locally.",
                file=sys.stderr,
            )
            return []

        stack_key = (repo_path, start_sha, end_sha)
        if stack_key in self._active_stack:
            return []

        self._active_stack.add(stack_key)
        try:
            commits = self._get_commits_with_raw_diffs(
                repo_path,
                start_sha,
                end_sha,
                include_start=include_start
                or (level == 0 and start_sha == end_sha),
            )
            entries: list[CommitEntry] = []

            for commit_sha, raw_lines in commits:
                rolls = self._extract_commit_rolls(repo_path, raw_lines)
                entries.append(
                    CommitEntry(
                        path=display_path,
                        level=level,
                        commit=commit_sha,
                        roll=bool(rolls),
                    )
                )

                if self.max_level is not None and level >= self.max_level:
                    continue

                for roll in rolls:
                    subrepo_dir = repo_path / roll.rel_path
                    if not self._is_git_repo(subrepo_dir):
                        continue
                    sub_display_path = (
                        f"{display_path}/{roll.rel_path}"
                        if display_path and display_path != "."
                        else roll.rel_path
                    )
                    entries.extend(
                        self._collect_repo(
                            repo_dir=subrepo_dir,
                            display_path=sub_display_path,
                            start_rev=roll.old_rev,
                            end_rev=roll.new_rev,
                            level=level + 1,
                        )
                    )

            return entries
        finally:
            self._active_stack.discard(stack_key)


def format_output(
    entries: Sequence[CommitEntry], fmt: OutputFormat | str
) -> str:
    """Formats commit entries as table, csv, or json."""
    if fmt == OutputFormat.JSON:
        return (
            json.dumps([dataclasses.asdict(e) for e in entries], indent=2)
            + "\n"
        )

    headers = ("Path", "Level", "commit hash", "roll")
    rows = [
        (e.path, str(e.level), e.commit, "true" if e.roll else "false")
        for e in entries
    ]

    if fmt == OutputFormat.CSV:
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


def _parse_rev_arg(value: str) -> str:
    """Validates and normalizes a revision or range CLI argument."""
    cleaned = value.strip()
    if not cleaned:
        raise argparse.ArgumentTypeError("Revision must not be empty.")
    if cleaned.startswith("-"):
        raise argparse.ArgumentTypeError(
            f"Invalid revision {value!r}: must not start with '-'."
        )
    if any(ch.isspace() or not ch.isprintable() for ch in cleaned):
        raise argparse.ArgumentTypeError(
            f"Invalid revision {value!r}: must not contain whitespace or "
            "control characters."
        )
    return cleaned


def _parse_repo_arg(value: str) -> str:
    """Validates and normalizes a --repo CLI argument."""
    cleaned = value.strip()
    if not cleaned:
        raise argparse.ArgumentTypeError("Repository path must not be empty.")
    if cleaned.startswith("-"):
        raise argparse.ArgumentTypeError(
            f"Invalid repository {value!r}: must not start with '-'."
        )
    return cleaned


def _parse_non_negative_int(value: str) -> int:
    """Parses and validates a non-negative integer CLI argument."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid integer value: {value!r}"
        ) from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(
            f"Must be a non-negative integer, got {parsed}."
        )
    return parsed


def _parse_format_arg(value: str) -> OutputFormat:
    """Parses and validates a --format CLI argument."""
    cleaned = value.strip().lower()
    try:
        return OutputFormat(cleaned)
    except ValueError as exc:
        valid = ", ".join(fmt.value for fmt in OutputFormat)
        raise argparse.ArgumentTypeError(
            f"Invalid format {value!r}: choose from {valid}."
        ) from exc


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
        type=_parse_rev_arg,
        help=(
            "Git revision range (e.g. 'AAA..BBBB', 'AAA...BBBB', 'AAA BBBB', "
            "or a single commit 'AAA')."
        ),
    )
    parser.add_argument(
        "--repo",
        type=_parse_repo_arg,
        default=".",
        help=(
            "Repository path or name "
            "(e.g. 'chromium', 'v8', or path to checkout)."
        ),
    )
    parser.add_argument(
        "--start",
        type=_parse_rev_arg,
        default=None,
        help=(
            "Start commit revision "
            "(exclusive unless --include-start or start == end)."
        ),
    )
    parser.add_argument(
        "--end",
        type=_parse_rev_arg,
        default=None,
        help="End commit revision (inclusive, default: HEAD).",
    )
    parser.add_argument(
        "--format",
        type=_parse_format_arg,
        choices=tuple(OutputFormat),
        default=OutputFormat.TABLE,
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
        type=_parse_non_negative_int,
        default=None,
        help="Maximum recursion depth (0 = top-level only).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for commit_deps."""
    args = parse_args(argv)
    try:
        collector = CommitDepsCollector(
            args.repo,
            first_parent=args.first_parent,
            allow_fetch=not args.no_fetch,
            max_level=args.max_level,
        )
        entries = collector.collect_range(
            args.start,
            args.end,
            args.revisions,
            include_start=args.include_start,
        )
        sys.stdout.write(format_output(entries, args.format))
        return 0
    except CommitDepsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
