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
import subprocess
import sys
from typing import Any, NamedTuple

import gclient_eval
import gclient_paths
import gclient_utils

NEED_SHELL = sys.platform.startswith("win")
ZERO_SHA = "0" * 40
_COMMIT_PREFIX = "COMMIT:"
_GITLINK_MODE = "160000"
_DEPS_FILE = "DEPS"
_GIT_SHA_RE = re.compile(r"(?<!git_revision:)\b[0-9a-fA-F]{40}\b")
_DIFF_TREE_LINE_RE = re.compile(
    r"^:([0-7]{6}) ([0-7]{6}) ([0-9a-fA-F]+) ([0-9a-fA-F]+) ([A-Z0-9]+)\t(.+)$"
)


class CommitDepsError(Exception):
    pass


class RevisionRange(NamedTuple):
    start: str
    end: str

    @classmethod
    def from_args(
        cls,
        start: str | None = None,
        end: str | None = None,
        revisions: Sequence[str] = (),
    ) -> RevisionRange:
        if revisions:
            return cls._from_positional(start, end, revisions)
        return cls._from_flags(start, end)

    @classmethod
    def _from_positional(
        cls,
        start: str | None,
        end: str | None,
        revisions: Sequence[str],
    ) -> RevisionRange:
        if start is not None or end is not None:
            raise CommitDepsError(
                "Cannot combine positional revisions with --start or --end."
            )
        if len(revisions) == 1:
            return cls.parse(revisions[0])
        if len(revisions) == 2:
            return cls(revisions[0], revisions[1])
        raise CommitDepsError(
            "Expected 1 or 2 positional revision arguments, "
            f"got {len(revisions)}."
        )

    @classmethod
    def _from_flags(cls, start: str | None, end: str | None) -> RevisionRange:
        if not start:
            raise CommitDepsError(
                "Must specify a revision range "
                "(e.g. 'AAA..BBBB' or --start=AAA [--end=BBBB])."
            )
        if end is None and (".." in start or start.endswith("^!")):
            return cls.parse(start)
        return cls(start, end or "HEAD")

    @classmethod
    def parse(cls, expr: str) -> RevisionRange:
        for sep in ("...", ".."):
            if sep in expr:
                left, _, right = expr.partition(sep)
                return cls(left or "HEAD", right or "HEAD")
        rev = expr.removesuffix("^!") or "HEAD" if expr.endswith("^!") else expr
        return cls(rev, rev)


class DiffTreeEntry(NamedTuple):
    old_mode: str
    new_mode: str
    old_sha: str
    new_sha: str
    file_path: str

    @classmethod
    def parse(cls, line: str) -> DiffTreeEntry | None:
        match = _DIFF_TREE_LINE_RE.match(line)
        if not match:
            return None
        old_mode, new_mode, old_sha, new_sha, _status, file_path = (
            match.groups()
        )
        old_sha, new_sha = old_sha.lower(), new_sha.lower()
        if old_sha == new_sha or ZERO_SHA in (old_sha, new_sha):
            return None
        return cls(old_mode, new_mode, old_sha, new_sha, file_path)


@dataclasses.dataclass(frozen=True)
class CommitEntry:
    path: str
    level: int
    commit: str
    roll: bool = False

    @classmethod
    def build_sync_commands(cls, entries: Sequence[CommitEntry]) -> list[str]:
        ancestors: list[CommitEntry] = []
        commands: list[str] = []
        for entry in entries:
            ancestors = [*ancestors[: entry.level], entry]
            rev_flags = " ".join(
                f"--revision {item.path}@{item.commit}" for item in ancestors
            )
            commands.append(f"gclient sync {rev_flags}")
        return commands


@dataclasses.dataclass(frozen=True)
class SubrepoRoll:
    rel_path: str
    old_rev: str
    new_rev: str


class OutputFormat(enum.StrEnum):
    TABLE = "table"
    CSV = "csv"
    JSON = "json"

    def format_entries(
        self,
        entries: Sequence[CommitEntry],
        *,
        sync_command: bool = False,
    ) -> str:
        sync_cmds = (
            CommitEntry.build_sync_commands(entries) if sync_command else []
        )
        if self is OutputFormat.JSON:
            return self._format_json(entries, sync_cmds)

        headers: tuple[str, ...] = ("Path", "Level", "commit hash", "roll")
        if sync_command:
            headers = (*headers, "sync command")
        rows = [
            (
                entry.path,
                str(entry.level),
                entry.commit,
                "true" if entry.roll else "false",
                *([sync_cmds[i]] if sync_command else []),
            )
            for i, entry in enumerate(entries)
        ]
        if self is OutputFormat.CSV:
            return self._format_csv(headers, rows)
        return self._format_table(headers, rows)

    def _format_json(
        self, entries: Sequence[CommitEntry], sync_cmds: Sequence[str]
    ) -> str:
        items = [dataclasses.asdict(entry) for entry in entries]
        for item, cmd in zip(items, sync_cmds):
            item["sync_command"] = cmd
        return json.dumps(items, indent=2) + "\n"

    def _format_csv(
        self, headers: Sequence[str], rows: Sequence[Sequence[str]]
    ) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)
        return buf.getvalue()

    def _format_table(
        self, headers: Sequence[str], rows: Sequence[Sequence[str]]
    ) -> str:
        widths = [
            max([len(header), *(len(row[i]) for row in rows)])
            for i, header in enumerate(headers)
        ]
        out_lines = [
            self._format_table_row(headers, widths),
            self._format_table_row(["-" * width for width in widths], widths),
            *(self._format_table_row(row, widths) for row in rows),
        ]
        return "\n".join(out_lines) + "\n"

    def _format_table_row(
        self, cols: Sequence[str], widths: Sequence[int]
    ) -> str:
        cells = " | ".join(
            f"{col:<{width}}" for col, width in zip(cols, widths)
        )
        return f"| {cells} |"


class InputParser:
    @classmethod
    def parse_args(
        cls, argv: Sequence[str] | None = None
    ) -> argparse.Namespace:
        return cls()._build_parser().parse_args(argv)

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description=(
                "Collects commit hashes for a given range and recursively "
                "expands DEPS / submodule rolls."
            )
        )
        parser.add_argument(
            "revisions",
            nargs="*",
            metavar="REV",
            type=self._parse_rev,
            help=(
                "Git revision range (e.g. 'AAA..BBBB', 'AAA...BBBB', "
                "'AAA BBBB', or a single commit 'AAA')."
            ),
        )
        parser.add_argument(
            "--repo",
            metavar="PATH",
            type=self._parse_repo,
            default=Path("."),
            help="Repository checkout path (default: current directory).",
        )
        parser.add_argument(
            "--start",
            metavar="REV",
            type=self._parse_rev,
            default=None,
            help=(
                "Start commit revision "
                "(exclusive unless --include-start or start == end)."
            ),
        )
        parser.add_argument(
            "--end",
            metavar="REV",
            type=self._parse_rev,
            default=None,
            help="End commit revision (inclusive, default: HEAD).",
        )
        parser.add_argument(
            "--format",
            type=self._parse_format,
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
            metavar="INT",
            type=self._parse_non_negative_int,
            default=None,
            help="Maximum recursion depth (0 = top-level only).",
        )
        parser.add_argument(
            "--sync-command",
            "--sync-commands",
            dest="sync_command",
            action="store_true",
            help="Include the 'gclient sync' command for each commit.",
        )
        return parser

    def _parse_rev(self, value: str) -> str:
        cleaned = self._require_non_empty(value, "revision")
        if any(ch.isspace() or not ch.isprintable() for ch in cleaned):
            raise argparse.ArgumentTypeError(
                f"Invalid revision {value!r}: must not contain whitespace or "
                "control characters."
            )
        return cleaned

    def _parse_repo(self, value: str) -> Path:
        return Path(self._require_non_empty(value, "repository")).expanduser()

    def _parse_format(self, value: str) -> OutputFormat:
        cleaned = value.strip().lower()
        try:
            return OutputFormat(cleaned)
        except ValueError as exc:
            valid = ", ".join(fmt.value for fmt in OutputFormat)
            raise argparse.ArgumentTypeError(
                f"Invalid format {value!r}: choose from {valid}."
            ) from exc

    def _parse_non_negative_int(self, value: str) -> int:
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

    def _require_non_empty(self, value: str, label: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise argparse.ArgumentTypeError(
                f"{label.capitalize()} must not be empty."
            )
        if cleaned.startswith("-"):
            raise argparse.ArgumentTypeError(
                f"Invalid {label} {value!r}: must not start with '-'."
            )
        return cleaned


class CommitDepsCollector:
    def __init__(
        self,
        repo: str | Path = Path("."),
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

    def _resolve_repo(
        self, repo: str | Path, cwd: str | Path | None = None
    ) -> tuple[Path, str, Path | None]:
        candidate = (
            Path(cwd or Path.cwd()) / Path(repo).expanduser()
        ).resolve()
        git_root = (
            self._run_git(
                candidate, ["rev-parse", "--show-toplevel"], check=False
            )
            if candidate.is_dir()
            else None
        )
        if not git_root:
            raise CommitDepsError(f"Could not resolve repository: {repo!r}")

        repo_dir = Path(git_root.strip()).resolve()
        found_gclient_root = gclient_paths.FindGclientRoot(str(repo_dir))
        gclient_root = (
            Path(found_gclient_root).resolve() if found_gclient_root else None
        )
        display_path = (
            repo_dir.relative_to(gclient_root).as_posix()
            if gclient_root and repo_dir != gclient_root
            else (repo_dir.name or ".")
        )
        return repo_dir, display_path, gclient_root

    def collect_range(
        self,
        start: str | None = None,
        end: str | None = None,
        revisions: Sequence[str] = (),
        *,
        include_start: bool = False,
    ) -> list[CommitEntry]:
        rev_range = RevisionRange.from_args(start, end, revisions)
        return self.collect(
            rev_range.start, rev_range.end, include_start=include_start
        )

    def collect(
        self,
        start_rev: str,
        end_rev: str,
        *,
        include_start: bool = False,
    ) -> list[CommitEntry]:
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
        repo_dir = repo_dir.resolve()
        shas = self._resolve_range_shas(
            repo_dir, display_path, start_rev, end_rev, level=level
        )
        if not shas:
            return []

        stack_key = (repo_dir, shas.start, shas.end)
        if stack_key in self._active_stack:
            return []

        self._active_stack.add(stack_key)
        try:
            return self._collect_repo_entries(
                repo_dir,
                display_path,
                shas.start,
                shas.end,
                level,
                include_start,
            )
        finally:
            self._active_stack.discard(stack_key)

    def _resolve_range_shas(
        self,
        repo_dir: Path,
        display_path: str,
        start_rev: str,
        end_rev: str,
        *,
        level: int,
    ) -> RevisionRange | None:
        start_sha = self._resolve_commit(repo_dir, start_rev)
        end_sha = self._resolve_commit(repo_dir, end_rev)
        if start_sha and end_sha:
            return RevisionRange(start_sha, end_sha)
        if level == 0:
            missing = start_rev if not start_sha else end_rev
            raise CommitDepsError(
                f"Cannot resolve revision {missing!r} in {repo_dir}"
            )
        print(
            f"Warning: skipping {display_path} "
            f"({start_rev[:12]}..{end_rev[:12]}): "
            "commits not found locally.",
            file=sys.stderr,
        )
        return None

    def _collect_repo_entries(
        self,
        repo_dir: Path,
        display_path: str,
        start_sha: str,
        end_sha: str,
        level: int,
        include_start: bool,
    ) -> list[CommitEntry]:
        commits = self._list_commits_with_diffs(
            repo_dir,
            start_sha,
            end_sha,
            include_start=include_start
            or (level == 0 and start_sha == end_sha),
        )
        entries: list[CommitEntry] = []
        for commit_sha, diff_lines in commits:
            entries.extend(
                self._collect_commit_entries(
                    repo_dir, display_path, commit_sha, diff_lines, level
                )
            )
        return entries

    def _list_commits_with_diffs(
        self,
        repo_dir: Path,
        start_sha: str,
        end_sha: str,
        *,
        include_start: bool = False,
    ) -> list[tuple[str, list[str]]]:
        if not include_start and start_sha == end_sha:
            return []

        rev_range = self._format_rev_range(
            repo_dir, start_sha, end_sha, include_start
        )
        cmd = self._build_log_cmd(rev_range)
        output = self._run_git(repo_dir, cmd, check=False)
        return self._parse_log_output(output)

    def _format_rev_range(
        self, repo_dir: Path, start_sha: str, end_sha: str, include_start: bool
    ) -> str:
        if not include_start:
            return f"{start_sha}..{end_sha}"
        if self._verify_commit(repo_dir, f"{start_sha}^"):
            return f"{start_sha}^..{end_sha}"
        return end_sha

    def _build_log_cmd(self, rev_range: str) -> list[str]:
        cmd = [
            "log",
            "--raw",
            "--no-abbrev",
            "--diff-merges=first-parent",
            f"--format={_COMMIT_PREFIX}%H",
            "--reverse",
            "--topo-order",
        ]
        if self.first_parent:
            cmd.append("--first-parent")
        cmd.append(rev_range)
        return cmd

    def _parse_log_output(
        self, output: str | None
    ) -> list[tuple[str, list[str]]]:
        if not output:
            return []
        commits: list[tuple[str, list[str]]] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if line.startswith(_COMMIT_PREFIX):
                commits.append(
                    (line.removeprefix(_COMMIT_PREFIX).strip().lower(), [])
                )
            elif line.startswith(":") and commits:
                commits[-1][1].append(line)
        return commits

    def _collect_commit_entries(
        self,
        repo_dir: Path,
        display_path: str,
        commit_sha: str,
        diff_lines: Sequence[str],
        level: int,
    ) -> list[CommitEntry]:
        rolls = self._extract_commit_rolls(repo_dir, diff_lines)
        entries = [
            CommitEntry(
                path=display_path,
                level=level,
                commit=commit_sha,
                roll=bool(rolls),
            )
        ]
        entries.extend(
            self._expand_commit_rolls(
                repo_dir, display_path, rolls, level=level
            )
        )
        return entries

    def _extract_commit_rolls(
        self, repo_dir: Path, diff_lines: Sequence[str]
    ) -> list[SubrepoRoll]:
        rolls_by_path: dict[str, SubrepoRoll] = {}
        deps_blob_change: RevisionRange | None = None

        for line in diff_lines:
            diff = DiffTreeEntry.parse(line)
            if not diff:
                continue
            if (
                diff.old_mode == _GITLINK_MODE
                and diff.new_mode == _GITLINK_MODE
            ):
                rel_path = self._to_posix_path(diff.file_path)
                rolls_by_path[rel_path] = SubrepoRoll(
                    rel_path, diff.old_sha, diff.new_sha
                )
            elif diff.file_path == _DEPS_FILE:
                deps_blob_change = RevisionRange(diff.old_sha, diff.new_sha)

        if deps_blob_change:
            for rel_path, roll in self._collect_deps_blob_rolls(
                repo_dir, *deps_blob_change
            ).items():
                rolls_by_path.setdefault(rel_path, roll)

        return [rolls_by_path[k] for k in sorted(rolls_by_path)]

    def _collect_deps_blob_rolls(
        self, repo_dir: Path, old_blob: str, new_blob: str
    ) -> dict[str, SubrepoRoll]:
        old_content = self._get_blob_content(repo_dir, old_blob)
        new_content = self._get_blob_content(repo_dir, new_blob)
        if not old_content or not new_content:
            return {}
        changed_lines = set(old_content.splitlines()) ^ set(
            new_content.splitlines()
        )
        if not any(_GIT_SHA_RE.search(line) for line in changed_lines):
            return {}
        old_revs = self._parse_deps_git_revs(repo_dir, old_blob)
        new_revs = self._parse_deps_git_revs(repo_dir, new_blob)
        return {
            rel_path: SubrepoRoll(rel_path, old_revs[rel_path], new_rev)
            for rel_path, new_rev in new_revs.items()
            if rel_path in old_revs and old_revs[rel_path] != new_rev
        }

    def _get_blob_content(self, repo_dir: Path, blob_sha: str) -> str | None:
        if not blob_sha or blob_sha == ZERO_SHA:
            return None
        key = (repo_dir, blob_sha)
        if key not in self._blob_cache:
            self._blob_cache[key] = self._run_git(
                repo_dir, ["cat-file", "blob", blob_sha], check=False
            )
        return self._blob_cache[key]

    def _parse_deps_git_revs(
        self, repo_dir: Path, blob_sha: str
    ) -> dict[str, str]:
        key = (repo_dir, blob_sha)
        if key not in self._deps_cache:
            content = self._get_blob_content(repo_dir, blob_sha)
            parsed = self._eval_deps_content(content)
            self._deps_cache[key] = self._extract_git_revs(repo_dir, parsed)
        return self._deps_cache[key]

    def _eval_deps_content(self, content: str | None) -> Mapping[str, Any]:
        if not content:
            return {}
        try:
            parsed = gclient_eval.Parse(content, _DEPS_FILE)
        except Exception:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}

    def _extract_git_revs(
        self,
        repo_dir: Path,
        parsed: Mapping[str, Any],
    ) -> dict[str, str]:
        deps_dict = parsed.get("deps")
        if not isinstance(deps_dict, Mapping):
            return {}
        use_relative_paths = bool(parsed.get("use_relative_paths", False))
        git_revs: dict[str, str] = {}
        for dep_key, dep_info in deps_dict.items():
            if not self._is_valid_git_dep(dep_info):
                continue
            rev = gclient_eval.GetRevision(parsed, dep_key)
            if not rev or not gclient_utils.IsFullGitSha(rev):
                continue
            rel_path = self._normalize_dep_path(
                repo_dir, dep_key, use_relative_paths
            )
            if rel_path and rel_path != ".":
                git_revs[rel_path] = rev.lower()
        return git_revs

    def _is_valid_git_dep(self, dep_info: Any) -> bool:
        return (
            isinstance(dep_info, Mapping)
            and dep_info.get("dep_type", "git") == "git"
            and isinstance(dep_info.get("url"), str)
        )

    def _normalize_dep_path(
        self,
        repo_dir: Path,
        dep_key: str,
        use_relative_paths: bool,
    ) -> str:
        norm_key = self._to_posix_path(dep_key).lstrip("/")
        if use_relative_paths:
            return norm_key

        for prefix in (self.display_path, repo_dir.name):
            if prefix and norm_key.startswith(f"{prefix}/"):
                return norm_key.removeprefix(f"{prefix}/")

        if norm_key.startswith("src/") and not (repo_dir / norm_key).exists():
            return norm_key.removeprefix("src/")

        return norm_key

    def _to_posix_path(self, path_str: str) -> str:
        return PurePosixPath(path_str.replace("\\", "/")).as_posix()

    def _expand_commit_rolls(
        self,
        repo_dir: Path,
        display_path: str,
        rolls: Sequence[SubrepoRoll],
        *,
        level: int,
    ) -> list[CommitEntry]:
        if self.max_level is not None and level >= self.max_level:
            return []

        sub_entries: list[CommitEntry] = []
        for roll in rolls:
            subrepo_dir = repo_dir / roll.rel_path
            if not self._is_git_repo(subrepo_dir):
                continue
            sub_display_path = (
                f"{display_path}/{roll.rel_path}"
                if display_path and display_path != "."
                else roll.rel_path
            )
            sub_entries.extend(
                self._collect_repo(
                    repo_dir=subrepo_dir,
                    display_path=sub_display_path,
                    start_rev=roll.old_rev,
                    end_rev=roll.new_rev,
                    level=level + 1,
                )
            )
        return sub_entries

    def _is_git_repo(self, path: Path) -> bool:
        return (path / ".git").exists()

    def _resolve_commit(self, repo_dir: Path, rev: str) -> str | None:
        if sha := self._verify_commit(repo_dir, rev):
            return sha
        if not self.allow_fetch:
            return None

        for fetch_args in (
            ["fetch", "origin", "--quiet"],
            ["fetch", "origin", rev, "--quiet"],
        ):
            self._run_git(repo_dir, fetch_args, check=False)
            if sha := self._verify_commit(repo_dir, rev):
                return sha
        return None

    def _verify_commit(self, repo_dir: Path, rev: str) -> str | None:
        out = self._run_git(
            repo_dir,
            ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
            check=False,
        )
        return out.strip() if out else None

    def _run_git(
        self, repo_dir: Path, args: Sequence[str], *, check: bool = True
    ) -> str | None:
        cmd = ["git", "-C", str(repo_dir), *args]
        try:
            return subprocess.check_output(
                cmd,
                encoding="utf-8",
                errors="replace",
                shell=NEED_SHELL,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            if check:
                raise CommitDepsError(
                    f"Git command failed in {repo_dir}: {' '.join(cmd)}"
                ) from exc
            return None


def main(argv: Sequence[str] | None = None) -> int:
    args = InputParser.parse_args(argv)
    try:
        collector = CommitDepsCollector(
            args.repo,
            first_parent=args.first_parent,
            allow_fetch=not args.no_fetch,
            max_level=args.max_level,
        )
        entries: list[CommitEntry] = collector.collect_range(
            args.start,
            args.end,
            args.revisions,
            include_start=args.include_start,
        )
        output_format: OutputFormat = args.format
        sys.stdout.write(
            output_format.format_entries(
                entries, sync_command=args.sync_command
            )
        )
        return 0
    except CommitDepsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
