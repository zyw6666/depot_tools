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
from typing import Any, NamedTuple

import gclient_eval
import gclient_paths
import gclient_utils
import scm
import subprocess2

ZERO_SHA = "0" * 40
_COMMIT_PREFIX = "COMMIT:"
_GITLINK_MODE = "160000"
_NON_CIPD_GIT_SHA_RE = re.compile(r"(?<!git_revision:)\b[0-9a-fA-F]{40}\b")
_DIFF_TREE_LINE_RE = re.compile(
    r"^:([0-7]{6}) ([0-7]{6}) ([0-9a-fA-F]+) ([0-9a-fA-F]+) ([A-Z0-9]+)\t(.+)$"
)


class CommitDepsError(Exception):
    pass


class RevisionRange(NamedTuple):
    """A pair of git commit-ish strings (SHAs or git refs like 'HEAD~2').

    Note: These are git revisions/SHAs resolved by `git rev-parse`, not
    Chromium commit position numbers (Cr-Commit-Position).
    """

    start: str
    end: str

    @property
    def is_single_commit(self) -> bool:
        return self.start == self.end

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
        if end is None and cls._is_range_expr(start):
            return cls.parse(start)
        return cls(start, end or "HEAD")

    @classmethod
    def _is_range_expr(cls, expr: str) -> bool:
        return ".." in expr or expr.endswith("^!")

    @classmethod
    def parse(cls, expr: str) -> RevisionRange:
        for sep in ("...", ".."):
            if sep in expr:
                left, _, right = expr.partition(sep)
                return cls(left or "HEAD", right or "HEAD")
        rev = expr.removesuffix("^!") or "HEAD"
        return cls(rev, rev)


@dataclasses.dataclass(frozen=True)
class SubrepoRoll:
    rel_path: str
    old_rev: str
    new_rev: str


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
        old_mode, new_mode, old_sha, new_sha, _status, raw_path = match.groups()
        old_sha, new_sha = old_sha.lower(), new_sha.lower()
        if old_sha == new_sha or ZERO_SHA in (old_sha, new_sha):
            return None
        posix_path = PurePosixPath(raw_path.replace("\\", "/")).as_posix()
        return cls(old_mode, new_mode, old_sha, new_sha, posix_path)

    @property
    def is_gitlink_roll(self) -> bool:
        return self.old_mode == _GITLINK_MODE and self.new_mode == _GITLINK_MODE

    @property
    def is_deps_file(self) -> bool:
        return self.file_path == gclient_eval.DEPS

    def to_subrepo_roll(self) -> SubrepoRoll:
        return SubrepoRoll(self.file_path, self.old_sha, self.new_sha)


class CommitLogEntry(NamedTuple):
    sha: str
    diffs: tuple[DiffTreeEntry, ...]

    @classmethod
    def parse_log(cls, output: str | None) -> list[CommitLogEntry]:
        if not output:
            return []
        entries: list[CommitLogEntry] = []
        current_sha: str | None = None
        current_diffs: list[DiffTreeEntry] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if line.startswith(_COMMIT_PREFIX):
                if current_sha is not None:
                    entries.append(cls(current_sha, tuple(current_diffs)))
                current_sha = line.removeprefix(_COMMIT_PREFIX).strip().lower()
                current_diffs = []
            elif line.startswith(":") and current_sha is not None:
                if diff := DiffTreeEntry.parse(line):
                    current_diffs.append(diff)
        if current_sha is not None:
            entries.append(cls(current_sha, tuple(current_diffs)))
        return entries


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
            rev_flags = " ".join(item.revision_flag for item in ancestors)
            commands.append(f"gclient sync {rev_flags}")
        return commands

    @property
    def revision_flag(self) -> str:
        return f"--revision {self.path}@{self.commit}"


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


class GitRepo:
    def __init__(
        self,
        path: Path,
        display_path: str,
        *,
        first_parent: bool = False,
    ) -> None:
        self.path = path
        self.display_path = display_path
        self.first_parent = first_parent
        self._blob_cache: dict[str, str | None] = {}

    @classmethod
    def resolve(
        cls,
        repo: str | Path = Path("."),
        cwd: str | Path | None = None,
        *,
        first_parent: bool = False,
    ) -> GitRepo:
        repo_dir = cls._resolve_repo_dir(repo, cwd)
        display_path = cls._resolve_display_path(repo_dir)
        return cls(
            repo_dir,
            display_path,
            first_parent=first_parent,
        )

    @classmethod
    def _resolve_repo_dir(
        cls, repo: str | Path, cwd: str | Path | None
    ) -> Path:
        candidate = (
            Path(cwd or Path.cwd()) / Path(repo).expanduser()
        ).resolve()
        if candidate.is_dir():
            try:
                git_root = scm.GIT.GetCheckoutRoot(str(candidate))
            except (subprocess2.CalledProcessError, OSError):
                git_root = None
            if git_root:
                repo_dir = Path(git_root).resolve()
                if (repo_dir / ".git").exists():
                    return repo_dir
        raise CommitDepsError(f"Could not resolve repository: {repo!r}")

    @classmethod
    def _resolve_display_path(cls, repo_dir: Path) -> str:
        found_root = gclient_paths.FindGclientRoot(str(repo_dir))
        gclient_root = Path(found_root).resolve() if found_root else None
        if gclient_root and repo_dir != gclient_root:
            return repo_dir.relative_to(gclient_root).as_posix()
        return repo_dir.name or "."

    def subrepo(self, rel_path: str) -> GitRepo | None:
        sub_path = (self.path / rel_path).resolve()
        if not (sub_path / ".git").exists():
            return None
        sub_display = (
            f"{self.display_path}/{rel_path}"
            if self.display_path and self.display_path != "."
            else rel_path
        )
        return GitRepo(
            sub_path,
            sub_display,
            first_parent=self.first_parent,
        )

    def resolve_range(self, start_rev: str, end_rev: str) -> RevisionRange:
        return RevisionRange(
            self._resolve_commit(start_rev), self._resolve_commit(end_rev)
        )

    def list_commits(
        self, shas: RevisionRange, *, include_start: bool = False
    ) -> list[CommitLogEntry]:
        if not include_start and shas.is_single_commit:
            return []
        rev_range = self._format_rev_range(shas, include_start=include_start)
        cmd = self._build_log_cmd(rev_range)
        output = self._run_git(cmd)
        return CommitLogEntry.parse_log(output)

    def read_blob(self, blob_sha: str) -> str | None:
        if not blob_sha or blob_sha == ZERO_SHA:
            return None
        if blob_sha not in self._blob_cache:
            self._blob_cache[blob_sha] = self._run_git(
                ["cat-file", "blob", blob_sha]
            )
        return self._blob_cache[blob_sha]

    def _resolve_commit(self, rev: str) -> str:
        if sha := self._verify_commit(rev):
            return sha
        raise CommitDepsError(f"Cannot resolve revision {rev!r} in {self.path}")

    def _format_rev_range(
        self, shas: RevisionRange, *, include_start: bool
    ) -> str:
        if not include_start:
            return f"{shas.start}..{shas.end}"
        if self._verify_commit(f"{shas.start}^"):
            return f"{shas.start}^..{shas.end}"
        return shas.end

    def _verify_commit(self, rev: str) -> str | None:
        try:
            sha = scm.GIT.ResolveCommit(str(self.path), rev)
        except (subprocess2.CalledProcessError, OSError):
            return None
        return sha.strip().lower() if isinstance(sha, str) and sha else None

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

    def _run_git(self, args: Sequence[str]) -> str | None:
        try:
            out = scm.GIT.Capture(
                list(args), cwd=str(self.path), strip_out=False
            )
            return (
                out if isinstance(out, str) else out.decode("utf-8", "replace")
            )
        except (subprocess2.CalledProcessError, OSError):
            return None


class DepsParser:
    def __init__(self) -> None:
        self._deps_cache: dict[tuple[Path, str], dict[str, str]] = {}

    def diff_rolls(
        self, repo: GitRepo, old_blob: str, new_blob: str
    ) -> dict[str, SubrepoRoll]:
        old_content = repo.read_blob(old_blob)
        new_content = repo.read_blob(new_blob)
        if not old_content or not new_content:
            return {}
        if not self._has_non_cipd_sha_change(old_content, new_content):
            return {}
        old_revs = self._parse_git_revs(repo, old_blob, old_content)
        new_revs = self._parse_git_revs(repo, new_blob, new_content)
        return {
            rel_path: SubrepoRoll(rel_path, old_revs[rel_path], new_rev)
            for rel_path, new_rev in new_revs.items()
            if rel_path in old_revs and old_revs[rel_path] != new_rev
        }

    def _has_non_cipd_sha_change(
        self, old_content: str, new_content: str
    ) -> bool:
        changed_lines = set(old_content.splitlines()) ^ set(
            new_content.splitlines()
        )
        return any(_NON_CIPD_GIT_SHA_RE.search(line) for line in changed_lines)

    def _parse_git_revs(
        self, repo: GitRepo, blob_sha: str, content: str
    ) -> dict[str, str]:
        key = (repo.path, blob_sha)
        if key not in self._deps_cache:
            parsed = self._eval_deps_content(content)
            self._deps_cache[key] = self._extract_git_revs(repo, parsed)
        return self._deps_cache[key]

    def _eval_deps_content(self, content: str) -> Mapping[str, Any]:
        try:
            parsed = gclient_eval.Parse(content, gclient_eval.DEPS)
        except Exception:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}

    def _extract_git_revs(
        self,
        repo: GitRepo,
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
                repo, dep_key, use_relative_paths
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
        repo: GitRepo,
        dep_key: str,
        use_relative_paths: bool,
    ) -> str:
        dep_path = (
            PurePosixPath(dep_key.replace("\\", "/")).as_posix().lstrip("/")
        )
        if use_relative_paths:
            return dep_path
        return self._strip_solution_prefix(repo, dep_path)

    def _strip_solution_prefix(self, repo: GitRepo, dep_path: str) -> str:
        for prefix in (repo.display_path, repo.path.name):
            if prefix and dep_path.startswith(f"{prefix}/"):
                return dep_path.removeprefix(f"{prefix}/")
        if dep_path.startswith("src/") and not (repo.path / dep_path).exists():
            return dep_path.removeprefix("src/")
        return dep_path


class CommitDepsCollector:
    def __init__(
        self,
        repo: str | Path = Path("."),
        *,
        cwd: str | Path | None = None,
        first_parent: bool = False,
        max_level: int | None = None,
    ) -> None:
        self._root_repo = GitRepo.resolve(
            repo,
            cwd=cwd,
            first_parent=first_parent,
        )
        self.repo_dir = self._root_repo.path
        self.display_path = self._root_repo.display_path
        self.max_level = max_level
        self._deps_parser = DepsParser()
        self._active_ranges: set[tuple[Path, RevisionRange]] = set()

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
            self._root_repo,
            start_rev,
            end_rev,
            level=0,
            include_start=include_start,
        )

    def _collect_repo(
        self,
        repo: GitRepo,
        start_rev: str,
        end_rev: str,
        *,
        level: int = 0,
        include_start: bool = False,
    ) -> list[CommitEntry]:
        shas = repo.resolve_range(start_rev, end_rev)
        range_key = (repo.path, shas)
        if range_key in self._active_ranges:
            return []

        self._active_ranges.add(range_key)
        try:
            return self._collect_repo_entries(
                repo, shas, level=level, include_start=include_start
            )
        finally:
            self._active_ranges.discard(range_key)

    def _collect_repo_entries(
        self,
        repo: GitRepo,
        shas: RevisionRange,
        *,
        level: int,
        include_start: bool,
    ) -> list[CommitEntry]:
        is_top_level_single_commit = level == 0 and shas.is_single_commit
        commits = repo.list_commits(
            shas, include_start=include_start or is_top_level_single_commit
        )
        entries: list[CommitEntry] = []
        for commit in commits:
            entries.extend(self._collect_commit_entries(repo, commit, level))
        return entries

    def _collect_commit_entries(
        self,
        repo: GitRepo,
        commit: CommitLogEntry,
        level: int,
    ) -> list[CommitEntry]:
        rolls = self._extract_commit_rolls(repo, commit.diffs)
        entries = [
            CommitEntry(
                path=repo.display_path,
                level=level,
                commit=commit.sha,
                roll=bool(rolls),
            )
        ]
        entries.extend(self._expand_commit_rolls(repo, rolls, level=level))
        return entries

    def _extract_commit_rolls(
        self, repo: GitRepo, diffs: Sequence[DiffTreeEntry]
    ) -> list[SubrepoRoll]:
        gitlink_rolls = {
            diff.file_path: diff.to_subrepo_roll()
            for diff in diffs
            if diff.is_gitlink_roll
        }
        deps_rolls = self._extract_deps_rolls(repo, diffs)
        merged_rolls = deps_rolls | gitlink_rolls
        return [merged_rolls[path] for path in sorted(merged_rolls)]

    def _extract_deps_rolls(
        self, repo: GitRepo, diffs: Sequence[DiffTreeEntry]
    ) -> dict[str, SubrepoRoll]:
        for diff in diffs:
            if diff.is_deps_file:
                return self._deps_parser.diff_rolls(
                    repo, diff.old_sha, diff.new_sha
                )
        return {}

    def _expand_commit_rolls(
        self,
        repo: GitRepo,
        rolls: Sequence[SubrepoRoll],
        *,
        level: int,
    ) -> list[CommitEntry]:
        if self.max_level is not None and level >= self.max_level:
            return []

        sub_entries: list[CommitEntry] = []
        for roll in rolls:
            subrepo = repo.subrepo(roll.rel_path)
            if not subrepo:
                continue
            sub_entries.extend(
                self._collect_repo(
                    subrepo,
                    roll.old_rev,
                    roll.new_rev,
                    level=level + 1,
                )
            )
        return sub_entries


def main(argv: Sequence[str] | None = None) -> int:
    args = InputParser.parse_args(argv)
    try:
        collector = CommitDepsCollector(
            args.repo,
            first_parent=args.first_parent,
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
