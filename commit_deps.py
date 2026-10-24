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

_NON_CIPD_GIT_SHA_RE = re.compile(r"(?<!git_revision:)\b[0-9a-fA-F]{40}\b")


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
    def from_args(cls, revisions: Sequence[str] = ()) -> RevisionRange:
        if len(revisions) == 1:
            return cls.parse(revisions[0])
        if len(revisions) == 2:
            return cls(revisions[0], revisions[1])
        raise CommitDepsError(
            "Expected 1 or 2 positional revision arguments "
            f"(e.g. 'AAA..BBBB', 'AAA BBBB', or 'AAA'), got {len(revisions)}."
        )

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

    @classmethod
    def from_gitlink(cls, diff: scm.DiffTreeEntry) -> SubrepoRoll:
        return cls(diff.file_path, diff.old_sha, diff.new_sha)


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

        headers = (
            "Path",
            "Level",
            "commit hash",
            "roll",
            *(("sync command",) if sync_command else ()),
        )
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
        items = [
            {
                **dataclasses.asdict(entry),
                **({"sync_command": sync_cmds[i]} if sync_cmds else {}),
            }
            for i, entry in enumerate(entries)
        ]
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
            max(len(col) for col in (header, *(row[i] for row in rows)))
            for i, header in enumerate(headers)
        ]
        all_rows = (headers, ["-" * width for width in widths], *rows)
        return (
            "\n".join(
                "| "
                + " | ".join(
                    f"{col:<{width}}" for col, width in zip(cols, widths)
                )
                + " |"
                for cols in all_rows
            )
            + "\n"
        )


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
            "--format",
            type=self._parse_format,
            choices=tuple(OutputFormat),
            default=OutputFormat.TABLE,
            help="Output format: table (default), csv, or json.",
        )
        parser.add_argument(
            "--include-start",
            action="store_true",
            help="Include the start commit itself in the top-level range.",
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

    @classmethod
    def resolve(
        cls,
        repo: str | Path = Path("."),
        cwd: str | Path | None = None,
        *,
        first_parent: bool = False,
    ) -> GitRepo:
        repo_dir = cls._resolve_repo_dir(repo, cwd)
        return cls(
            repo_dir,
            cls._resolve_display_path(repo_dir),
            first_parent=first_parent,
        )

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
    ) -> list[scm.CommitLogEntry]:
        if not include_start and shas.is_single_commit:
            return []
        rev_range = self._format_rev_range(shas, include_start=include_start)
        return scm.GIT.GetCommitLogDiffs(
            str(self.path), rev_range, first_parent=self.first_parent
        )

    def read_blob(self, blob_sha: str) -> str | None:
        return scm.GIT.GetBlob(str(self.path), blob_sha)

    @classmethod
    def _resolve_repo_dir(
        cls, repo: str | Path, cwd: str | Path | None
    ) -> Path:
        candidate = (
            Path(cwd or Path.cwd()) / Path(repo).expanduser()
        ).resolve()
        if candidate.is_dir():
            if (candidate / ".git").exists():
                return candidate
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

    def _resolve_commit(self, rev: str) -> str:
        if sha := self._verify_commit(rev):
            return sha
        raise CommitDepsError(f"Cannot resolve revision {rev!r} in {self.path}")

    def _format_rev_range(
        self, shas: RevisionRange, *, include_start: bool
    ) -> str:
        if not include_start:
            return f"{shas.start}..{shas.end}"
        if shas.is_single_commit:
            return f"{shas.start}^!"
        if self._verify_commit(f"{shas.start}^"):
            return f"{shas.start}^..{shas.end}"
        return shas.end

    def _verify_commit(self, rev: str) -> str | None:
        try:
            sha = scm.GIT.ResolveCommit(str(self.path), rev)
        except (subprocess2.CalledProcessError, OSError):
            return None
        return sha.strip().lower() if isinstance(sha, str) and sha else None


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
        if _NON_CIPD_GIT_SHA_RE.findall(
            old_content
        ) == _NON_CIPD_GIT_SHA_RE.findall(new_content):
            return {}
        old_revs = self._parse_git_revs(repo, old_blob, old_content)
        new_revs = self._parse_git_revs(repo, new_blob, new_content)
        return {
            rel_path: SubrepoRoll(rel_path, old_revs[rel_path], new_rev)
            for rel_path, new_rev in new_revs.items()
            if rel_path in old_revs and old_revs[rel_path] != new_rev
        }

    def _parse_git_revs(
        self, repo: GitRepo, blob_sha: str, content: str
    ) -> dict[str, str]:
        key = (repo.path, blob_sha)
        if key not in self._deps_cache:
            try:
                parsed = gclient_eval.Parse(content, gclient_eval.DEPS)
            except Exception:
                parsed = {}
            self._deps_cache[key] = self._extract_git_revs(
                repo, parsed if isinstance(parsed, Mapping) else {}
            )
        return self._deps_cache[key]

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
            commits = repo.list_commits(
                shas,
                include_start=include_start
                or (level == 0 and shas.is_single_commit),
            )
            entries: list[CommitEntry] = []
            for commit in commits:
                rolls = self._extract_commit_rolls(repo, commit.diffs)
                entries.append(
                    CommitEntry(
                        path=repo.display_path,
                        level=level,
                        commit=commit.sha,
                        roll=bool(rolls),
                    )
                )
                if self.max_level is not None and level >= self.max_level:
                    continue
                for roll in rolls:
                    if subrepo := repo.subrepo(roll.rel_path):
                        entries.extend(
                            self._collect_repo(
                                subrepo,
                                roll.old_rev,
                                roll.new_rev,
                                level=level + 1,
                            )
                        )
            return entries
        finally:
            self._active_ranges.discard(range_key)

    def _extract_commit_rolls(
        self, repo: GitRepo, diffs: Sequence[scm.DiffTreeEntry]
    ) -> list[SubrepoRoll]:
        deps_rolls: dict[str, SubrepoRoll] = {}
        for diff in diffs:
            if diff.file_path == gclient_eval.DEPS:
                deps_rolls = self._deps_parser.diff_rolls(
                    repo, diff.old_sha, diff.new_sha
                )
                break
        gitlink_rolls = {
            diff.file_path: SubrepoRoll.from_gitlink(diff)
            for diff in diffs
            if diff.is_gitlink_roll
        }
        merged_rolls = deps_rolls | gitlink_rolls
        return [merged_rolls[path] for path in sorted(merged_rolls)]


def main(argv: Sequence[str] | None = None) -> int:
    args = InputParser.parse_args(argv)
    try:
        rev_range = RevisionRange.from_args(args.revisions)
        collector = CommitDepsCollector(
            args.repo,
            first_parent=args.first_parent,
            max_level=args.max_level,
        )
        entries = collector.collect(
            rev_range.start,
            rev_range.end,
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
