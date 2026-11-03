#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Analyzes commit ranges and recursively expands DEPS / submodule rolls."""

from __future__ import annotations

import argparse
import contextlib
import csv
from dataclasses import dataclass, field, replace
import enum
import io
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import sys
import types
from typing import TYPE_CHECKING, NamedTuple

import gclient
import gclient_eval
import gclient_paths
import gclient_utils
import git_common
import git_footers
import scm
import setup_color
import subprocess2

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Final, TypeAlias, TypedDict

    _VarValue: TypeAlias = gclient_eval.ConstantString | str | bool | int
    _VariableMap: TypeAlias = Mapping[str, _VarValue]
    _EvalVarsDict: TypeAlias = dict[str, _VarValue]
    _SubmoduleSection: TypeAlias = Mapping[str, str]
    _PinnedShas: TypeAlias = tuple[tuple[str, str], ...]
    _JsonValue: TypeAlias = str | int | bool | dict[str, str]

    class _CipdPackage(TypedDict, total=False):
        package: str
        version: str
        version_file: str

    class _GcsObject(TypedDict, total=False):
        object_name: str
        sha256sum: str
        size_bytes: int
        generation: int
        output_file: str
        condition: str

    class _DepInfo(TypedDict, total=False):
        dep_type: str
        url: str | None
        condition: str
        packages: Sequence[_CipdPackage]
        bucket: str
        objects: Sequence[_GcsObject]

    class _DepsScope(TypedDict, total=False):
        git_dependencies: str
        use_relative_paths: bool
        target_os: Sequence[str | None]
        vars: Mapping[str, _VarValue]
        deps: Mapping[str, _DepInfo]
        recursedeps: Sequence[str | Sequence[str]]

    _CipdDep: TypeAlias = tuple[str, Sequence[_CipdPackage] | None]
    _GcsDep: TypeAlias = tuple[str, str | None, list[_GcsObject]]
    _NonGitDep: TypeAlias = _CipdDep | _GcsDep

_COMMIT_POSITION_KEY: Final = "Cr-Commit-Position"
_BARE_POSITION_RE: Final = re.compile(r"(?i)^r?(\d{1,8})$")
_CHROME_VERSION_RE: Final = re.compile(r"\d+\.\d+\.\d+\.\d+")
_PARSE_ERRORS: Final = (
    gclient_utils.Error,
    SyntaxError,
    ValueError,
    KeyError,
    TypeError,
)
_MAIN_POSITION_REFS: Final[tuple[str, ...]] = (
    "refs/remotes/origin/main",
    "refs/remotes/origin/master",
    "refs/heads/main",
    "refs/heads/master",
    "HEAD",
)
_TABLE_MAX_SUBJECT_LEN: Final = 72
_TABLE_TRUNCATED_PREFIX_LEN: Final = 69
_HELP_EPILOG: Final = (
    "examples:\n  git commit-deps origin/main..HEAD\n"
    "  git commit-deps 'Cr-Commit-Position: refs/heads/main@{#1412300}'\n"
    "  git commit-deps -n 10 --filter v8 --fetch\n"
    "  git checkout --detach && git commit-deps A..B --sync-command\n"
)
_TARGET_OS_ALIASES: Final[dict[str, str]] = {
    "unix": "linux",
    "osx": "mac",
    "windows": "win",
}


class CommitDepsError(Exception):
    """Raised when commit_deps arguments, repositories, or revisions fail."""


class CommitRange(NamedTuple):
    """Commit-ish range before resolution; 40-char git SHAs after resolution."""

    start: str
    end: str
    is_range: bool = True
    use_merge_base: bool = False

    @property
    def is_single_commit(self) -> bool:
        return not self.is_range

    @classmethod
    def from_args(
        cls, commits: Sequence[str] = (), *, max_count: int | None = None
    ) -> CommitRange:
        if not commits and max_count is not None:
            return cls("HEAD", "HEAD", is_range=max_count > 1)
        if len(commits) not in (1, 2):
            raise CommitDepsError(f"Expected 1..2 commits, got {len(commits)}.")
        if len(commits) == 2:
            return cls(commits[0], commits[1], is_range=True)
        parsed_range = cls.parse(commits[0])
        if max_count is None or parsed_range.is_range:
            return parsed_range
        return cls(parsed_range.start, parsed_range.end, is_range=max_count > 1)

    @classmethod
    def parse(cls, range_expr: str) -> CommitRange:
        if ".." in range_expr:
            separator = "..." if "..." in range_expr else ".."
            left_ref, _, right_ref = range_expr.partition(separator)
            if not left_ref and not right_ref:
                raise CommitDepsError(f"Invalid empty range {range_expr!r}.")
            return cls(
                left_ref or "HEAD",
                right_ref or "HEAD",
                is_range=True,
                use_merge_base=separator == "...",
            )
        commit_ref = range_expr.removesuffix("^!") or "HEAD"
        return cls(commit_ref, commit_ref, is_range=False)


@dataclass(frozen=True)
class SubrepoRoll:
    """A subrepository roll between two 40-char git commit SHAs."""

    rel_path: str
    old_sha: str
    new_sha: str

    @classmethod
    def from_diff_entry(cls, diff: scm.DiffTreeEntry) -> SubrepoRoll:
        return cls(diff.file_path, diff.old_sha, diff.new_sha)


@dataclass(frozen=True)
class GitCommitEntry:
    """A collected repository commit entry (`commit` is a 40-char git SHA)."""

    # POSIX path relative to .gclient root, or rooted at checkout dirname.
    path: str
    # Roll expansion depth: 0 for target --repo, +1 per nested subrepo.
    level: int
    # Full 40-character lowercase hex Git commit SHA in this repository.
    commit: str
    # True if commit changes DEPS, gitlinks, or CIPD/GCS (after --filter).
    roll: bool = False
    # First line of commit message (%s); ignored in dataclass comparisons.
    subject: str = field(default="", compare=False)
    # Last Cr-Commit-Position trailer (e.g. "refs/heads/main@{#1}"), or "".
    commit_position: str = field(default="", compare=False)
    # Active (path, sha) repo pins; if (), derived from ancestor entries.
    pinned_shas: _PinnedShas = field(default=(), repr=False, compare=False)

    @classmethod
    def build_sync_commands(
        cls, entries: Sequence[GitCommitEntry], *, standalone: bool = False
    ) -> list[str]:
        if standalone:
            return [
                f"git checkout --detach {entry.commit}" for entry in entries
            ]
        commands: list[str] = []
        for repo_shas in cls.build_pinned_shas(entries):
            revision_flags = " ".join(
                f"--revision {repo_path}@{commit_sha}"
                for repo_path, commit_sha in repo_shas.items()
            )
            commands.append(f"gclient sync {revision_flags}")
        return commands

    @classmethod
    def build_pinned_shas(
        cls, entries: Sequence[GitCommitEntry]
    ) -> list[dict[str, str]]:
        ancestors: list[GitCommitEntry] = []
        pinned_shas_list: list[dict[str, str]] = []
        for entry in entries:
            ancestors = [*ancestors[: entry.level], entry]
            if entry.pinned_shas:
                pinned_shas_list.append(dict(entry.pinned_shas))
            else:
                pinned_shas_list.append(
                    {ancestor.path: ancestor.commit for ancestor in ancestors}
                )
        return pinned_shas_list


class OutputFormat(enum.StrEnum):
    TABLE = "table"
    CSV = "csv"
    JSON = "json"

    @classmethod
    def _missing_(cls, value: object) -> OutputFormat | None:
        if isinstance(value, str):
            normalized = value.strip().lower()
            for member in cls:
                if member.value == normalized:
                    return member
        return None

    def format_entries(
        self,
        entries: Sequence[GitCommitEntry],
        *,
        sync_command: bool = False,
        standalone: bool = False,
    ) -> str:
        sync_commands = (
            GitCommitEntry.build_sync_commands(entries, standalone=standalone)
            if sync_command
            else ()
        )
        if self is OutputFormat.JSON:
            return self._format_json(entries, sync_commands)
        if self is OutputFormat.CSV:
            return self._format_csv(entries, sync_commands)
        return self._format_table(entries, sync_commands)

    def _format_json(
        self, entries: Sequence[GitCommitEntry], sync_commands: Sequence[str]
    ) -> str:
        pinned_shas_list = GitCommitEntry.build_pinned_shas(entries)
        payload: list[dict[str, _JsonValue]] = []
        for index, (entry, pinned_shas) in enumerate(
            zip(entries, pinned_shas_list)
        ):
            record: dict[str, _JsonValue] = {
                "path": entry.path,
                "level": entry.level,
                "commit": entry.commit,
                "roll": entry.roll,
                "subject": entry.subject,
                "commit_position": entry.commit_position,
                "pinned_shas": pinned_shas,
            }
            if sync_commands:
                record["sync_command"] = sync_commands[index]
            payload.append(record)
        return f"{json.dumps(payload, indent=2)}\n"

    def _format_csv(
        self, entries: Sequence[GitCommitEntry], sync_commands: Sequence[str]
    ) -> str:
        headers, rows = self._build_display_rows(
            entries, sync_commands, truncate_subject=False
        )
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="\n").writerows((headers, *rows))
        return buffer.getvalue()

    def _format_table(
        self, entries: Sequence[GitCommitEntry], sync_commands: Sequence[str]
    ) -> str:
        headers, rows = self._build_display_rows(
            entries, sync_commands, truncate_subject=True
        )
        all_rows = (headers, *rows)
        column_widths = [
            max(len(row[col_index]) for row in all_rows)
            for col_index in range(len(headers))
        ]
        divider_row = tuple("-" * width for width in column_widths)
        formatted_lines: list[str] = []
        for row in (headers, divider_row, *rows):
            formatted_cells = " | ".join(
                f"{cell:<{width}}" for cell, width in zip(row, column_widths)
            )
            formatted_lines.append(f"| {formatted_cells} |\n")
        return "".join(formatted_lines)

    def _build_display_rows(
        self,
        entries: Sequence[GitCommitEntry],
        sync_commands: Sequence[str],
        *,
        truncate_subject: bool,
    ) -> tuple[tuple[str, ...], list[tuple[str, ...]]]:
        headers: tuple[str, ...] = (
            "Path",
            "Level",
            "commit hash",
            "roll",
            "subject",
        )
        if sync_commands:
            headers = (*headers, "sync command")
        rows: list[tuple[str, ...]] = []
        for index, entry in enumerate(entries):
            subject = entry.subject
            if truncate_subject and len(subject) > _TABLE_MAX_SUBJECT_LEN:
                subject = f"{subject[:_TABLE_TRUNCATED_PREFIX_LEN]}..."
            row: tuple[str, ...] = (
                entry.path,
                str(entry.level),
                entry.commit,
                str(entry.roll).lower(),
                subject,
            )
            if sync_commands:
                row = (*row, sync_commands[index])
            rows.append(row)
        return headers, rows


class InputParser:
    @classmethod
    def parse_args(
        cls, argv: Sequence[str] | None = None
    ) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog="git commit-deps",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description="Collects commits and recursively expands DEPS rolls.",
            epilog=_HELP_EPILOG,
        )
        parser.add_argument("commits", nargs="*", type=cls._parse_commit_ref)
        parser.add_argument("--repo", default=".")
        parser.add_argument(
            "--format",
            type=OutputFormat,
            choices=tuple(OutputFormat),
            default=OutputFormat.TABLE,
        )
        parser.add_argument("--include-start", action="store_true")
        parser.add_argument("--first-parent", action="store_true")
        parser.add_argument("--max-level", type=int)
        parser.add_argument("-n", "--max-count", type=int)
        parser.add_argument(
            "--filter",
            action="append",
            type=RepoFilter.normalize_pattern,
            default=[],
        )
        parser.add_argument("--fetch", action="store_true")
        parser.add_argument(
            "--sync-command", "--sync-commands", action="store_true"
        )
        parsed_args = parser.parse_args(argv)
        cls._validate_parsed_args(parser, parsed_args)
        return parsed_args

    @classmethod
    def resolve_commit_range(
        cls, repo_dir: Path, commit_range: CommitRange
    ) -> CommitRange:
        start_sha = cls.resolve_commit(repo_dir, commit_range.start)
        end_sha = cls.resolve_commit(repo_dir, commit_range.end)
        if commit_range.use_merge_base:
            start_sha = _capture_git(
                repo_dir, "merge-base", start_sha, end_sha
            ).lower()
            if not start_sha:
                raise CommitDepsError(
                    f"No merge-base for {commit_range.start!r} and "
                    f"{commit_range.end!r}"
                )
        return CommitRange(start_sha, end_sha, is_range=commit_range.is_range)

    @classmethod
    def resolve_commit(cls, repo_dir: Path, commit_ref: str) -> str:
        error_msg = f"Cannot resolve {commit_ref!r} in {repo_dir}"
        position_sha = cls._resolve_commit_position(
            repo_dir, commit_ref, error_msg
        )
        if position_sha:
            return position_sha
        resolved_sha = _try_resolve_commit(repo_dir, commit_ref)
        if resolved_sha:
            return resolved_sha
        if _CHROME_VERSION_RE.fullmatch(commit_ref):
            fetch_hint = f"fetch tags with 'git fetch origin tag {commit_ref}'"
            error_msg = (
                f"{error_msg} ({fetch_hint} or 'gclient sync --with_tags')"
            )
        raise CommitDepsError(error_msg)

    @classmethod
    def _parse_commit_ref(cls, commit_ref: str) -> str:
        stripped = commit_ref.strip()
        value, has_footer_key = cls._split_position_footer(stripped)
        has_whitespace = any(char.isspace() for char in value)
        if not value or not value.isprintable() or has_whitespace:
            raise ValueError(commit_ref)
        if has_footer_key:
            return f"{_COMMIT_POSITION_KEY}: {value}"
        return value

    @classmethod
    def _validate_parsed_args(
        cls, parser: argparse.ArgumentParser, parsed_args: argparse.Namespace
    ) -> None:
        repo_arg = parsed_args.repo.strip()
        max_level = parsed_args.max_level or 0
        max_count = (
            1 if parsed_args.max_count is None else parsed_args.max_count
        )
        if not repo_arg or max_level < 0 or max_count < 1:
            parser.error("invalid option value")
        parsed_args.repo = Path(repo_arg).expanduser()

    @classmethod
    def _resolve_commit_position(
        cls, repo_dir: Path, commit_ref: str, error_msg: str
    ) -> str | None:
        parsed_target = cls._parse_position_target(commit_ref, error_msg)
        if parsed_target is None:
            return None
        raw_branch, position_number, is_strict = parsed_target
        grep_arg, candidate_refs = cls._build_position_search_plan(
            raw_branch, position_number
        )
        seen_tips: set[str] = set()
        for candidate_ref in candidate_refs:
            tip_sha = _try_resolve_commit(repo_dir, candidate_ref)
            if not tip_sha or tip_sha in seen_tips:
                continue
            seen_tips.add(tip_sha)
            matched_sha = _capture_git(
                repo_dir,
                "rev-list",
                "-n",
                "1",
                "--extended-regexp",
                grep_arg,
                candidate_ref,
            ).lower()
            if gclient_utils.IsFullGitSha(matched_sha):
                return matched_sha
        if is_strict:
            raise CommitDepsError(error_msg)
        return None

    @classmethod
    def _parse_position_target(
        cls, commit_ref: str, error_msg: str
    ) -> tuple[str, str, bool] | None:
        spec, has_footer_key = cls._split_position_footer(commit_ref)
        match_target = f"HEAD{spec}" if spec.startswith("@{#") else spec
        position_match = git_footers.CHROME_COMMIT_POSITION_PATTERN.match(
            match_target
        )
        if position_match:
            return position_match.group(1), position_match.group(2), True
        if has_footer_key or "@{#" in commit_ref:
            raise CommitDepsError(error_msg)
        bare_match = _BARE_POSITION_RE.match(commit_ref)
        if bare_match:
            return "*", bare_match.group(1), False
        return None

    @classmethod
    def _split_position_footer(cls, raw_ref: str) -> tuple[str, bool]:
        footer = git_footers.parse_footer(raw_ref)
        if not footer:
            return raw_ref, False
        key, value = footer
        if git_footers.normalize_name(key) != _COMMIT_POSITION_KEY:
            return raw_ref, False
        return str(value).strip(), True

    @classmethod
    def _build_position_search_plan(
        cls, raw_branch: str, position_number: str
    ) -> tuple[str, tuple[str, ...]]:
        remote_ref, canonical_ref = cls._normalize_position_branch(raw_branch)
        is_main = canonical_ref in ("refs/heads/main", "refs/heads/master")
        if raw_branch == "*":
            ref_pattern, candidates = ".*", _MAIN_POSITION_REFS
        elif raw_branch == "HEAD" or is_main:
            ref_pattern = "refs/heads/(master|main)"
            candidates = _MAIN_POSITION_REFS
        else:
            ref_pattern = re.escape(canonical_ref)
            raw_refs = (remote_ref, canonical_ref, raw_branch, "HEAD")
            candidates = tuple(dict.fromkeys(ref for ref in raw_refs if ref))
        grep_arg = (
            f"--grep=^{_COMMIT_POSITION_KEY}: "
            f"{ref_pattern}@\\{{#{position_number}\\}}"
        )
        return grep_arg, candidates

    @classmethod
    def _normalize_position_branch(cls, raw_branch: str) -> tuple[str, str]:
        ref_pair = scm.GIT.RefToRemoteRef(raw_branch, "origin")
        if ref_pair is None and not raw_branch.startswith("refs/"):
            ref_pair = scm.GIT.RefToRemoteRef(f"heads/{raw_branch}", "origin")
        remote_ref = "".join(ref_pair) if ref_pair else ""
        mapped_ref = (
            scm.GIT.RemoteRefToRef(remote_ref, "origin") if remote_ref else None
        )
        canonical_ref = (
            mapped_ref
            if isinstance(mapped_ref, str) and mapped_ref
            else raw_branch
        )
        return remote_ref, canonical_ref


@dataclass(frozen=True)
class _WalkContext:
    repo_dir: Path
    display_path: str
    base_shas: Mapping[str, str]
    parse_rolls: bool
    deps_file: str
    parent_vars: _VariableMap
    level: int = 0


@dataclass(frozen=True)
class _RollTarget:
    roll: SubrepoRoll
    parse_rolls: bool
    deps_file: str
    parent_vars: _VariableMap


@dataclass(frozen=True)
class _CommitRollSummary:
    is_roll: bool = False
    targets: tuple[_RollTarget, ...] = ()


class CommitDepsCollector:
    """Recursively walks commits and expands subrepository rolls."""

    def __init__(
        self,
        repo: Path = Path("."),
        *,
        cwd: Path | None = None,
        first_parent: bool = False,
        max_level: int | None = None,
        max_count: int | None = None,
        filters: Sequence[str] = (),
        fetch: bool = False,
    ) -> None:
        repo_dir, main_worktree = self._resolve_checkout(repo, cwd=cwd)
        display_path, gclient_root = self._resolve_gclient_location(
            repo_dir, main_worktree
        )
        self.repo_dir: Final[Path] = repo_dir
        self.display_path: Final[str] = display_path
        self._gclient_root: Final[Path | None] = gclient_root
        self._main_worktree: Final[Path | None] = main_worktree
        self.is_standalone: Final[bool] = gclient_root is None
        self.first_parent: Final[bool] = first_parent
        self.max_level: Final[int | None] = max_level
        self.max_count: Final[int | None] = max_count
        self.fetch: Final[bool] = fetch
        self._active_ranges: Final[set[tuple[Path, str, str]]] = set()
        self._repo_filter: Final[RepoFilter] = RepoFilter(filters)
        self._deps_evaluator: Final[GclientDepsEvaluator] = (
            GclientDepsEvaluator(repo_dir, display_path, gclient_root)
        )
        self._root_context: Final[_WalkContext] = (
            self._deps_evaluator.resolve_root_context()
        )

    def collect(
        self,
        start_ref: CommitRange | str,
        end_ref: str | None = None,
        *,
        include_start: bool = False,
        is_range: bool = True,
        use_merge_base: bool = False,
        max_count: int | None = None,
    ) -> list[GitCommitEntry]:
        effective_max_count = self.max_count if max_count is None else max_count
        requested_range = self._coerce_commit_range(
            start_ref,
            end_ref,
            is_range=is_range,
            use_merge_base=use_merge_base,
            max_count=effective_max_count,
        )
        sha_range = InputParser.resolve_commit_range(
            self.repo_dir, requested_range
        )
        commits = self._list_root_commits(
            sha_range,
            requested_range,
            include_start=include_start,
            max_count=effective_max_count,
        )
        return self._walk_commits(sha_range, commits, self._root_context)

    def _resolve_checkout(
        self, repo: Path, *, cwd: Path | None
    ) -> tuple[Path, Path | None]:
        base_dir = cwd or Path(".")
        candidate_dir = _abspath(base_dir / repo.expanduser())
        if (candidate_dir / ".git").exists():
            return candidate_dir, self._find_main_worktree(candidate_dir)
        try:
            checkout_root = scm.GIT.GetCheckoutRoot(str(candidate_dir))
        except (subprocess2.CalledProcessError, OSError) as exc:
            raise CommitDepsError(
                f"Could not resolve repository: {repo!r}"
            ) from exc
        repo_dir = _abspath(Path(checkout_root))
        if not (repo_dir / ".git").exists():
            raise CommitDepsError(f"Could not resolve repository: {repo!r}")
        return repo_dir, self._find_main_worktree(repo_dir)

    def _find_main_worktree(self, repo_dir: Path) -> Path | None:
        if (repo_dir / ".git").is_dir():
            return None
        with contextlib.suppress(subprocess2.CalledProcessError, OSError):
            common_git_dir = _abspath(
                Path(scm.GIT.GetCommonGitDir(str(repo_dir)))
            )
            worktree_dir = (
                common_git_dir.parent if common_git_dir.name == ".git" else None
            )
            if worktree_dir is None:
                configured_worktree = scm.GIT.GetConfig(
                    str(repo_dir), "core.worktree"
                )
                if configured_worktree:
                    worktree_dir = _abspath(
                        common_git_dir / configured_worktree
                    )
            if (
                worktree_dir is not None
                and worktree_dir.is_dir()
                and worktree_dir.resolve() != repo_dir.resolve()
            ):
                return worktree_dir
        return None

    def _resolve_gclient_location(
        self, repo_dir: Path, main_worktree: Path | None
    ) -> tuple[str, Path | None]:
        found_root = gclient_paths.FindGclientRoot(str(repo_dir))
        anchor_dir = repo_dir if found_root else (main_worktree or repo_dir)
        if not found_root and main_worktree is not None:
            found_root = gclient_paths.FindGclientRoot(str(main_worktree))
        if not found_root:
            return repo_dir.name or ".", None
        gclient_root = _abspath(Path(found_root))
        if anchor_dir == gclient_root:
            return anchor_dir.name or ".", gclient_root
        for candidate_anchor, candidate_root in (
            (anchor_dir, gclient_root),
            (anchor_dir.resolve(), gclient_root.resolve()),
        ):
            with contextlib.suppress(ValueError):
                rel_display = candidate_anchor.relative_to(
                    candidate_root
                ).as_posix()
                return rel_display or ".", gclient_root
        return repo_dir.name or ".", None

    def _coerce_commit_range(
        self,
        start_ref: CommitRange | str,
        end_ref: str | None,
        *,
        is_range: bool,
        use_merge_base: bool,
        max_count: int | None,
    ) -> CommitRange:
        if isinstance(start_ref, CommitRange):
            return start_ref
        if end_ref is None:
            return CommitRange.from_args([start_ref], max_count=max_count)
        return CommitRange(start_ref, end_ref, is_range, use_merge_base)

    def _list_root_commits(
        self,
        sha_range: CommitRange,
        requested_range: CommitRange,
        *,
        include_start: bool,
        max_count: int | None,
    ) -> list[scm.CommitLogEntry]:
        if sha_range.is_single_commit:
            return self._get_commit_log(
                self.repo_dir, sha_range.end, single_commit=True
            )
        if sha_range.start == sha_range.end:
            is_implicit_count = (
                bool(max_count)
                and requested_range.start == requested_range.end
                and not requested_range.use_merge_base
            )
            if not is_implicit_count and not include_start:
                return []
            return self._get_commit_log(
                self.repo_dir,
                sha_range.end,
                max_count=max_count if is_implicit_count else None,
                single_commit=not is_implicit_count,
            )
        commits = self._get_commit_log(
            self.repo_dir,
            f"{sha_range.start}..{sha_range.end}",
            max_count=max_count,
        )
        if include_start:
            return self._prepend_start_commit(
                sha_range.start, commits, max_count=max_count
            )
        return commits

    def _prepend_start_commit(
        self,
        start_sha: str,
        commits: Sequence[scm.CommitLogEntry],
        *,
        max_count: int | None,
    ) -> list[scm.CommitLogEntry]:
        start_commits = self._get_commit_log(
            self.repo_dir, start_sha, single_commit=True
        )
        seen_shas = {entry.sha for entry in start_commits}
        merged_commits = [
            *start_commits,
            *(commit for commit in commits if commit.sha not in seen_shas),
        ]
        if max_count is not None:
            return merged_commits[-max_count:]
        return merged_commits

    def _walk_commits(
        self,
        sha_range: CommitRange,
        commits: Sequence[scm.CommitLogEntry],
        context: _WalkContext,
    ) -> list[GitCommitEntry]:
        if not commits:
            return []
        range_key = (context.repo_dir, sha_range.start, sha_range.end)
        if range_key in self._active_ranges:
            return []
        session = _RepoDepsSession(
            context, self._deps_evaluator, commits, self._repo_filter
        )
        self._active_ranges.add(range_key)
        try:
            entries: list[GitCommitEntry] = []
            for commit in commits:
                entries.extend(self._collect_commit(commit, context, session))
            return entries
        finally:
            self._active_ranges.discard(range_key)

    def _collect_commit(
        self,
        commit: scm.CommitLogEntry,
        context: _WalkContext,
        session: _RepoDepsSession,
    ) -> list[GitCommitEntry]:
        roll_summary = session.inspect_commit(commit)
        entries: list[GitCommitEntry] = []
        can_expand = self.max_level is None or context.level < self.max_level
        if roll_summary.targets and can_expand:
            entries.extend(
                self._expand_roll_targets(commit, context, roll_summary.targets)
            )
        entry_shas = tuple(
            {**context.base_shas, context.display_path: commit.sha}.items()
        )
        entries.append(
            GitCommitEntry(
                path=context.display_path,
                level=context.level,
                commit=commit.sha,
                roll=roll_summary.is_roll,
                subject=commit.subject,
                commit_position=commit.commit_position,
                pinned_shas=entry_shas,
            )
        )
        return entries

    def _expand_roll_targets(
        self,
        commit: scm.CommitLogEntry,
        context: _WalkContext,
        targets: Sequence[_RollTarget],
    ) -> list[GitCommitEntry]:
        parent_sha = (
            _try_resolve_commit(context.repo_dir, f"{commit.sha}~1")
            or commit.sha
        )
        child_shas = {**context.base_shas, context.display_path: parent_sha}
        entries: list[GitCommitEntry] = []
        for target in targets:
            subrepo_display, subrepo_entries = self._expand_single_target(
                context, target, child_shas
            )
            entries.extend(subrepo_entries)
            if subrepo_display is not None:
                child_shas[subrepo_display] = target.roll.new_sha
        return entries

    def _expand_single_target(
        self,
        context: _WalkContext,
        target: _RollTarget,
        child_shas: Mapping[str, str],
    ) -> tuple[str | None, list[GitCommitEntry]]:
        roll = target.roll
        child_display = _join_display_path(context.display_path, roll.rel_path)
        subrepo_dir = self._find_subrepo_dir(
            context.repo_dir, roll.rel_path, child_display
        )
        if subrepo_dir is None:
            _warn_skipped_roll(child_display, roll.old_sha, roll.new_sha)
            return None, []
        sha_range = self._resolve_subrepo_roll(subrepo_dir, child_display, roll)
        if sha_range is None:
            return child_display, []
        if (subrepo_dir, sha_range.start, sha_range.end) in self._active_ranges:
            return child_display, []
        commits = self._get_commit_log(
            subrepo_dir, f"{sha_range.start}..{sha_range.end}"
        ) or self._list_reverted_roll_commits(subrepo_dir, sha_range)
        child_context = _WalkContext(
            repo_dir=subrepo_dir,
            display_path=child_display,
            base_shas=dict(child_shas),
            parse_rolls=target.parse_rolls,
            deps_file=target.deps_file,
            parent_vars=target.parent_vars,
            level=context.level + 1,
        )
        return child_display, self._walk_commits(
            sha_range, commits, child_context
        )

    def _find_subrepo_dir(
        self, parent_dir: Path, rel_path: str, child_display: str
    ) -> Path | None:
        norm_rel_path = _normalize_posix_path(rel_path)
        local_subrepo = _abspath(parent_dir / norm_rel_path)
        candidates: list[Path] = [local_subrepo]
        if self._main_worktree is not None:
            with contextlib.suppress(ValueError):
                rel_from_root = local_subrepo.relative_to(self.repo_dir)
                candidates.append(_abspath(self._main_worktree / rel_from_root))
        if self._gclient_root is not None:
            candidates.append(_abspath(self._gclient_root / child_display))
        for candidate in candidates:
            if (candidate / ".git").exists():
                return candidate
        return None

    def _resolve_subrepo_roll(
        self, subrepo_dir: Path, display_path: str, roll: SubrepoRoll
    ) -> CommitRange | None:
        start_sha = _try_resolve_commit(subrepo_dir, roll.old_sha)
        end_sha = _try_resolve_commit(subrepo_dir, roll.new_sha)
        if (not start_sha or not end_sha) and self.fetch:
            missing_shas = [
                sha
                for sha, resolved in (
                    (roll.old_sha, start_sha),
                    (roll.new_sha, end_sha),
                )
                if not resolved
            ]
            _capture_git(
                subrepo_dir,
                "fetch",
                "--no-tags",
                "--quiet",
                "origin",
                *missing_shas,
            )
            start_sha = start_sha or _try_resolve_commit(
                subrepo_dir, roll.old_sha
            )
            end_sha = end_sha or _try_resolve_commit(subrepo_dir, roll.new_sha)
        if start_sha and end_sha:
            return CommitRange(start_sha, end_sha, is_range=True)
        _warn_skipped_roll(
            display_path, roll.old_sha, roll.new_sha, unfetched=True
        )
        return None

    def _list_reverted_roll_commits(
        self, subrepo_dir: Path, sha_range: CommitRange
    ) -> list[scm.CommitLogEntry]:
        forward_commits = self._get_commit_log(
            subrepo_dir, f"{sha_range.end}..{sha_range.start}"
        )
        if not forward_commits:
            return []
        endpoint_commits = self._get_commit_log(
            subrepo_dir, sha_range.end, single_commit=True
        )
        target_commits = [*reversed(forward_commits[:-1]), *endpoint_commits]
        reverted_commits: list[scm.CommitLogEntry] = []
        for source_commit, target_commit in zip(
            reversed(forward_commits), target_commits
        ):
            inverted_diffs = tuple(
                scm.DiffTreeEntry(
                    diff.new_mode,
                    diff.old_mode,
                    diff.new_sha,
                    diff.old_sha,
                    diff.file_path,
                )
                for diff in source_commit.diffs
            )
            reverted_commits.append(
                target_commit._replace(diffs=inverted_diffs)
            )
        return reverted_commits

    def _get_commit_log(
        self,
        repo_dir: Path,
        rev_spec: str,
        *,
        max_count: int | None = None,
        single_commit: bool = False,
    ) -> list[scm.CommitLogEntry]:
        return scm.GIT.GetCommitLogDiffs(
            str(repo_dir),
            rev_spec,
            first_parent=self.first_parent,
            max_count=max_count,
            single_commit=single_commit,
        )


@dataclass(frozen=True)
class _ParsedGitmodules:
    deps: Mapping[str, _DepInfo]
    recursedeps: Mapping[str, str]


@dataclass
class _ParsedDeps:
    git_shas: dict[str, str] = field(default_factory=dict)
    non_git_deps: dict[str, _NonGitDep] = field(default_factory=dict)
    repo_urls: dict[str, str] = field(default_factory=dict)
    disabled_paths: set[str] = field(default_factory=set)
    recursedeps: dict[str, str] = field(default_factory=dict)
    effective_vars: _VariableMap = field(default_factory=dict)
    is_sync_deps: bool = False
    has_deps_config: bool = True


_EMPTY_GITMODULES: Final = _ParsedGitmodules({}, {})
_EMPTY_PARSED_DEPS: Final = _ParsedDeps(has_deps_config=False)


class GclientDepsEvaluator:
    """Evaluates and caches `.gclient`, `DEPS`, and `.gitmodules` metadata."""

    def __init__(
        self,
        repo_dir: Path,
        display_path: str,
        gclient_root: Path | None,
    ) -> None:
        self._repo_dir: Final[Path] = repo_dir
        self._display_path: Final[str] = display_path
        self._gclient_root: Final[Path | None] = gclient_root
        self._deps_cache: Final[
            dict[tuple[Path, str, str, str], _ParsedDeps | None]
        ] = {}
        self._gitmodules_cache: Final[
            dict[tuple[Path, str], _ParsedGitmodules]
        ] = {}
        solution = self._load_gclient_dep()
        self._builtin_vars: Final[_VariableMap] = solution.get_builtin_vars()
        self._custom_vars: Final[_VariableMap] = solution.custom_vars
        self._default_vars: Final[_EvalVarsDict] = self._merge_vars(
            self._builtin_vars, self._custom_vars
        )
        self._custom_deps: Final[Mapping[str, str | bool | None]] = (
            solution.custom_deps
        )
        self._solution_name: Final[str] = (solution.name or "").strip("/")
        self._deps_file: Final[str] = (
            _normalize_posix_path(solution.deps_file)
            if solution.name and solution.deps_file
            else "DEPS"
        )

    def resolve_root_context(self) -> _WalkContext:
        solution_prefix = f"{self._solution_name}/"
        if (
            self._gclient_root is not None
            and self._solution_name
            and self._display_path.startswith(solution_prefix)
        ):
            remaining_path = self._display_path.removeprefix(solution_prefix)
            return self._walk_ancestor_recursedeps(
                self._gclient_root, remaining_path
            )
        deps_file = self._deps_file if self._gclient_root else "DEPS"
        return _WalkContext(
            self._repo_dir, self._display_path, {}, True, deps_file, {}
        )

    def parse_deps_state(
        self,
        repo_dir: Path,
        display_path: str,
        deps_blob_sha: str,
        gitmodules_ref: str,
        deps_file: str,
        parent_vars: _VariableMap,
    ) -> _ParsedDeps | None:
        cache_key = (
            repo_dir,
            deps_blob_sha,
            gitmodules_ref,
            repr(parent_vars),
        )
        if cache_key in self._deps_cache:
            return self._deps_cache[cache_key]
        evaluated = self._evaluate_deps_blob(
            repo_dir, deps_blob_sha, deps_file, parent_vars
        )
        if evaluated is None:
            return self._deps_cache.setdefault(cache_key, None)
        scope, effective_vars = evaluated
        has_deps_blob = deps_blob_sha != scm.ZERO_SHA
        git_deps_mode = scope.get("git_dependencies", gclient_eval.DEPS)
        submodules = (
            self._load_gitmodules(repo_dir, gitmodules_ref)
            if not has_deps_blob or git_deps_mode == gclient_eval.SUBMODULES
            else _EMPTY_GITMODULES
        )
        parsed_deps = (
            _EMPTY_PARSED_DEPS
            if not has_deps_blob and not submodules.deps
            else self._build_parsed_deps(
                repo_dir, display_path, scope, submodules, effective_vars
            )
        )
        return self._deps_cache.setdefault(cache_key, parsed_deps)

    def _load_gclient_dep(self) -> gclient.Dependency:
        options = types.SimpleNamespace(deps_os=None, verbose=False)
        client_root = self._gclient_root or self._repo_dir
        client = gclient.GClient(str(client_root), options)
        if self._gclient_root is None:
            return client
        config_path = self._gclient_root / ".gclient"
        if config_path.is_file():
            with contextlib.suppress(*_PARSE_ERRORS, OSError):
                client.SetConfig(gclient_utils.FileRead(str(config_path)))
        display_prefix = f"{self._display_path}/"
        for solution in client.dependencies:
            solution_name = (solution.name or "").strip("/")
            if solution_name and display_prefix.startswith(f"{solution_name}/"):
                return solution
        return client.dependencies[0] if client.dependencies else client

    def _walk_ancestor_recursedeps(
        self, gclient_root: Path, remaining_path: str
    ) -> _WalkContext:
        current_display = self._solution_name
        current_dir: Path | None = _abspath(gclient_root / current_display)
        base_shas: dict[str, str] = {}
        deps_file = self._deps_file
        parent_vars: _VariableMap = {}
        while remaining_path and current_dir is not None:
            head_sha = _try_resolve_commit(current_dir, "HEAD")
            if not head_sha:
                break
            base_shas[current_display] = head_sha
            parsed_deps = self._parse_head_deps(
                current_dir, current_display, head_sha, deps_file, parent_vars
            )
            matched_dep = self._find_recursedep_prefix(
                parsed_deps, remaining_path
            )
            if not matched_dep or parsed_deps is None:
                break
            parent_vars = parsed_deps.effective_vars
            deps_file = parsed_deps.recursedeps[matched_dep]
            remaining_path = remaining_path[len(matched_dep) + 1 :]
            if remaining_path:
                current_display = _join_display_path(
                    current_display, matched_dep
                )
                next_dir = _abspath(
                    current_dir / _normalize_posix_path(matched_dep)
                )
                current_dir = next_dir if (next_dir / ".git").exists() else None
        parse_rolls = bool(base_shas and not remaining_path)
        return _WalkContext(
            self._repo_dir,
            self._display_path,
            base_shas,
            parse_rolls,
            deps_file,
            parent_vars,
        )

    def _parse_head_deps(
        self,
        repo_dir: Path,
        display_path: str,
        head_sha: str,
        deps_file: str,
        parent_vars: _VariableMap,
    ) -> _ParsedDeps | None:
        deps_blob = (
            _try_resolve_commit(repo_dir, f"{head_sha}:{deps_file}")
            or _try_resolve_commit(repo_dir, f"{head_sha}:DEPS")
            or scm.ZERO_SHA
        )
        return self.parse_deps_state(
            repo_dir,
            display_path,
            deps_blob,
            f"{head_sha}:.gitmodules",
            deps_file,
            parent_vars,
        )

    def _find_recursedep_prefix(
        self, parsed_deps: _ParsedDeps | None, remaining_path: str
    ) -> str:
        if parsed_deps is None:
            return ""
        for dep_path in parsed_deps.recursedeps:
            if f"{remaining_path}/".startswith(f"{dep_path}/"):
                return dep_path
        return ""

    def _evaluate_deps_blob(
        self,
        repo_dir: Path,
        deps_blob_sha: str,
        deps_file: str,
        parent_vars: _VariableMap,
    ) -> tuple[_DepsScope, _VariableMap] | None:
        vars_override: _VariableMap = (
            self._merge_vars(parent_vars, self._default_vars)
            if parent_vars
            else self._default_vars
        )
        if deps_blob_sha == scm.ZERO_SHA:
            return {}, vars_override
        deps_text = scm.GIT.GetBlob(str(repo_dir), deps_blob_sha)
        if deps_text is None:
            return None
        try:
            scope: _DepsScope = gclient_eval.Parse(
                deps_text,
                deps_file,
                vars_override=vars_override,
                builtin_vars=self._builtin_vars,
            )
        except _PARSE_ERRORS:
            return None
        target_os = scope.get("target_os")
        if not target_os:
            return scope, self._merge_vars(scope.get("vars", {}), vars_override)
        builtin_vars = dict(self._builtin_vars)
        for os_name in target_os:
            mapped_os = _TARGET_OS_ALIASES.get(os_name or "", os_name or "")
            if f"checkout_{mapped_os}" in builtin_vars:
                builtin_vars[f"checkout_{mapped_os}"] = True
        effective_vars = self._merge_vars(
            scope.get("vars", {}), parent_vars, builtin_vars, self._custom_vars
        )
        return scope, effective_vars

    def _merge_vars(self, *variable_maps: _VariableMap) -> _EvalVarsDict:
        merged_vars: _EvalVarsDict = {}
        for variable_map in variable_maps:
            for var_name, var_value in variable_map.items():
                is_const_str = isinstance(
                    merged_vars.get(var_name), gclient_eval.ConstantString
                )
                if is_const_str and isinstance(var_value, str):
                    merged_vars[var_name] = gclient_eval.ConstantString(
                        var_value
                    )
                else:
                    merged_vars[var_name] = var_value
        return merged_vars

    def _load_gitmodules(
        self, repo_dir: Path, gitmodules_ref: str
    ) -> _ParsedGitmodules:
        if gitmodules_ref in ("", scm.ZERO_SHA):
            return _EMPTY_GITMODULES
        cache_key = (repo_dir, gitmodules_ref)
        cached = self._gitmodules_cache.get(cache_key)
        if cached is not None:
            return cached
        deps: dict[str, _DepInfo] = {}
        recursedeps: dict[str, str] = {}
        for section in self._read_gitmodules_sections(repo_dir, gitmodules_ref):
            rel_path = _normalize_posix_path(section.get("path", ""))
            if rel_path in ("", "."):
                continue
            deps[rel_path] = {
                "dep_type": "git",
                "url": section.get("url", ""),
                "condition": section.get("gclient-condition", ""),
            }
            if section.get("gclient-recursedeps", "").lower() == "true":
                recursedeps[rel_path] = "DEPS"
        parsed = _ParsedGitmodules(deps, recursedeps)
        self._gitmodules_cache[cache_key] = parsed
        return parsed

    def _read_gitmodules_sections(
        self, repo_dir: Path, gitmodules_ref: str
    ) -> tuple[_SubmoduleSection, ...]:
        sections_by_name: dict[str, dict[str, str]] = {}
        raw_config = _capture_git(
            repo_dir, "config", "--blob", gitmodules_ref, "-l"
        )
        for config_line in raw_config.splitlines():
            config_key, separator, config_value = config_line.partition("=")
            if not separator or not config_key.startswith("submodule."):
                continue
            subsection_and_key = config_key.removeprefix("submodule.")
            if "." not in subsection_and_key:
                continue
            submodule_name, option_name = subsection_and_key.rsplit(".", 1)
            sections_by_name.setdefault(submodule_name, {})[option_name] = (
                config_value.strip()
            )
        return tuple(sections_by_name.values())

    def _build_parsed_deps(
        self,
        repo_dir: Path,
        display_path: str,
        scope: _DepsScope,
        submodules: _ParsedGitmodules,
        vars_map: _VariableMap,
    ) -> _ParsedDeps:
        merged_deps, recursedeps = self._merge_deps_and_recursedeps(
            repo_dir, display_path, scope, submodules
        )
        parsed = _ParsedDeps(
            recursedeps=recursedeps,
            effective_vars=vars_map,
            is_sync_deps=scope.get("git_dependencies", "DEPS") != "DEPS",
        )
        for rel_path, dep_info in merged_deps.items():
            if self._apply_custom_dep(display_path, rel_path, parsed):
                continue
            dep_type = dep_info.get("dep_type", "git")
            if not self._evaluate_condition(
                dep_info.get("condition"), vars_map
            ):
                if dep_type == "git":
                    parsed.disabled_paths.add(rel_path)
                continue
            if dep_type == "git":
                self._record_git_dep(rel_path, dep_info.get("url"), parsed)
            else:
                self._record_non_git_dep(
                    rel_path, dep_type, dep_info, vars_map, parsed
                )
        return parsed

    def _merge_deps_and_recursedeps(
        self,
        repo_dir: Path,
        display_path: str,
        scope: _DepsScope,
        submodules: _ParsedGitmodules,
    ) -> tuple[dict[str, _DepInfo], dict[str, str]]:
        prefixes = (
            ()
            if scope.get("use_relative_paths")
            else (display_path, repo_dir.name)
        )
        merged_deps: dict[str, _DepInfo] = {}
        for raw_key, dep_info in scope.get("deps", {}).items():
            rel_path = self._normalize_dep_path(repo_dir, raw_key, prefixes)
            if rel_path:
                merged_deps[rel_path] = dep_info
        merged_deps.update(submodules.deps)

        recursedeps = dict(submodules.recursedeps)
        for entry in scope.get("recursedeps", ()):
            raw_dep, raw_file = (
                (entry, "DEPS") if isinstance(entry, str) else entry
            )
            rel_path = self._normalize_dep_path(repo_dir, raw_dep, prefixes)
            if rel_path:
                recursedeps[rel_path] = _normalize_posix_path(raw_file)
        return merged_deps, recursedeps

    def _apply_custom_dep(
        self, display_path: str, rel_path: str, parsed: _ParsedDeps
    ) -> bool:
        if not self._custom_deps:
            return False
        candidates = (
            _join_display_path(display_path, rel_path),
            rel_path,
            f"src/{rel_path}",
        )
        for dep_key in candidates:
            if dep_key in self._custom_deps:
                custom_value = self._custom_deps[dep_key]
                break
        else:
            return False
        if not isinstance(custom_value, str) or custom_value.endswith(
            "@unmanaged"
        ):
            parsed.disabled_paths.add(rel_path)
            return True
        custom_url, custom_revision = gclient_utils.SplitUrlRevision(
            custom_value
        )
        if custom_url:
            parsed.repo_urls[rel_path] = custom_url
        if custom_revision:
            parsed.disabled_paths.add(rel_path)
            return True
        return False

    def _record_git_dep(
        self, rel_path: str, dep_url: str | None, parsed: _ParsedDeps
    ) -> None:
        if dep_url is None:
            parsed.disabled_paths.add(rel_path)
            return
        if not dep_url:
            return
        with contextlib.suppress(gclient_utils.Error):
            origin_url, revision = gclient_utils.SplitUrlRevision(dep_url)
            if origin_url:
                parsed.repo_urls.setdefault(rel_path, origin_url)
            if revision and gclient_utils.IsFullGitSha(revision):
                parsed.git_shas[rel_path] = revision.lower()

    def _record_non_git_dep(
        self,
        rel_path: str,
        dep_type: str,
        dep_info: _DepInfo,
        vars_map: _VariableMap,
        parsed: _ParsedDeps,
    ) -> None:
        if dep_type == "cipd":
            parsed.non_git_deps[rel_path] = ("cipd", dep_info.get("packages"))
            return
        if dep_type != "gcs":
            return
        active_objects = [
            gcs_object
            for gcs_object in dep_info.get("objects", ())
            if self._evaluate_condition(gcs_object.get("condition"), vars_map)
        ]
        if active_objects:
            parsed.non_git_deps[rel_path] = (
                "gcs",
                dep_info.get("bucket"),
                active_objects,
            )

    def _evaluate_condition(
        self, condition: str | None, effective_vars: _VariableMap
    ) -> bool:
        if not condition:
            return True
        with contextlib.suppress(*_PARSE_ERRORS):
            return bool(
                gclient_eval.EvaluateCondition(condition, effective_vars)
            )
        return False

    def _normalize_dep_path(
        self, repo_dir: Path, raw_key: str, strip_prefixes: Sequence[str]
    ) -> str:
        dep_path = _normalize_posix_path(raw_key)
        if dep_path in ("", "."):
            return ""
        if not strip_prefixes:
            return dep_path
        for prefix in strip_prefixes:
            if prefix and dep_path.startswith(f"{prefix}/"):
                return dep_path.removeprefix(f"{prefix}/")
        if dep_path.startswith("src/") and not (repo_dir / dep_path).exists():
            return dep_path.removeprefix("src/")
        return dep_path


class _RepoDepsSession:
    """Tracks temporal `DEPS` and `.gitmodules` state for one repository."""

    def __init__(
        self,
        context: _WalkContext,
        evaluator: GclientDepsEvaluator,
        commits: Sequence[scm.CommitLogEntry],
        repo_filter: RepoFilter,
    ) -> None:
        tip_sha = commits[-1].sha
        initial_blobs = {
            diff.file_path: diff.old_sha
            for commit in reversed(commits)
            for diff in commit.diffs
        }
        deps_file = context.deps_file
        if (
            context.parse_rolls
            and deps_file != "DEPS"
            and deps_file not in initial_blobs
            and not _try_resolve_commit(
                context.repo_dir, f"{tip_sha}:{deps_file}"
            )
        ):
            deps_file = "DEPS"
        self.repo_dir: Final[Path] = context.repo_dir
        self.display_path: Final[str] = context.display_path
        self._evaluator: Final[GclientDepsEvaluator] = evaluator
        self._repo_filter: Final[RepoFilter] = repo_filter
        self._parse_rolls: Final[bool] = context.parse_rolls
        self._deps_file: Final[str] = deps_file
        self._parent_vars: Final[_VariableMap] = context.parent_vars
        self._tip_sha: Final[str] = tip_sha
        self._deps_blob: str = initial_blobs.get(deps_file, "")
        self._gitmodules_ref: str = initial_blobs.get(
            ".gitmodules", f"{tip_sha}:.gitmodules"
        )
        self._current_deps: _ParsedDeps | None = None

    def inspect_commit(self, commit: scm.CommitLogEntry) -> _CommitRollSummary:
        if not self._parse_rolls:
            return _CommitRollSummary()
        diffs_by_path = {diff.file_path: diff for diff in commit.diffs}
        deps_diff = diffs_by_path.get(self._deps_file)
        gitmodules_diff = diffs_by_path.get(".gitmodules")
        gitlink_diffs = [
            diff for diff in commit.diffs if diff.is_gitlink_change
        ]
        if deps_diff is None and gitmodules_diff is None and not gitlink_diffs:
            return _CommitRollSummary()
        old_deps, new_deps = self._advance_commit_deps(
            deps_diff, gitmodules_diff, gitlink_diffs
        )
        return self._build_roll_summary(
            old_deps, new_deps, deps_diff, gitlink_diffs
        )

    def _advance_commit_deps(
        self,
        deps_diff: scm.DiffTreeEntry | None,
        gitmodules_diff: scm.DiffTreeEntry | None,
        gitlink_diffs: Sequence[scm.DiffTreeEntry],
    ) -> tuple[_ParsedDeps | None, _ParsedDeps | None]:
        if not self._deps_blob:
            tip_blob = _try_resolve_commit(
                self.repo_dir, f"{self._tip_sha}:{self._deps_file}"
            )
            self._deps_blob = tip_blob or scm.ZERO_SHA
        old_blob = deps_diff.old_sha if deps_diff else self._deps_blob
        new_blob = deps_diff.new_sha if deps_diff else self._deps_blob
        old_gitmodules = (
            gitmodules_diff.old_sha if gitmodules_diff else self._gitmodules_ref
        )
        new_gitmodules = (
            gitmodules_diff.new_sha if gitmodules_diff else self._gitmodules_ref
        )
        old_deps = self._resolve_old_deps(old_blob, old_gitmodules)
        new_deps = self._resolve_new_deps(
            old_deps,
            old_blob,
            new_blob,
            old_gitmodules,
            new_gitmodules,
            gitlink_diffs,
        )
        self._current_deps = new_deps
        self._deps_blob = new_blob
        self._gitmodules_ref = new_gitmodules
        return old_deps, new_deps

    def _resolve_old_deps(
        self, old_deps_blob: str, old_gitmodules: str
    ) -> _ParsedDeps | None:
        matches_current = (
            old_deps_blob == self._deps_blob
            and old_gitmodules == self._gitmodules_ref
        )
        if matches_current and self._current_deps is not None:
            return self._current_deps
        return self._evaluator.parse_deps_state(
            self.repo_dir,
            self.display_path,
            old_deps_blob,
            old_gitmodules,
            self._deps_file,
            self._parent_vars,
        )

    def _resolve_new_deps(
        self,
        old_deps: _ParsedDeps | None,
        old_deps_blob: str,
        new_deps_blob: str,
        old_gitmodules: str,
        new_gitmodules: str,
        gitlink_diffs: Sequence[scm.DiffTreeEntry],
    ) -> _ParsedDeps | None:
        if (old_deps_blob, old_gitmodules) == (new_deps_blob, new_gitmodules):
            return old_deps
        if old_deps is not None and old_gitmodules == new_gitmodules:
            fast_deps = self._try_fast_sync_update(
                old_deps, old_deps_blob, new_deps_blob, gitlink_diffs
            )
            if fast_deps is not None:
                return fast_deps
        return self._evaluator.parse_deps_state(
            self.repo_dir,
            self.display_path,
            new_deps_blob,
            new_gitmodules,
            self._deps_file,
            self._parent_vars,
        )

    def _try_fast_sync_update(
        self,
        old_deps: _ParsedDeps,
        old_deps_blob: str,
        new_deps_blob: str,
        gitlink_diffs: Sequence[scm.DiffTreeEntry],
    ) -> _ParsedDeps | None:
        if not old_deps.is_sync_deps or scm.ZERO_SHA in (
            old_deps_blob,
            new_deps_blob,
        ):
            return None
        gitlink_rolls = [diff for diff in gitlink_diffs if diff.is_gitlink_roll]
        if not gitlink_rolls:
            return None
        old_text = scm.GIT.GetBlob(str(self.repo_dir), old_deps_blob)
        new_text = scm.GIT.GetBlob(str(self.repo_dir), new_deps_blob)
        if old_text is None or new_text is None:
            return None
        for diff in gitlink_rolls:
            old_text = old_text.replace(diff.old_sha, diff.new_sha)
        if old_text != new_text:
            return None
        updated_shas = {
            diff.file_path: diff.new_sha
            for diff in gitlink_rolls
            if diff.file_path in old_deps.git_shas
        }
        return replace(old_deps, git_shas={**old_deps.git_shas, **updated_shas})

    def _build_roll_summary(
        self,
        old_deps: _ParsedDeps | None,
        new_deps: _ParsedDeps | None,
        deps_diff: scm.DiffTreeEntry | None,
        gitlink_diffs: Sequence[scm.DiffTreeEntry],
    ) -> _CommitRollSummary:
        rolls_by_path: dict[str, SubrepoRoll] = {}
        other_changed_paths: set[str] = set()
        if deps_diff and old_deps is not None and new_deps is not None:
            self._diff_parsed_deps(
                old_deps, new_deps, rolls_by_path, other_changed_paths
            )
        self._apply_gitlink_diffs(
            gitlink_diffs,
            old_deps,
            new_deps,
            rolls_by_path,
            other_changed_paths,
        )
        repo_urls = {
            **(old_deps.repo_urls if old_deps else {}),
            **(new_deps.repo_urls if new_deps else {}),
        }
        targets = self._select_roll_targets(rolls_by_path, new_deps, repo_urls)
        has_other_roll = any(
            self._matches_subrepo(rel_path, repo_urls)
            for rel_path in other_changed_paths - rolls_by_path.keys()
        )
        return _CommitRollSummary(
            is_roll=bool(targets) or has_other_roll, targets=targets
        )

    def _diff_parsed_deps(
        self,
        old_deps: _ParsedDeps,
        new_deps: _ParsedDeps,
        rolls_by_path: dict[str, SubrepoRoll],
        other_changed_paths: set[str],
    ) -> None:
        all_non_git_paths = (
            old_deps.non_git_deps.keys() | new_deps.non_git_deps.keys()
        )
        for dep_path in all_non_git_paths:
            if old_deps.non_git_deps.get(dep_path) != new_deps.non_git_deps.get(
                dep_path
            ):
                other_changed_paths.add(dep_path)
        if old_deps.is_sync_deps and new_deps.is_sync_deps:
            return
        old_git_shas = old_deps.git_shas
        new_git_shas = new_deps.git_shas
        other_changed_paths |= old_git_shas.keys() ^ new_git_shas.keys()
        for dep_path in old_git_shas.keys() & new_git_shas.keys():
            old_sha = old_git_shas[dep_path]
            new_sha = new_git_shas[dep_path]
            if old_sha != new_sha and scm.ZERO_SHA not in (old_sha, new_sha):
                rolls_by_path[dep_path] = SubrepoRoll(
                    dep_path, old_sha, new_sha
                )

    def _apply_gitlink_diffs(
        self,
        gitlink_diffs: Sequence[scm.DiffTreeEntry],
        old_deps: _ParsedDeps | None,
        new_deps: _ParsedDeps | None,
        rolls_by_path: dict[str, SubrepoRoll],
        other_changed_paths: set[str],
    ) -> None:
        old_disabled = old_deps.disabled_paths if old_deps else set()
        new_disabled = new_deps.disabled_paths if new_deps else set()
        for diff in gitlink_diffs:
            rel_path = diff.file_path
            if diff.is_gitlink_roll and rel_path in new_disabled:
                rolls_by_path.pop(rel_path, None)
            elif diff.is_gitlink_roll:
                rolls_by_path[rel_path] = SubrepoRoll.from_diff_entry(diff)
            else:
                is_added = diff.new_mode == scm.GITLINK_MODE
                active_disabled = new_disabled if is_added else old_disabled
                if rel_path in active_disabled:
                    continue
                previous_sha = (
                    old_deps.git_shas.get(rel_path)
                    if old_deps and is_added
                    else None
                )
                if previous_sha and diff.new_sha not in (
                    scm.ZERO_SHA,
                    previous_sha,
                ):
                    rolls_by_path[rel_path] = SubrepoRoll(
                        rel_path, previous_sha, diff.new_sha
                    )
                else:
                    other_changed_paths.add(rel_path)

    def _select_roll_targets(
        self,
        rolls_by_path: Mapping[str, SubrepoRoll],
        new_deps: _ParsedDeps | None,
        repo_urls: Mapping[str, str],
    ) -> tuple[_RollTarget, ...]:
        has_deps = new_deps is not None and new_deps.has_deps_config
        recursedeps = new_deps.recursedeps if new_deps else {}
        child_vars = (
            new_deps.effective_vars
            if new_deps and has_deps
            else self._parent_vars
        )
        targets: list[_RollTarget] = []
        for rel_path in sorted(rolls_by_path):
            parse_rolls = (not has_deps) or (rel_path in recursedeps)
            if self._matches_subrepo(
                rel_path, repo_urls, include_descendants=parse_rolls
            ):
                deps_file = recursedeps.get(rel_path, "DEPS")
                targets.append(
                    _RollTarget(
                        rolls_by_path[rel_path],
                        parse_rolls,
                        deps_file,
                        child_vars,
                    )
                )
        return tuple(targets)

    def _matches_subrepo(
        self,
        rel_path: str,
        repo_urls: Mapping[str, str],
        *,
        include_descendants: bool = False,
    ) -> bool:
        return self._repo_filter.matches(
            rel_path,
            _join_display_path(self.display_path, rel_path),
            repo_url=repo_urls.get(rel_path, ""),
            include_descendants=include_descendants,
        )


class RepoFilter:
    """Matches subrepository paths and remote URLs against `--filter` specs."""

    def __init__(self, raw_patterns: Sequence[str] = ()) -> None:
        self.patterns: Final[tuple[str, ...]] = tuple(
            self.normalize_pattern(pattern).removesuffix(".git").rstrip("/")
            for pattern in raw_patterns
        )

    @classmethod
    def normalize_pattern(cls, raw_pattern: str) -> str:
        stripped = raw_pattern.strip()
        if "://" in stripped:
            trimmed_url = stripped.rstrip("/")
            scheme, _, location = trimmed_url.partition("://")
            if not scheme or not location.removesuffix(".git").strip("/"):
                raise ValueError(raw_pattern)
            return trimmed_url
        normalized_path = posixpath.normpath(
            stripped.replace("\\", "/").strip("/")
        )
        if normalized_path in (".", "..", ".git"):
            raise ValueError(raw_pattern)
        if normalized_path.startswith("../"):
            raise ValueError(raw_pattern)
        return normalized_path

    def matches(
        self,
        *repo_paths: str,
        repo_url: str = "",
        include_descendants: bool = False,
    ) -> bool:
        if not self.patterns:
            return True
        clean_url = repo_url.rstrip("/").removesuffix(".git").rstrip("/")
        if clean_url and clean_url in self.patterns:
            return True
        for repo_path in repo_paths:
            if not repo_path or repo_path in (".", ".."):
                continue
            if any(
                f"/{pattern}/" in f"/{repo_path}/"
                or (include_descendants and pattern.startswith(f"{repo_path}/"))
                for pattern in self.patterns
            ):
                return True
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parsed_args = InputParser.parse_args(argv)
    try:
        return _run_cli(parsed_args)
    except CommitDepsError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        _silence_broken_pipe()
        return 0
    except KeyboardInterrupt:
        return 130


def _run_cli(parsed_args: argparse.Namespace) -> int:
    if gclient_utils.IsEnvCog():
        raise CommitDepsError(
            "commit_deps is not supported in non-git environments."
        )
    commit_range = CommitRange.from_args(
        parsed_args.commits, max_count=parsed_args.max_count
    )
    collector = CommitDepsCollector(
        parsed_args.repo,
        first_parent=parsed_args.first_parent,
        max_level=parsed_args.max_level,
        max_count=parsed_args.max_count,
        filters=parsed_args.filter,
        fetch=parsed_args.fetch,
    )
    entries = collector.collect(
        commit_range, include_start=parsed_args.include_start
    )
    if parsed_args.sync_command and not collector.is_standalone:
        _warn_if_attached_branch(collector)
    output_format: OutputFormat = parsed_args.format
    formatted_output = output_format.format_entries(
        entries,
        sync_command=parsed_args.sync_command,
        standalone=collector.is_standalone,
    )
    _write_output(formatted_output, output_format, has_entries=bool(entries))
    return 0


def _warn_if_attached_branch(collector: CommitDepsCollector) -> None:
    with contextlib.suppress(OSError):
        branch_name = scm.GIT.GetBranch(str(collector.repo_dir))
        if branch_name:
            print(
                f"Warning: {collector.display_path} is on branch "
                f"{branch_name!r}; run 'git checkout --detach' before "
                "'gclient sync --revision'.",
                file=sys.stderr,
            )


def _write_output(
    formatted_output: str, output_format: OutputFormat, *, has_entries: bool
) -> None:
    setup_color.init()
    if _should_use_pager(output_format, has_entries=has_entries):
        with git_common.less() as pager_stdin:
            pager_stdin.write(formatted_output.encode("utf-8", "replace"))
        return
    sys.stdout.write(formatted_output)
    sys.stdout.flush()


def _should_use_pager(
    output_format: OutputFormat, *, has_entries: bool
) -> bool:
    if output_format is not OutputFormat.TABLE or not has_entries:
        return False
    if not setup_color.IS_TTY or not shutil.which("less"):
        return False
    term_name = os.environ.get("TERM") or "dumb"
    pager_cmd = os.environ.get("GIT_PAGER", os.environ.get("PAGER", "less"))
    return term_name != "dumb" and pager_cmd not in ("", "cat")


def _silence_broken_pipe() -> None:
    with contextlib.suppress(OSError):
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull_fd, sys.stdout.fileno())
        finally:
            os.close(devnull_fd)


def _try_resolve_commit(repo_dir: Path, rev_spec: str) -> str | None:
    with contextlib.suppress(subprocess2.CalledProcessError, OSError):
        resolved = scm.GIT.ResolveCommit(str(repo_dir), rev_spec)
        return resolved.strip().lower() if resolved else None
    return None


def _capture_git(repo_dir: Path, *git_args: str) -> str:
    with contextlib.suppress(subprocess2.CalledProcessError, OSError):
        output = scm.GIT.Capture(list(git_args), cwd=str(repo_dir))
        return output.strip() if isinstance(output, str) else ""
    return ""


def _warn_skipped_roll(
    display_path: str, old_sha: str, new_sha: str, *, unfetched: bool = False
) -> None:
    reason = (
        "commit not fetched"
        if unfetched
        else "subrepository checkout not found"
    )
    print(
        f"Warning: Skipping unexpanded roll in {display_path} "
        f"({old_sha[:12]}..{new_sha[:12]}): {reason} locally.",
        file=sys.stderr,
    )


def _join_display_path(parent_display: str, rel_path: str) -> str:
    norm_rel_path = _normalize_posix_path(rel_path)
    if parent_display in ("", "."):
        joined_path = norm_rel_path
    else:
        joined_path = posixpath.normpath(f"{parent_display}/{norm_rel_path}")
    return parent_display if joined_path == "." else joined_path


def _normalize_posix_path(raw_path: str) -> str:
    return posixpath.normpath(raw_path.replace("\\", "/")).lstrip("/")


def _abspath(target_path: Path) -> Path:
    return Path(os.path.abspath(target_path))


if __name__ == "__main__":
    sys.exit(main())
