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

_COMMIT_POSITION_PREFIX_RE: Final = re.compile(r"(?i)\bcr-commit-position:\s*")
_COMMIT_POSITION_RE: Final = re.compile(
    r"(?i)^(?:cr-commit-position:\s*)?([\w/.-]*)@\{#(\d+)\}$"
)
_BARE_POSITION_RE: Final = re.compile(r"(?i)^r?(\d{1,8})$")
_CHROME_VERSION_RE: Final = re.compile(r"\d+\.\d+\.\d+\.\d+")
_REF_PREFIX_RE: Final = re.compile(
    r"^(?:refs/(?:remotes/(?:origin/)?|heads/)|origin/)"
)
_REPO_URL_RE: Final = re.compile(
    r"(?i)^(?:[a-z][a-z0-9+.-]*://|[\w.-]+@[\w.-]+:)"
)
_PARSE_ERRORS: Final = (
    gclient_utils.Error,
    SyntaxError,
    ValueError,
    KeyError,
    TypeError,
)
_MAIN_POSITION_REFS: Final = (
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

    def to_commit_range(self) -> CommitRange:
        return CommitRange(self.old_sha, self.new_sha)


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
    def _parse_commit_ref(cls, commit_ref: str) -> str:
        normalized_ref = _COMMIT_POSITION_PREFIX_RE.sub(
            "Cr-Commit-Position: ", commit_ref.strip()
        )
        remainder = normalized_ref.replace("Cr-Commit-Position: ", "")
        has_whitespace = any(char.isspace() for char in remainder)
        if (
            not normalized_ref
            or not normalized_ref.isprintable()
            or has_whitespace
        ):
            raise ValueError(commit_ref)
        return normalized_ref

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


@dataclass(frozen=True)
class _WalkContext:
    base_shas: Mapping[str, str]
    parse_rolls: bool
    deps_file: str
    parent_vars: _VariableMap
    repo_filter: RepoFilter
    level: int = 0
    include_start: bool = False
    max_count: int | None = None


@dataclass(frozen=True)
class _RollTarget:
    roll: SubrepoRoll
    parse_rolls: bool
    deps_file: str
    parent_vars: _VariableMap
    repo_filter: RepoFilter


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
        self._root_repo = GitRepo.resolve(
            repo, cwd=cwd, first_parent=first_parent
        )
        self.repo_dir = self._root_repo.path
        self.display_path = self._root_repo.display_path
        self.is_standalone = self._root_repo.gclient_root is None
        self.max_level = max_level
        self.max_count = max_count
        self.fetch = fetch
        self._active_ranges: set[tuple[Path, str, str]] = set()
        repo_filter = RepoFilter.from_patterns(filters)
        self._deps_evaluator = GclientDepsEvaluator(self._root_repo)
        self._root_context = self._deps_evaluator.resolve_root_context(
            self._root_repo, repo_filter
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
        commit_range = self._coerce_commit_range(
            start_ref,
            end_ref,
            is_range=is_range,
            use_merge_base=use_merge_base,
            max_count=effective_max_count,
        )
        walk_context = replace(
            self._root_context,
            include_start=include_start,
            max_count=effective_max_count,
        )
        return self._collect_repo(self._root_repo, commit_range, walk_context)

    def attached_branch_name(self) -> str:
        return self._root_repo.attached_branch_name()

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

    def _collect_repo(
        self, repo: GitRepo, commit_range: CommitRange, context: _WalkContext
    ) -> list[GitCommitEntry]:
        sha_range = repo.resolve_range(
            commit_range, is_subrepo=context.level > 0, fetch=self.fetch
        )
        if sha_range is None:
            return []
        range_key = (repo.path, sha_range.start, sha_range.end)
        if range_key in self._active_ranges:
            return []
        commits = repo.list_commits(
            sha_range,
            commit_range,
            is_subrepo=context.level > 0,
            include_start=context.include_start,
            max_count=context.max_count,
        )
        if not commits:
            return []
        session = self._deps_evaluator.open_session(repo, commits, context)
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
        repo = session.repo
        roll_summary = session.inspect_commit(commit, context.repo_filter)
        entries: list[GitCommitEntry] = []
        can_expand = self.max_level is None or context.level < self.max_level
        if roll_summary.targets and can_expand:
            entries.extend(
                self._expand_roll_targets(
                    repo, commit, context, roll_summary.targets
                )
            )
        entry_shas = tuple(
            {**context.base_shas, repo.display_path: commit.sha}.items()
        )
        entries.append(
            GitCommitEntry(
                path=repo.display_path,
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
        repo: GitRepo,
        commit: scm.CommitLogEntry,
        context: _WalkContext,
        targets: Sequence[_RollTarget],
    ) -> list[GitCommitEntry]:
        parent_sha = repo.try_resolve_sha(f"{commit.sha}~1") or commit.sha
        child_shas = {**context.base_shas, repo.display_path: parent_sha}
        entries: list[GitCommitEntry] = []
        for target in targets:
            subrepo, subrepo_entries = self._expand_single_target(
                repo, target, child_shas, context.level + 1
            )
            entries.extend(subrepo_entries)
            if subrepo is not None:
                child_shas[subrepo.display_path] = target.roll.new_sha
        return entries

    def _expand_single_target(
        self,
        repo: GitRepo,
        target: _RollTarget,
        child_shas: Mapping[str, str],
        child_level: int,
    ) -> tuple[GitRepo | None, list[GitCommitEntry]]:
        roll = target.roll
        subrepo = repo.subrepo(roll.rel_path)
        if subrepo is None:
            child_display = repo.child_display_path(roll.rel_path)
            _warn_skipped_roll(child_display, roll.old_sha, roll.new_sha)
            return None, []
        child_context = _WalkContext(
            base_shas=dict(child_shas),
            parse_rolls=target.parse_rolls,
            deps_file=target.deps_file,
            parent_vars=target.parent_vars,
            repo_filter=target.repo_filter,
            level=child_level,
        )
        return subrepo, self._collect_repo(
            subrepo, roll.to_commit_range(), child_context
        )


@dataclass(frozen=True)
class _ParsedDeps:
    git_shas: Mapping[str, str]
    non_git_deps: Mapping[str, _NonGitDep]
    repo_urls: Mapping[str, str]
    disabled_paths: frozenset[str]
    recursedeps: Mapping[str, str]
    effective_vars: _VariableMap
    is_sync_deps: bool = False


_EMPTY_PARSED_DEPS: Final = _ParsedDeps({}, {}, {}, frozenset(), {}, {})


class GclientDepsEvaluator:
    """Evaluates and caches `.gclient`, `DEPS`, and `.gitmodules` metadata."""

    def __init__(self, root_repo: GitRepo) -> None:
        self._deps_cache: dict[
            tuple[Path, str, str, str], _ParsedDeps | None
        ] = {}
        self._gitmodules_cache: dict[
            tuple[Path, str], tuple[_SubmoduleSection, ...]
        ] = {}
        options = types.SimpleNamespace(deps_os=None, verbose=False)
        client_root = root_repo.gclient_root or root_repo.path
        client = gclient.GClient(str(client_root), options)
        solution: gclient.Dependency | None = None
        if root_repo.gclient_root is not None:
            config_path = root_repo.gclient_root / ".gclient"
            if config_path.is_file():
                with contextlib.suppress(*_PARSE_ERRORS, OSError):
                    client.SetConfig(gclient_utils.FileRead(str(config_path)))
            solution = self._select_gclient_solution(
                client.dependencies, root_repo.display_path
            )
        self._template_dep: gclient.Dependency = solution or client
        self._custom_vars: _VariableMap = (
            solution.custom_vars if solution else {}
        )
        self._custom_deps: Mapping[str, str | bool | None] = (
            solution.custom_deps if solution else {}
        )
        self._solution_name = (
            (solution.name or "").strip("/") if solution else ""
        )
        raw_deps_file = (solution.deps_file or "DEPS") if solution else "DEPS"
        self._deps_file = posixpath.normpath(raw_deps_file.replace("\\", "/"))

    def resolve_root_context(
        self, root_repo: GitRepo, repo_filter: RepoFilter
    ) -> _WalkContext:
        gclient_root = root_repo.gclient_root
        if gclient_root is None:
            return _WalkContext({}, True, gclient_eval.DEPS, {}, repo_filter)
        if not self._solution_name or not root_repo.display_path.startswith(
            f"{self._solution_name}/"
        ):
            return _WalkContext({}, True, self._deps_file, {}, repo_filter)
        return self._walk_ancestor_recursedeps(
            root_repo, gclient_root, repo_filter
        )

    def open_session(
        self,
        repo: GitRepo,
        commits: Sequence[scm.CommitLogEntry],
        context: _WalkContext,
    ) -> _RepoDepsSession:
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
        ):
            if not repo.try_resolve_sha(f"{tip_sha}:{deps_file}"):
                deps_file = "DEPS"
        return _RepoDepsSession(
            repo=repo,
            _evaluator=self,
            _parse_rolls=context.parse_rolls,
            _deps_file=deps_file,
            _parent_vars=context.parent_vars,
            _tip_sha=tip_sha,
            _deps_blob=initial_blobs.get(deps_file, ""),
            _gitmodules_ref=initial_blobs.get(
                ".gitmodules", f"{tip_sha}:.gitmodules"
            ),
        )

    def parse_deps_state(
        self,
        repo: GitRepo,
        deps_blob_sha: str,
        gitmodules_ref: str,
        deps_file: str,
        parent_vars: _VariableMap,
    ) -> _ParsedDeps | None:
        cache_key = (
            repo.path,
            deps_blob_sha,
            gitmodules_ref,
            repr(parent_vars),
        )
        if cache_key in self._deps_cache:
            return self._deps_cache[cache_key]
        parsed_scope: _DepsScope = {}
        effective_vars = self._merge_variable_maps(
            parent_vars,
            self._template_dep.get_builtin_vars(),
            self._custom_vars,
        )
        if deps_blob_sha != scm.ZERO_SHA:
            deps_text = repo.read_blob(deps_blob_sha)
            if deps_text is None:
                return None
            evaluated = self._evaluate_deps_blob(
                deps_text, deps_file, parent_vars, effective_vars
            )
            if evaluated is None:
                return self._deps_cache.setdefault(cache_key, None)
            parsed_scope, effective_vars = evaluated
        git_deps_mode = parsed_scope.get("git_dependencies", gclient_eval.DEPS)
        submodules = self._get_submodule_sections(
            repo, deps_blob_sha, gitmodules_ref, git_deps_mode
        )
        parsed_deps = (
            _EMPTY_PARSED_DEPS
            if deps_blob_sha == scm.ZERO_SHA and not submodules
            else self._extract_parsed_deps(
                repo, parsed_scope, submodules, effective_vars
            )
        )
        return self._deps_cache.setdefault(cache_key, parsed_deps)

    def _select_gclient_solution(
        self, solutions: Sequence[gclient.Dependency], display_path: str
    ) -> gclient.Dependency | None:
        if not solutions:
            return None
        display_prefix = f"{display_path}/"
        for solution in solutions:
            solution_name = (solution.name or "").strip("/")
            if solution_name and display_prefix.startswith(f"{solution_name}/"):
                return solution
        return solutions[0]

    def _walk_ancestor_recursedeps(
        self,
        root_repo: GitRepo,
        gclient_root: Path,
        repo_filter: RepoFilter,
    ) -> _WalkContext:
        current_repo: GitRepo | None = GitRepo(
            _abspath(gclient_root / self._solution_name),
            self._solution_name,
            gclient_root,
        )
        base_shas: dict[str, str] = {}
        remaining_path = root_repo.display_path[len(self._solution_name) + 1 :]
        deps_file = self._deps_file
        parent_vars: _VariableMap = {}
        while remaining_path and current_repo is not None:
            head_sha = current_repo.try_resolve_sha("HEAD")
            if not head_sha:
                break
            base_shas[current_repo.display_path] = head_sha
            parsed_deps = self._parse_head_deps(
                current_repo, head_sha, deps_file, parent_vars
            )
            matched_dep = self._find_matching_recursedep(
                parsed_deps, remaining_path
            )
            if not matched_dep or parsed_deps is None:
                return _WalkContext(
                    base_shas, False, deps_file, {}, repo_filter
                )
            parent_vars = parsed_deps.effective_vars
            deps_file = parsed_deps.recursedeps[matched_dep]
            remaining_path = remaining_path[len(matched_dep) + 1 :]
            if remaining_path:
                current_repo = current_repo.subrepo(matched_dep)
        parse_rolls = bool(base_shas and not remaining_path)
        return _WalkContext(
            base_shas, parse_rolls, deps_file, parent_vars, repo_filter
        )

    def _parse_head_deps(
        self,
        repo: GitRepo,
        head_sha: str,
        deps_file: str,
        parent_vars: _VariableMap,
    ) -> _ParsedDeps | None:
        deps_blob = (
            repo.try_resolve_sha(f"{head_sha}:{deps_file}")
            or repo.try_resolve_sha(f"{head_sha}:DEPS")
            or scm.ZERO_SHA
        )
        return self.parse_deps_state(
            repo, deps_blob, f"{head_sha}:.gitmodules", deps_file, parent_vars
        )

    def _find_matching_recursedep(
        self, parsed_deps: _ParsedDeps | None, remaining_path: str
    ) -> str:
        if parsed_deps is None or parsed_deps is _EMPTY_PARSED_DEPS:
            return ""
        for dep_path in parsed_deps.recursedeps:
            if f"{remaining_path}/".startswith(f"{dep_path}/"):
                return dep_path
        return ""

    def _evaluate_deps_blob(
        self,
        deps_text: str,
        deps_file: str,
        parent_vars: _VariableMap,
        initial_vars: _VariableMap,
    ) -> tuple[_DepsScope, _EvalVarsDict] | None:
        builtin_vars = self._template_dep.get_builtin_vars()
        try:
            parsed_scope: _DepsScope = gclient_eval.Parse(
                deps_text,
                deps_file,
                vars_override=initial_vars,
                builtin_vars=builtin_vars,
            )
        except _PARSE_ERRORS:
            return None
        target_os = parsed_scope.get("target_os")
        if target_os:
            saved_target_os = self._template_dep.local_target_os
            self._template_dep.local_target_os = list(target_os)
            try:
                builtin_vars = self._template_dep.get_builtin_vars()
            finally:
                self._template_dep.local_target_os = saved_target_os
        effective_vars = self._merge_variable_maps(
            parsed_scope.get("vars", {}),
            parent_vars,
            builtin_vars,
            self._custom_vars,
        )
        return parsed_scope, effective_vars

    def _merge_variable_maps(
        self, *variable_maps: _VariableMap
    ) -> _EvalVarsDict:
        merged_vars: _EvalVarsDict = {}
        for variable_map in variable_maps:
            copied_map: _EvalVarsDict = {
                var_name: (
                    gclient_eval.ConstantString(var_value.value)
                    if isinstance(var_value, gclient_eval.ConstantString)
                    else var_value
                )
                for var_name, var_value in variable_map.items()
            }
            gclient.merge_vars(merged_vars, copied_map)
        return merged_vars

    def _get_submodule_sections(
        self,
        repo: GitRepo,
        deps_blob_sha: str,
        gitmodules_ref: str,
        git_deps_mode: str,
    ) -> tuple[_SubmoduleSection, ...]:
        if gitmodules_ref in ("", scm.ZERO_SHA):
            return ()
        if deps_blob_sha != scm.ZERO_SHA and git_deps_mode != "SUBMODULES":
            return ()
        cache_key = (repo.path, gitmodules_ref)
        cached_sections = self._gitmodules_cache.get(cache_key)
        if cached_sections is None:
            cached_sections = repo.read_gitmodules_sections(gitmodules_ref)
            self._gitmodules_cache[cache_key] = cached_sections
        return cached_sections

    def _extract_parsed_deps(
        self,
        repo: GitRepo,
        scope: _DepsScope,
        submodules: Sequence[_SubmoduleSection],
        vars_map: _VariableMap,
    ) -> _ParsedDeps:
        git_shas: dict[str, str] = {}
        non_git_deps: dict[str, _NonGitDep] = {}
        repo_urls: dict[str, str] = {}
        disabled_paths: set[str] = set()
        cond_cache: dict[str, bool] = {}
        dep_items = self._collect_dependency_items(repo, scope, submodules)
        for rel_path, raw_key, dep_info in dep_items:
            if self._apply_custom_dep_override(
                repo, raw_key, rel_path, repo_urls, disabled_paths
            ):
                continue
            dep_type = dep_info.get("dep_type", "git")
            condition = dep_info.get("condition")
            if not self._evaluate_condition(condition, vars_map, cond_cache):
                if dep_type == "git":
                    disabled_paths.add(rel_path)
                continue
            dep_url = dep_info.get("url")
            if dep_type == "git" and dep_url is None:
                disabled_paths.add(rel_path)
            elif dep_type == "git":
                self._parse_git_dep_url(rel_path, dep_url, git_shas, repo_urls)
            elif dep_type in ("cipd", "gcs"):
                entry = self._parse_non_git_dep(dep_info, vars_map, cond_cache)
                if entry is not None:
                    non_git_deps[rel_path] = entry
        for disabled_path in disabled_paths:
            git_shas.pop(disabled_path, None)
        return _ParsedDeps(
            git_shas,
            non_git_deps,
            repo_urls,
            frozenset(disabled_paths),
            self._extract_recursedeps(repo, scope, submodules),
            vars_map,
            scope.get("git_dependencies", "DEPS") != "DEPS",
        )

    def _extract_recursedeps(
        self,
        repo: GitRepo,
        scope: _DepsScope,
        submodules: Sequence[_SubmoduleSection],
    ) -> dict[str, str]:
        use_relative_paths = bool(scope.get("use_relative_paths"))
        recursedeps: dict[str, str] = {}
        for entry in scope.get("recursedeps", ()):
            if isinstance(entry, str):
                raw_dep = entry
                raw_deps_file = "DEPS"
            else:
                raw_dep, raw_deps_file = entry
            norm_dep = self._normalize_dep_path(
                repo, raw_dep, use_relative_paths
            )
            if norm_dep:
                norm_file = posixpath.normpath(raw_deps_file.replace("\\", "/"))
                recursedeps.setdefault(norm_dep, norm_file)
        for section in submodules:
            submodule_path = section.get("path", "")
            is_recurse = (
                section.get("gclient-recursedeps", "").lower() == "true"
            )
            if submodule_path and is_recurse:
                norm_dep = self._normalize_dep_path(repo, submodule_path, True)
                if norm_dep:
                    recursedeps.setdefault(norm_dep, "DEPS")
        return recursedeps

    def _collect_dependency_items(
        self,
        repo: GitRepo,
        scope: _DepsScope,
        submodules: Sequence[_SubmoduleSection],
    ) -> list[tuple[str, str, _DepInfo]]:
        use_relative = bool(scope.get("use_relative_paths"))
        items: list[tuple[str, str, _DepInfo]] = []
        for dep_key, dep_info in scope.get("deps", {}).items():
            rel_path = self._normalize_dep_path(repo, dep_key, use_relative)
            if rel_path:
                items.append((rel_path, dep_key, dep_info))
        for section in submodules:
            submodule_path = section.get("path", "")
            rel_path = (
                self._normalize_dep_path(repo, submodule_path, True)
                if submodule_path
                else ""
            )
            if rel_path:
                submodule_dep: _DepInfo = {
                    "dep_type": "git",
                    "url": section.get("url", ""),
                    "condition": section.get("gclient-condition", ""),
                }
                items.append((rel_path, submodule_path, submodule_dep))
        return items

    def _apply_custom_dep_override(
        self,
        repo: GitRepo,
        raw_key: str,
        rel_path: str,
        repo_urls: dict[str, str],
        disabled_paths: set[str],
    ) -> bool:
        candidates = (raw_key, repo.child_display_path(rel_path), rel_path)
        matched_key = next(
            (dep_key for dep_key in candidates if dep_key in self._custom_deps),
            "",
        )
        if not matched_key:
            return False
        custom_value = self._custom_deps[matched_key]
        if not isinstance(custom_value, str) or custom_value.endswith(
            "@unmanaged"
        ):
            disabled_paths.add(rel_path)
            return True
        custom_url, custom_revision = gclient_utils.SplitUrlRevision(
            custom_value
        )
        if custom_url:
            repo_urls[rel_path] = custom_url
        if custom_revision:
            disabled_paths.add(rel_path)
            return True
        return False

    def _parse_git_dep_url(
        self,
        rel_path: str,
        dep_url: str | None,
        git_shas: dict[str, str],
        repo_urls: dict[str, str],
    ) -> None:
        if not isinstance(dep_url, str):
            return
        with contextlib.suppress(gclient_utils.Error):
            origin_url, revision = gclient_utils.SplitUrlRevision(dep_url)
            if origin_url:
                repo_urls.setdefault(rel_path, origin_url)
            if revision and gclient_utils.IsFullGitSha(revision):
                git_shas[rel_path] = revision.lower()

    def _parse_non_git_dep(
        self,
        dep_info: _DepInfo,
        vars_map: _VariableMap,
        cond_cache: dict[str, bool],
    ) -> _NonGitDep | None:
        if dep_info.get("dep_type") == "cipd":
            return ("cipd", dep_info.get("packages"))
        active_objects = [
            gcs_object
            for gcs_object in dep_info.get("objects", ())
            if self._evaluate_condition(
                gcs_object.get("condition"), vars_map, cond_cache
            )
        ]
        if active_objects:
            return ("gcs", dep_info.get("bucket"), active_objects)
        return None

    def _evaluate_condition(
        self,
        condition: str | None,
        effective_vars: _VariableMap,
        condition_cache: dict[str, bool],
    ) -> bool:
        if not isinstance(condition, str) or not condition:
            return True
        if condition not in condition_cache:
            evaluated = False
            with contextlib.suppress(*_PARSE_ERRORS):
                evaluated = bool(
                    gclient_eval.EvaluateCondition(condition, effective_vars)
                )
            condition_cache[condition] = evaluated
        return condition_cache[condition]

    def _normalize_dep_path(
        self, repo: GitRepo, raw_key: str, use_relative_paths: bool
    ) -> str:
        stripped_key = raw_key.replace("\\", "/").strip("/")
        dep_path = posixpath.normpath(stripped_key) if stripped_key else ""
        if dep_path and not use_relative_paths:
            prefixes = (repo.display_path, repo.path.name)
            matched_prefix = next(
                (
                    prefix
                    for prefix in prefixes
                    if prefix and dep_path.startswith(f"{prefix}/")
                ),
                "",
            )
            has_implicit_src = (
                dep_path.startswith("src/")
                and not (repo.path / dep_path).exists()
            )
            if matched_prefix or has_implicit_src:
                prefix_to_strip = matched_prefix or "src"
                dep_path = posixpath.normpath(
                    dep_path.removeprefix(f"{prefix_to_strip}/")
                )
        return "" if dep_path == "." else dep_path


@dataclass
class _RepoDepsSession:
    """Tracks temporal `DEPS` and `.gitmodules` state for one repository."""

    repo: GitRepo
    _evaluator: GclientDepsEvaluator
    _parse_rolls: bool
    _deps_file: str
    _parent_vars: _VariableMap
    _tip_sha: str
    _deps_blob: str
    _gitmodules_ref: str
    _current_deps: _ParsedDeps | None = None

    def inspect_commit(
        self, commit: scm.CommitLogEntry, repo_filter: RepoFilter
    ) -> _CommitRollSummary:
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
            old_deps, new_deps, deps_diff, gitlink_diffs, repo_filter
        )

    def _advance_commit_deps(
        self,
        deps_diff: scm.DiffTreeEntry | None,
        gitmodules_diff: scm.DiffTreeEntry | None,
        gitlink_diffs: Sequence[scm.DiffTreeEntry],
    ) -> tuple[_ParsedDeps | None, _ParsedDeps | None]:
        if not self._deps_blob:
            tip_blob = self.repo.try_resolve_sha(
                f"{self._tip_sha}:{self._deps_file}"
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
            self.repo,
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
            self.repo,
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
        old_text = self.repo.read_blob(old_deps_blob)
        new_text = self.repo.read_blob(new_deps_blob)
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
        repo_filter: RepoFilter,
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
        targets = self._select_roll_targets(
            rolls_by_path, new_deps, repo_urls, repo_filter
        )
        has_other_roll = False
        for rel_path in other_changed_paths - rolls_by_path.keys():
            matched, _ = repo_filter.match_subrepo(
                self.repo, rel_path, repo_urls
            )
            if matched:
                has_other_roll = True
                break
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
        old_disabled = old_deps.disabled_paths if old_deps else frozenset()
        new_disabled = new_deps.disabled_paths if new_deps else frozenset()
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
        repo_filter: RepoFilter,
    ) -> tuple[_RollTarget, ...]:
        has_deps = new_deps is not None and new_deps is not _EMPTY_PARSED_DEPS
        recursedeps = new_deps.recursedeps if new_deps else {}
        child_vars = (
            new_deps.effective_vars
            if new_deps and has_deps
            else self._parent_vars
        )
        targets: list[_RollTarget] = []
        for rel_path in sorted(rolls_by_path):
            matched, child_filter = repo_filter.match_subrepo(
                self.repo, rel_path, repo_urls
            )
            if matched:
                parse_rolls = (not has_deps) or (rel_path in recursedeps)
                deps_file = recursedeps.get(rel_path, "DEPS")
                targets.append(
                    _RollTarget(
                        rolls_by_path[rel_path],
                        parse_rolls,
                        deps_file,
                        child_vars,
                        child_filter,
                    )
                )
        return tuple(targets)


@dataclass(frozen=True)
class RepoFilter:
    """Matches subrepository paths and remote URLs against `--filter` specs."""

    patterns: tuple[str, ...] = ()

    @classmethod
    def from_patterns(cls, raw_patterns: Sequence[str]) -> RepoFilter:
        normalized_patterns: list[str] = []
        for raw_pattern in raw_patterns:
            with contextlib.suppress(ValueError):
                normalized_patterns.append(cls.normalize_pattern(raw_pattern))
        return cls(tuple(normalized_patterns))

    @classmethod
    def normalize_pattern(cls, raw_pattern: str) -> str:
        stripped_pattern = raw_pattern.strip()
        if _REPO_URL_RE.match(stripped_pattern):
            return stripped_pattern.rstrip("/")
        trimmed_path = stripped_pattern.replace("\\", "/").strip("/")
        normalized_path = (
            posixpath.normpath(trimmed_path) if trimmed_path else ""
        )
        if normalized_path in ("", ".", ".."):
            raise ValueError(raw_pattern)
        return normalized_path

    def match_subrepo(
        self, repo: GitRepo, rel_path: str, repo_urls: Mapping[str, str]
    ) -> tuple[bool, RepoFilter]:
        if not self.patterns:
            return True, RepoFilter()
        path_candidates = (rel_path, repo.child_display_path(rel_path))
        origin_url = self._resolve_subrepo_url(repo, rel_path, repo_urls)
        if self._is_direct_match(path_candidates, origin_url):
            return True, RepoFilter()
        is_ancestor_prefix = any(
            pattern.startswith(f"{candidate}/")
            for candidate in path_candidates
            for pattern in self.patterns
        )
        return is_ancestor_prefix, self

    def _resolve_subrepo_url(
        self, repo: GitRepo, rel_path: str, repo_urls: Mapping[str, str]
    ) -> str:
        raw_url = repo_urls.get(rel_path, "")
        has_url_pattern = any(
            _REPO_URL_RE.match(pattern) for pattern in self.patterns
        )
        if not raw_url and has_url_pattern:
            subrepo = repo.subrepo(rel_path)
            if subrepo is not None:
                raw_url = subrepo.origin_url()
        if not raw_url:
            return ""
        origin_url, _ = gclient_utils.SplitUrlRevision(raw_url)
        return origin_url or ""

    def _is_direct_match(
        self, path_candidates: tuple[str, str], origin_url: str
    ) -> bool:
        has_segment_match = any(
            f"/{pattern}/" in f"/{candidate}/"
            for candidate in path_candidates
            for pattern in self.patterns
        )
        if has_segment_match:
            return True
        fuzzy_candidates = (
            (*path_candidates, origin_url) if origin_url else path_candidates
        )
        return any(
            bool(gclient_utils.FuzzyMatchRepo(candidate, self.patterns))
            for candidate in fuzzy_candidates
        )


class GitRepo(NamedTuple):
    """Represents a resolved Git repository checkout or worktree."""

    path: Path
    display_path: str
    gclient_root: Path | None = None
    main_worktree: Path | None = None
    first_parent: bool = False

    @classmethod
    def resolve(
        cls,
        repo: Path = Path("."),
        cwd: Path | None = None,
        first_parent: bool = False,
    ) -> GitRepo:
        base_dir = cwd or Path(".")
        candidate_dir = _abspath(base_dir / repo.expanduser())
        repo_dir = cls._find_checkout_root(candidate_dir)
        if repo_dir is None:
            raise CommitDepsError(f"Could not resolve repository: {repo!r}")
        main_worktree = cls._find_main_worktree(repo_dir)
        display_path, gclient_root = cls._resolve_gclient_location(
            repo_dir, main_worktree
        )
        return cls(
            repo_dir, display_path, gclient_root, main_worktree, first_parent
        )

    def subrepo(self, rel_path: str) -> GitRepo | None:
        norm_rel_path = _normalize_posix_path(rel_path)
        local_subrepo = _abspath(self.path / norm_rel_path)
        worktree_subrepo = (
            _abspath(self.main_worktree / norm_rel_path)
            if self.main_worktree
            else None
        )
        chosen_dir = (
            local_subrepo
            if (local_subrepo / ".git").exists()
            else worktree_subrepo
        )
        if chosen_dir is None or not (chosen_dir / ".git").exists():
            return None
        sub_worktree = (
            worktree_subrepo
            if chosen_dir == local_subrepo
            and worktree_subrepo is not None
            and worktree_subrepo.is_dir()
            else None
        )
        return GitRepo(
            chosen_dir,
            self.child_display_path(norm_rel_path),
            self.gclient_root,
            sub_worktree,
            self.first_parent,
        )

    def child_display_path(self, rel_path: str) -> str:
        norm_rel_path = _normalize_posix_path(rel_path)
        if self.display_path in ("", "."):
            joined_path = norm_rel_path
        else:
            joined_path = posixpath.normpath(
                f"{self.display_path}/{norm_rel_path}"
            )
        return self.display_path if joined_path == "." else joined_path

    def resolve_range(
        self,
        commit_range: CommitRange,
        *,
        is_subrepo: bool = False,
        fetch: bool = False,
    ) -> CommitRange | None:
        if is_subrepo:
            return self._resolve_subrepo_range(commit_range, fetch=fetch)
        start_sha = self.resolve_commit(commit_range.start)
        end_sha = self.resolve_commit(commit_range.end)
        if commit_range.use_merge_base:
            start_sha = self._resolve_merge_base(
                commit_range, start_sha, end_sha
            )
        return CommitRange(start_sha, end_sha, is_range=commit_range.is_range)

    def resolve_commit(self, commit_ref: str) -> str:
        error_msg = f"Cannot resolve {commit_ref!r} in {self.path}"
        position_sha = self._resolve_commit_position(commit_ref, error_msg)
        if position_sha:
            return position_sha
        lowered_ref = commit_ref.lower()
        if (
            lowered_ref.startswith("cr-commit-position:")
            or "@{#" in lowered_ref
        ):
            raise CommitDepsError(error_msg)
        resolved_sha = self.try_resolve_sha(commit_ref)
        if resolved_sha:
            return resolved_sha
        if _CHROME_VERSION_RE.fullmatch(commit_ref):
            fetch_hint = f"fetch tags with 'git fetch origin tag {commit_ref}'"
            error_msg = (
                f"{error_msg} ({fetch_hint} or 'gclient sync --with_tags')"
            )
        raise CommitDepsError(error_msg)

    def list_commits(
        self,
        sha_range: CommitRange,
        requested_range: CommitRange,
        *,
        is_subrepo: bool = False,
        include_start: bool = False,
        max_count: int | None = None,
    ) -> list[scm.CommitLogEntry]:
        if sha_range.is_single_commit:
            return self._commit_log_diffs(sha_range.end, single_commit=True)
        if sha_range.start == sha_range.end:
            return self._list_empty_range_commits(
                sha_range,
                requested_range,
                include_start=include_start,
                max_count=max_count,
            )
        commits = self._commit_log_diffs(
            f"{sha_range.start}..{sha_range.end}", max_count=max_count
        )
        if include_start:
            return self._prepend_start_commit(
                sha_range.start, commits, max_count=max_count
            )
        if not commits and is_subrepo:
            return self._list_reverted_roll_commits(sha_range)
        return commits

    def try_resolve_sha(self, rev_spec: str) -> str | None:
        with contextlib.suppress(subprocess2.CalledProcessError, OSError):
            resolved = scm.GIT.ResolveCommit(str(self.path), rev_spec)
            return resolved.strip().lower() if resolved else None
        return None

    def read_blob(self, blob_sha: str) -> str | None:
        return scm.GIT.GetBlob(str(self.path), blob_sha)

    def read_gitmodules_sections(
        self, gitmodules_ref: str
    ) -> tuple[_SubmoduleSection, ...]:
        sections_by_name: dict[str, dict[str, str]] = {}
        raw_config = self.capture("config", "--blob", gitmodules_ref, "-l")
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

    def origin_url(self) -> str:
        with contextlib.suppress(subprocess2.CalledProcessError, OSError):
            return scm.GIT.GetConfig(str(self.path), "remote.origin.url") or ""
        return ""

    def attached_branch_name(self) -> str:
        with contextlib.suppress(subprocess2.CalledProcessError, OSError):
            branch_ref = scm.GIT.GetBranchRef(str(self.path))
            if branch_ref:
                return scm.GIT.ShortBranchName(branch_ref)
        return ""

    def capture(self, *git_args: str) -> str:
        with contextlib.suppress(subprocess2.CalledProcessError, OSError):
            output = scm.GIT.Capture(list(git_args), cwd=str(self.path))
            return output.strip() if isinstance(output, str) else ""
        return ""

    @classmethod
    def _find_checkout_root(cls, candidate_dir: Path) -> Path | None:
        if (candidate_dir / ".git").exists():
            return candidate_dir
        if not candidate_dir.is_dir():
            return None
        with contextlib.suppress(subprocess2.CalledProcessError, OSError):
            checkout_root = scm.GIT.GetCheckoutRoot(str(candidate_dir))
            if checkout_root:
                return _abspath(Path(checkout_root))
        return None

    @classmethod
    def _find_main_worktree(cls, repo_dir: Path) -> Path | None:
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

    @classmethod
    def _resolve_gclient_location(
        cls, repo_dir: Path, main_worktree: Path | None
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

    def _resolve_merge_base(
        self, commit_range: CommitRange, start_sha: str, end_sha: str
    ) -> str:
        merge_base_sha = self.capture("merge-base", start_sha, end_sha).lower()
        if not merge_base_sha:
            raise CommitDepsError(
                f"No merge-base for {commit_range.start!r} and "
                f"{commit_range.end!r}"
            )
        return merge_base_sha

    def _resolve_subrepo_range(
        self, commit_range: CommitRange, *, fetch: bool
    ) -> CommitRange | None:
        start_ref = commit_range.start
        end_ref = commit_range.end
        start_sha = self.try_resolve_sha(start_ref)
        end_sha = self.try_resolve_sha(end_ref)
        if (not start_sha or not end_sha) and fetch:
            missing_refs = list(
                dict.fromkeys(
                    commit_ref
                    for commit_ref, resolved in (
                        (start_ref, start_sha),
                        (end_ref, end_sha),
                    )
                    if not resolved
                )
            )
            self.capture(
                "fetch", "--no-tags", "--quiet", "origin", *missing_refs
            )
            start_sha = start_sha or self.try_resolve_sha(start_ref)
            end_sha = end_sha or self.try_resolve_sha(end_ref)
        if start_sha and end_sha:
            return CommitRange(start_sha, end_sha, is_range=True)
        _warn_skipped_roll(
            self.display_path, start_ref, end_ref, unfetched=True
        )
        return None

    def _resolve_commit_position(
        self, commit_ref: str, error_msg: str
    ) -> str | None:
        position_match = _COMMIT_POSITION_RE.match(commit_ref)
        bare_match = (
            None if position_match else _BARE_POSITION_RE.match(commit_ref)
        )
        if position_match:
            raw_branch = position_match.group(1)
            position_number = position_match.group(2)
            is_strict = True
        elif bare_match:
            raw_branch = "*"
            position_number = bare_match.group(1)
            is_strict = False
        else:
            return None
        grep_arg, candidate_refs = self._build_position_search_plan(
            raw_branch, position_number
        )
        seen_tips: set[str] = set()
        for candidate_ref in candidate_refs:
            tip_sha = self.try_resolve_sha(candidate_ref)
            if not tip_sha or tip_sha in seen_tips:
                continue
            seen_tips.add(tip_sha)
            matched_sha = self.capture(
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

    def _build_position_search_plan(
        self, raw_branch: str, position_number: str
    ) -> tuple[str, tuple[str, ...]]:
        branch_name = _REF_PREFIX_RE.sub("", raw_branch)
        if raw_branch == "*" or branch_name in ("", "HEAD", "main", "master"):
            ref_prefix = (
                ".*" if raw_branch == "*" else "refs/heads/(master|main)"
            )
            pattern = rf"{ref_prefix}@\{{#{position_number}\}}"
            return (
                f"--grep=^Cr-Commit-Position: {pattern}",
                _MAIN_POSITION_REFS,
            )
        ref_subdir = "" if branch_name.startswith("branch-heads/") else "heads/"
        canonical_ref = (
            raw_branch
            if raw_branch.startswith("refs/")
            else f"refs/{ref_subdir}{branch_name}"
        )
        pattern = rf"{re.escape(canonical_ref)}@\{{#{position_number}\}}"
        candidate_refs = (
            f"refs/remotes/origin/{branch_name}",
            canonical_ref,
            raw_branch,
            "HEAD",
        )
        return f"--grep=^Cr-Commit-Position: {pattern}", candidate_refs

    def _list_empty_range_commits(
        self,
        sha_range: CommitRange,
        requested_range: CommitRange,
        *,
        include_start: bool,
        max_count: int | None,
    ) -> list[scm.CommitLogEntry]:
        if (
            max_count
            and requested_range.start == requested_range.end
            and not requested_range.use_merge_base
        ):
            return self._commit_log_diffs(sha_range.end, max_count=max_count)
        if not include_start:
            return []
        return self._commit_log_diffs(sha_range.end, single_commit=True)

    def _prepend_start_commit(
        self,
        start_sha: str,
        commits: Sequence[scm.CommitLogEntry],
        *,
        max_count: int | None,
    ) -> list[scm.CommitLogEntry]:
        start_commits = self._commit_log_diffs(start_sha, single_commit=True)
        seen_shas = {entry.sha for entry in start_commits}
        merged_commits = [
            *start_commits,
            *(commit for commit in commits if commit.sha not in seen_shas),
        ]
        if max_count is not None:
            return merged_commits[-max_count:]
        return merged_commits

    def _list_reverted_roll_commits(
        self, sha_range: CommitRange
    ) -> list[scm.CommitLogEntry]:
        forward_commits = self._commit_log_diffs(
            f"{sha_range.end}..{sha_range.start}"
        )
        if not forward_commits:
            return []
        endpoint_commits = self._commit_log_diffs(
            sha_range.end, single_commit=True
        )
        target_commits = [*reversed(forward_commits[:-1]), *endpoint_commits]
        reverted_commits: list[scm.CommitLogEntry] = []
        for source_commit, target_commit in zip(
            reversed(forward_commits), target_commits
        ):
            inverted_diffs = self._invert_commit_diffs(source_commit.diffs)
            reverted_commits.append(
                target_commit._replace(diffs=inverted_diffs)
            )
        return reverted_commits

    def _invert_commit_diffs(
        self, diffs: Sequence[scm.DiffTreeEntry]
    ) -> tuple[scm.DiffTreeEntry, ...]:
        return tuple(
            scm.DiffTreeEntry(
                old_mode=diff.new_mode,
                new_mode=diff.old_mode,
                old_sha=diff.new_sha,
                new_sha=diff.old_sha,
                file_path=diff.file_path,
            )
            for diff in diffs
        )

    def _commit_log_diffs(
        self,
        rev_spec: str,
        *,
        max_count: int | None = None,
        single_commit: bool = False,
    ) -> list[scm.CommitLogEntry]:
        return scm.GIT.GetCommitLogDiffs(
            str(self.path),
            rev_spec,
            first_parent=self.first_parent,
            max_count=max_count,
            single_commit=single_commit,
        )


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
    branch_name = collector.attached_branch_name()
    if branch_name:
        print(
            f"Warning: {collector.display_path} is on branch {branch_name!r}; "
            "run 'git checkout --detach' before 'gclient sync --revision'.",
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


def _normalize_posix_path(raw_path: str) -> str:
    return posixpath.normpath(raw_path.replace("\\", "/")).lstrip("/")


def _abspath(target_path: Path) -> Path:
    return Path(os.path.abspath(target_path))


if __name__ == "__main__":
    sys.exit(main())
