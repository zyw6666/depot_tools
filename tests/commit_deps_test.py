#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Unit tests for commit_deps.py."""

from __future__ import annotations

import contextlib
import csv
import dataclasses
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commit_deps
from commit_deps import (
    CommitDepsCollector,
    CommitDepsError,
    CommitRange,
    GitCommitEntry,
    InputParser,
    OutputFormat,
    RepoFilter,
    SubrepoRoll,
)
import gclient_utils
import git_common
import scm
import setup_color
from testing_support import trial_dir


@dataclasses.dataclass(frozen=True)
class _NestedRepos:
    src_dir: Path
    src_c1: str
    src_c2: str
    src_c3_roll: str
    src_c4: str
    v8_c2: str
    v8_c3_roll: str
    sub_c2: str
    sub_c3: str
    entries: tuple[GitCommitEntry, ...]
    roll_entries: tuple[GitCommitEntry, ...]


class CommitDepsTest(trial_dir.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace: Path = Path(self.root_dir)

    def test_recursive_deps_expansion(self) -> None:
        repos = self._setup_nested_repos()
        collector = CommitDepsCollector(repos.src_dir)
        self.assertEqual(collector.display_path, "src")
        self.assertFalse(collector.is_standalone)
        collected = collector.collect(repos.src_c1, repos.src_c4)
        self.assertEqual(collected, list(repos.entries))
        self.assertEqual(
            [entry.subject for entry in collected],
            [entry.subject for entry in repos.entries],
        )
        sub_path = "src/v8/third_party/sub"
        self.assertEqual(
            GitCommitEntry.build_pinned_shas(collected),
            [
                {"src": repos.src_c2},
                {"src": repos.src_c2, "src/v8": repos.v8_c2},
                {
                    "src": repos.src_c2,
                    "src/v8": repos.v8_c2,
                    sub_path: repos.sub_c2,
                },
                {
                    "src": repos.src_c2,
                    "src/v8": repos.v8_c2,
                    sub_path: repos.sub_c3,
                },
                {"src": repos.src_c2, "src/v8": repos.v8_c3_roll},
                {"src": repos.src_c3_roll},
                {"src": repos.src_c4},
            ],
        )
        self.assertEqual(
            collector.collect(repos.src_c2, repos.src_c4, include_start=True),
            list(repos.entries),
        )
        self.assertEqual(
            collector.collect(repos.src_c1, repos.src_c2, include_start=True),
            [
                GitCommitEntry("src", 0, repos.src_c1, roll=True),
                GitCommitEntry("src", 0, repos.src_c2, roll=False),
            ],
        )
        for max_level in (0, 1):
            with self.subTest(max_level=max_level):
                leveled_collector = CommitDepsCollector(
                    repos.src_dir, max_level=max_level
                )
                self.assertEqual(
                    leveled_collector.collect(repos.src_c1, repos.src_c4),
                    [
                        entry
                        for entry in repos.entries
                        if entry.level <= max_level
                    ],
                )

    def test_multi_subrepo_roll_pinned_shas(self) -> None:
        workspace_dir = self.workspace / "multi_ws"
        _setup_gclient(workspace_dir)
        src_dir = workspace_dir / "src"
        angle_c0, angle_c1, angle_c2 = _init_commits(
            src_dir / "third_party/angle", "a0", "a1", "a2"
        )
        v8_c0, v8_c1, v8_c2 = _init_commits(src_dir / "v8", "v0", "v1", "v2")

        _init_repo(src_dir)
        src_c0 = _commit_file(
            src_dir,
            "DEPS",
            _deps_content({"third_party/angle": angle_c0, "v8": v8_c0}),
            "s0",
        )
        src_c1 = _commit_file(
            src_dir,
            "DEPS",
            _deps_content({"third_party/angle": angle_c2, "v8": v8_c2}),
            "s1 multi roll",
        )

        entries = CommitDepsCollector(src_dir).collect(src_c0, src_c1)
        self.assertEqual(
            entries,
            [
                GitCommitEntry(
                    "src/third_party/angle", 1, angle_c1, roll=False
                ),
                GitCommitEntry(
                    "src/third_party/angle", 1, angle_c2, roll=False
                ),
                GitCommitEntry("src/v8", 1, v8_c1, roll=False),
                GitCommitEntry("src/v8", 1, v8_c2, roll=False),
                GitCommitEntry("src", 0, src_c1, roll=True),
            ],
        )
        self.assertEqual(
            GitCommitEntry.build_pinned_shas(entries),
            [
                {"src": src_c0, "src/third_party/angle": angle_c1},
                {"src": src_c0, "src/third_party/angle": angle_c2},
                {
                    "src": src_c0,
                    "src/third_party/angle": angle_c2,
                    "src/v8": v8_c1,
                },
                {
                    "src": src_c0,
                    "src/third_party/angle": angle_c2,
                    "src/v8": v8_c2,
                },
                {"src": src_c1},
            ],
        )

    def test_recursedeps_custom_deps_file_and_subrepo_target(self) -> None:
        workspace_dir = self.workspace / "recurse_ws"
        _setup_gclient(workspace_dir)
        src_dir = workspace_dir / "src"
        skia_dir = src_dir / "third_party/skia"
        custom_dir = src_dir / "third_party/custom"
        leaf_c1, leaf_c2 = _init_commits(skia_dir / "leaf", "l1", "l2")
        inner_c1, inner_c2 = _init_commits(custom_dir / "inner", "i1", "i2")

        _init_repo(skia_dir)
        skia_c1 = _commit_file(
            skia_dir, "DEPS", _deps_content({"leaf": leaf_c1}), "s1"
        )
        skia_c2 = _commit_file(
            skia_dir, "DEPS", _deps_content({"leaf": leaf_c2}), "s2"
        )

        _init_repo(custom_dir)
        custom_c1 = _commit_file(
            custom_dir,
            "DEPS.chromium",
            _deps_content({"inner": inner_c1}),
            "c1",
        )
        custom_c2 = _commit_file(
            custom_dir,
            "DEPS.chromium",
            _deps_content({"inner": inner_c2}),
            "c2",
        )

        _init_repo(src_dir)
        deps_template = (
            "deps = {{\n"
            '  "src/third_party/skia": "https://example.com/skia.git@{skia}",\n'
            '  "src/third_party/custom": '
            '"https://example.com/custom.git@{custom}",\n'
            "}}\n"
            'recursedeps = [("src/third_party/custom", "DEPS.chromium")]\n'
        )
        src_c1 = _commit_file(
            src_dir,
            "DEPS",
            deps_template.format(skia=skia_c1, custom=custom_c1),
            "init",
        )
        src_c2 = _commit_file(
            src_dir,
            "DEPS",
            deps_template.format(skia=skia_c2, custom=custom_c2),
            "roll",
        )

        skia_path = "src/third_party/skia"
        custom_path = "src/third_party/custom"
        entries = CommitDepsCollector(src_dir).collect(src_c1, src_c2)
        self.assertEqual(
            entries,
            [
                GitCommitEntry(f"{custom_path}/inner", 2, inner_c2, roll=False),
                GitCommitEntry(custom_path, 1, custom_c2, roll=True),
                GitCommitEntry(skia_path, 1, skia_c2, roll=False),
                GitCommitEntry("src", 0, src_c2, roll=True),
            ],
        )

        # --repo on non-recursedeps subrepo suppresses nested DEPS & pins src.
        skia_entries = CommitDepsCollector(skia_dir).collect(skia_c1, skia_c2)
        self.assertEqual(
            skia_entries,
            [GitCommitEntry(skia_path, 0, skia_c2, roll=False)],
        )
        self.assertEqual(
            GitCommitEntry.build_pinned_shas(skia_entries),
            [{"src": src_c2, skia_path: skia_c2}],
        )

        # --repo on recursedeps subrepo honors DEPS.chromium & pins src.
        custom_entries = CommitDepsCollector(custom_dir).collect(
            custom_c1, custom_c2
        )
        self.assertEqual(
            custom_entries,
            [
                GitCommitEntry(f"{custom_path}/inner", 1, inner_c2, roll=False),
                GitCommitEntry(custom_path, 0, custom_c2, roll=True),
            ],
        )
        self.assertEqual(
            GitCommitEntry.build_pinned_shas(custom_entries),
            [
                {
                    "src": src_c2,
                    custom_path: custom_c1,
                    f"{custom_path}/inner": inner_c2,
                },
                {"src": src_c2, custom_path: custom_c2},
            ],
        )

    def test_conditions_vars_and_gitmodules(self) -> None:
        workspace_dir = self.workspace / "cond_ws"
        _write_file(
            workspace_dir / ".gclient",
            "solutions = [{\n"
            '  "name": "src",\n'
            '  "url": "https://example.com/src.git",\n'
            '  "custom_vars": {"enable_extra": True},\n'
            "}]\n"
            'target_os = ["android"]\n',
        )
        _write_file(
            workspace_dir / ".gclient_entries",
            'entries = {"src": "https://example.com/src.git"}\n',
        )
        src_dir = workspace_dir / "src"
        angle_dir = src_dir / "third_party/angle"
        standalone_c1, standalone_c2 = _init_commits(
            angle_dir / "standalone_dep", "st1", "st2"
        )
        active_c1, active_c2 = _init_commits(
            angle_dir / "active_dep", "ac1", "ac2"
        )
        ios_c1, ios_c2 = _init_commits(src_dir / "ios_only", "ios1", "ios2")
        android_c1, android_c2 = _init_commits(
            src_dir / "android_only", "and1", "and2"
        )

        _init_repo(angle_dir)
        angle_deps = (
            "use_relative_paths = True\n"
            'target_os = ["fuchsia"]\n'
            'vars = {"build_with_chromium": False, "enable_extra": False}\n'
            "deps = {\n"
            '  "standalone_dep": {\n'
            '    "url": "https://example.com/st.git@{standalone}",\n'
            '    "condition": "not build_with_chromium",\n'
            "  },\n"
            '  "active_dep": {\n'
            '    "url": "https://example.com/ac.git@{active}",\n'
            '    "condition": "build_with_chromium and enable_extra '
            'and checkout_fuchsia",\n'
            "  },\n"
            "}\n"
        )
        angle_c1 = _commit_file(
            angle_dir,
            "DEPS",
            angle_deps.replace("{standalone}", standalone_c1).replace(
                "{active}", active_c1
            ),
            "ang1",
        )
        angle_c2 = _commit_file(
            angle_dir,
            "DEPS",
            angle_deps.replace("{standalone}", standalone_c2).replace(
                "{active}", active_c2
            ),
            "ang2",
        )

        _init_repo(src_dir)
        _write_file(
            src_dir / "DEPS",
            'git_dependencies = "SUBMODULES"\n'
            'vars = {"build_with_chromium": True, "h": Var("host_os")}\n',
        )
        _write_file(
            src_dir / ".gitmodules",
            '[submodule "third_party/angle"]\n'
            "\tpath = third_party/angle\n\tgclient-recursedeps = true\n"
            '[submodule "ios_only"]\n'
            '\tpath = ios_only\n\tgclient-condition = "checkout_ios"\n'
            '[submodule "android_only"]\n'
            "\tpath = android_only\n"
            '\tgclient-condition = "checkout_android and enable_extra"\n',
        )
        _git(src_dir, "add", "DEPS", ".gitmodules")
        _stage_gitlink(src_dir, "third_party/angle", angle_c1)
        _stage_gitlink(src_dir, "ios_only", ios_c1)
        src_c1 = _commit_gitlink(
            src_dir, "android_only", android_c1, "init sync"
        )
        src_c2_ios = _commit_gitlink(src_dir, "ios_only", ios_c2, "roll ios")
        _stage_gitlink(src_dir, "third_party/angle", angle_c2)
        src_c3 = _commit_gitlink(
            src_dir, "android_only", android_c2, "roll active"
        )

        angle_path = "src/third_party/angle"
        self.assertEqual(
            CommitDepsCollector(src_dir).collect(src_c1, src_c3),
            [
                GitCommitEntry("src", 0, src_c2_ios, roll=False),
                GitCommitEntry("src/android_only", 1, android_c2, roll=False),
                GitCommitEntry(
                    f"{angle_path}/active_dep", 2, active_c2, roll=False
                ),
                GitCommitEntry(angle_path, 1, angle_c2, roll=True),
                GitCommitEntry("src", 0, src_c3, roll=True),
            ],
        )

        # custom_deps disabling src/android_only suppresses its roll.
        _write_file(
            workspace_dir / ".gclient",
            "solutions = [{\n"
            '  "name": "src",\n'
            '  "url": "https://example.com/src.git",\n'
            '  "custom_vars": {"enable_extra": True},\n'
            '  "custom_deps": {"src/android_only": None},\n'
            "}]\n"
            'target_os = ["android"]\n',
        )
        self.assertEqual(
            CommitDepsCollector(src_dir).collect(src_c1, src_c3),
            [
                GitCommitEntry("src", 0, src_c2_ios, roll=False),
                GitCommitEntry(
                    f"{angle_path}/active_dep", 2, active_c2, roll=False
                ),
                GitCommitEntry(angle_path, 1, angle_c2, roll=True),
                GitCommitEntry("src", 0, src_c3, roll=True),
            ],
        )

    def test_fetch_warnings_and_url_filter(self) -> None:
        workspace_dir = self.workspace / "fetch_ws"
        _setup_gclient(workspace_dir)
        src_dir = workspace_dir / "src"
        v8_dir = workspace_dir / "src/v8"
        upstream_v8 = self.workspace / "upstream_v8"
        v8_c1, v8_c2, v8_c3 = _init_commits(upstream_v8, "v1", "v2", "v3")

        _init_repo(v8_dir)
        _git(v8_dir, "remote", "add", "origin", str(upstream_v8))
        _git(v8_dir, "fetch", "--quiet", "origin", v8_c1)
        _git(v8_dir, "checkout", "--detach", "--quiet", v8_c1)

        missing_c1 = "1" * 40
        missing_c2 = "2" * 40
        _init_repo(src_dir)
        deps_template = (
            "deps = {{\n"
            '  "src/missing": "https://example.com/missing.git@{missing}",\n'
            '  "src/v8": "https://example.com/v8.git@{v8}",\n'
            "}}\n"
        )
        src_c1 = _commit_file(
            src_dir,
            "DEPS",
            deps_template.format(missing=missing_c1, v8=v8_c1),
            "1",
        )
        src_c2 = _commit_file(
            src_dir,
            "DEPS",
            deps_template.format(missing=missing_c2, v8=v8_c1),
            "2",
        )
        src_c3 = _commit_file(
            src_dir,
            "DEPS",
            deps_template.format(missing=missing_c2, v8=v8_c3),
            "3",
        )

        with contextlib.redirect_stderr(io.StringIO()) as stderr_buf:
            unfetched = CommitDepsCollector(src_dir).collect(src_c1, src_c3)
        stderr_text = stderr_buf.getvalue()
        self.assertIn("src/missing", stderr_text)
        self.assertIn("subrepository checkout not found locally.", stderr_text)
        self.assertIn("src/v8", stderr_text)
        self.assertIn("commit not fetched locally.", stderr_text)
        self.assertEqual(
            unfetched,
            [
                GitCommitEntry("src", 0, src_c2, roll=True),
                GitCommitEntry("src", 0, src_c3, roll=True),
            ],
        )

        with contextlib.redirect_stderr(io.StringIO()) as fetch_stderr:
            fetched = CommitDepsCollector(
                src_dir, filters=["https://example.com/v8.git"], fetch=True
            ).collect(src_c1, src_c3)
        self.assertEqual(fetch_stderr.getvalue(), "")
        self.assertEqual(
            fetched,
            [
                GitCommitEntry("src", 0, src_c2, roll=False),
                GitCommitEntry("src/v8", 1, v8_c2, roll=False),
                GitCommitEntry("src/v8", 1, v8_c3, roll=False),
                GitCommitEntry("src", 0, src_c3, roll=True),
            ],
        )

    def test_repo_filter_paths_and_urls(self) -> None:
        repos = self._setup_nested_repos()
        for filter_spec in (
            "v8",
            "  /src/v8/  ",
            "src/v8/third_party/sub",
            "v8/third_party/sub",
        ):
            with self.subTest(filter_spec=filter_spec):
                filtered = CommitDepsCollector(
                    repos.src_dir, filters=[filter_spec]
                ).collect(repos.src_c1, repos.src_c4)
                self.assertEqual(filtered, list(repos.entries))

        url_only = CommitDepsCollector(
            repos.src_dir, filters=["https://example.com/v8/"]
        ).collect(repos.src_c1, repos.src_c4)
        self.assertEqual(
            url_only,
            [
                GitCommitEntry("src", 0, repos.src_c2, roll=False),
                GitCommitEntry("src/v8", 1, repos.v8_c2, roll=False),
                GitCommitEntry("src/v8", 1, repos.v8_c3_roll, roll=False),
                GitCommitEntry("src", 0, repos.src_c3_roll, roll=True),
                GitCommitEntry("src", 0, repos.src_c4, roll=False),
            ],
        )
        unmatched = CommitDepsCollector(
            repos.src_dir, filters=["nonexistent"]
        ).collect(repos.src_c1, repos.src_c4)
        self.assertEqual(
            unmatched,
            [
                GitCommitEntry("src", 0, repos.src_c2, roll=False),
                GitCommitEntry("src", 0, repos.src_c3_roll, roll=False),
                GitCommitEntry("src", 0, repos.src_c4, roll=False),
            ],
        )
        for bad_pattern in ("", "/", ".", "..", "../v8", ".git", "https://"):
            with self.assertRaises(ValueError):
                RepoFilter([bad_pattern])

    def test_worktree_and_symlinked_workspace(self) -> None:
        repos = self._setup_nested_repos()
        worktree_dir = self.workspace / "external_wt"
        _git(
            repos.src_dir,
            "worktree",
            "add",
            "--detach",
            "--quiet",
            str(worktree_dir),
            repos.src_c4,
        )
        self.assertFalse((worktree_dir / "v8/.git").exists())
        worktree_collector = CommitDepsCollector(worktree_dir)
        self.assertFalse(worktree_collector.is_standalone)
        self.assertEqual(worktree_collector.display_path, "src")
        self.assertEqual(
            worktree_collector.collect(repos.src_c1, repos.src_c4),
            list(repos.entries),
        )

        phys_dir = self.workspace / "phys_src"
        phys_c1, phys_c2 = _init_commits(phys_dir, "p1", "p2")
        symlink_workspace = self.workspace / "sym_ws"
        _setup_gclient(symlink_workspace)
        symlink_src = symlink_workspace / "src"
        try:
            symlink_src.symlink_to(phys_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            if sys.platform.startswith("win"):
                return
            raise
        symlink_collector = CommitDepsCollector(symlink_src)
        self.assertFalse(symlink_collector.is_standalone)
        self.assertEqual(symlink_collector.display_path, "src")
        self.assertEqual(
            symlink_collector.collect(phys_c1, phys_c2),
            [GitCommitEntry("src", 0, phys_c2, roll=False)],
        )

    def test_repo_resolution(self) -> None:
        repos = self._setup_nested_repos()
        collector = CommitDepsCollector(Path("."), cwd=repos.src_dir)
        self.assertEqual(collector.display_path, "src")

        subdir = repos.src_dir / "nested/dir"
        subdir.mkdir(parents=True)
        self.assertEqual(CommitDepsCollector(subdir).display_path, "src")

        with self.assertRaises(CommitDepsError):
            collector.collect("deadbeef" * 5, repos.src_c4)
        for non_repo in (self.workspace, self.workspace / "missing"):
            with (
                self.subTest(path=non_repo),
                self.assertRaises(CommitDepsError),
            ):
                CommitDepsCollector(non_repo)

    def test_temporal_deps_cipd_gcs_gitlinks_and_roll_revert(self) -> None:
        workspace_dir = self.workspace / "temporal_ws"
        _setup_gclient(workspace_dir)
        src_dir = workspace_dir / "src"
        v8_dir = workspace_dir / "src/v8"
        leaf_c1, leaf_c2 = _init_commits(v8_dir / "leaf", "l1", "l2")
        submod_c1, submod_c2 = _init_commits(src_dir / "submod", "sm1", "sm2")

        _init_repo(v8_dir)
        v8_c1 = _commit_file(
            v8_dir, "DEPS", _deps_content({"leaf": leaf_c1}), "v1"
        )
        v8_c2 = _commit_file(v8_dir, "v.cc", "2\n", "v2")
        v8_c3 = _commit_file(
            v8_dir, "DEPS", _deps_content({"leaf": leaf_c2}), "v3"
        )

        gitlink_diff = scm.DiffTreeEntry(
            "160000", "160000", submod_c1, submod_c2, "submod"
        )
        self.assertEqual(
            SubrepoRoll.from_diff_entry(gitlink_diff),
            SubrepoRoll("submod", submod_c1, submod_c2),
        )

        _init_repo(src_dir)
        sha256_a = "a" * 64
        sha256_b = "b" * 64
        c1_init = _commit_file(
            src_dir,
            "DEPS",
            _temporal_deps(v8_c1, "1" * 40, sha256_a, True),
            "c1",
        )
        c2_roll = _commit_file(
            src_dir,
            "DEPS",
            _temporal_deps(v8_c3, "1" * 40, sha256_a, True),
            "c2",
        )
        c3_drop = _commit_file(
            src_dir,
            "DEPS",
            _temporal_deps(v8_c3, "1" * 40, sha256_a, False),
            "c3",
        )
        c4_cipd = _commit_file(
            src_dir,
            "DEPS",
            _temporal_deps(v8_c3, "2" * 40, sha256_a, False),
            "c4",
        )
        c5_gcs = _commit_file(
            src_dir,
            "DEPS",
            _temporal_deps(v8_c3, "2" * 40, sha256_b, False),
            "c5",
        )
        c6_add_gitlink = _commit_gitlink(
            src_dir, "submod", submod_c1, "c6 add submod"
        )
        c7_roll_gitlink = _commit_gitlink(
            src_dir, "submod", submod_c2, "c7 roll submod"
        )
        _git(src_dir, "rm", "--cached", "--quiet", "submod")
        _git(src_dir, "commit", "--quiet", "-m", "c8 delete submod")
        c8_del_gitlink = _git(src_dir, "rev-parse", "HEAD")
        c9_revert = _commit_file(
            src_dir,
            "DEPS",
            _temporal_deps(v8_c1, "2" * 40, sha256_b, False),
            "c9",
        )
        c10_malformed = _commit_file(
            src_dir, "DEPS", f"deps = {{\n  broken {'3' * 40}\n", "c10"
        )
        c11_non_sha = _commit_file(
            src_dir,
            "DEPS",
            f'deps = {{".": "https://example.com/r.git@{"4" * 40}", '
            '"src/ref": "https://example.com/ref.git@refs/heads/main"}\n',
            "c11",
        )

        self.assertEqual(
            CommitDepsCollector(src_dir).collect(c1_init, c11_non_sha),
            [
                GitCommitEntry("src/v8", 1, v8_c2, roll=False),
                GitCommitEntry("src/v8/leaf", 2, leaf_c2, roll=False),
                GitCommitEntry("src/v8", 1, v8_c3, roll=True),
                GitCommitEntry("src", 0, c2_roll, roll=True),
                GitCommitEntry("src", 0, c3_drop, roll=False),
                GitCommitEntry("src", 0, c4_cipd, roll=True),
                GitCommitEntry("src", 0, c5_gcs, roll=True),
                GitCommitEntry("src", 0, c6_add_gitlink, roll=True),
                GitCommitEntry("src/submod", 1, submod_c2, roll=False),
                GitCommitEntry("src", 0, c7_roll_gitlink, roll=True),
                GitCommitEntry("src", 0, c8_del_gitlink, roll=True),
                GitCommitEntry("src/v8", 1, v8_c2, roll=False),
                GitCommitEntry("src/v8", 1, v8_c1, roll=False),
                GitCommitEntry("src", 0, c9_revert, roll=True),
                GitCommitEntry("src", 0, c10_malformed, roll=False),
                GitCommitEntry("src", 0, c11_non_sha, roll=False),
            ],
        )

    def test_merge_commit_no_duplicate_rolls_and_include_start(self) -> None:
        repo_dir = self.workspace / "merge_repo"
        sub_c1, sub_c2 = _init_commits(repo_dir / "sub", "s1", "s2")

        _init_repo(repo_dir)
        root_commit = _commit_file(
            repo_dir, "DEPS", _deps_content({"sub": sub_c1}), "root"
        )
        _git(repo_dir, "checkout", "-b", "side", "--quiet")
        _commit_file(
            repo_dir, "DEPS", _deps_content({"sub": sub_c2}), "side roll"
        )
        _git(repo_dir, "checkout", "main", "--quiet")
        main_commit = _commit_file(repo_dir, "file.txt", "main\n", "c_main")
        _git(repo_dir, "merge", "--no-ff", "--quiet", "-m", "Merge", "side")
        merge_commit = _git(repo_dir, "rev-parse", "HEAD")
        after_commit = _commit_file(repo_dir, "file.txt", "after\n", "c_after")

        full_collector = CommitDepsCollector(repo_dir, first_parent=False)
        full_entries = full_collector.collect(root_commit, merge_commit)
        self.assertEqual(
            [entry for entry in full_entries if entry.path == "merge_repo/sub"],
            [GitCommitEntry("merge_repo/sub", 1, sub_c2, roll=False)],
        )
        self.assertEqual(
            [entry for entry in full_entries if entry.commit == merge_commit],
            [GitCommitEntry("merge_repo", 0, merge_commit, roll=False)],
        )
        self.assertEqual(
            CommitDepsCollector(repo_dir, first_parent=True).collect(
                root_commit, merge_commit
            ),
            [
                GitCommitEntry("merge_repo", 0, main_commit, roll=False),
                GitCommitEntry("merge_repo/sub", 1, sub_c2, roll=False),
                GitCommitEntry("merge_repo", 0, merge_commit, roll=True),
            ],
        )

        # Single-commit & --include-start on merge/root must not leak ^2 or
        # pass Windows-unsafe '^' revision suffixes to scm.GIT.
        with (
            mock.patch.object(
                scm.GIT, "ResolveCommit", wraps=scm.GIT.ResolveCommit
            ) as mock_resolve,
            mock.patch.object(
                scm.GIT, "GetCommitLogDiffs", wraps=scm.GIT.GetCommitLogDiffs
            ) as mock_log,
        ):
            self.assertEqual(
                full_collector.collect(merge_commit),
                [
                    GitCommitEntry("merge_repo/sub", 1, sub_c2, roll=False),
                    GitCommitEntry("merge_repo", 0, merge_commit, roll=True),
                ],
            )
            included_merge_entries = full_collector.collect(
                merge_commit, after_commit, include_start=True
            )
            self.assertEqual(
                full_collector.collect(
                    root_commit, main_commit, include_start=True
                ),
                [
                    GitCommitEntry("merge_repo", 0, root_commit, roll=True),
                    GitCommitEntry("merge_repo", 0, main_commit, roll=False),
                ],
            )
        seen_specs = [
            call_item.args[1]
            for call_item in (
                *mock_resolve.call_args_list,
                *mock_log.call_args_list,
            )
        ]
        self.assertEqual(
            [
                entry.commit
                for entry in included_merge_entries
                if entry.level == 0
            ],
            [merge_commit, after_commit],
        )
        self.assertTrue(seen_specs)
        self.assertFalse(any("^" in spec for spec in seen_specs))

    def test_ranges_empty_merge_base_and_max_count(self) -> None:
        commit_a = "a" * 40
        commit_b = "b" * 40
        commit_c = "c" * 40
        cases = [
            (
                (f"{commit_a}..{commit_c}",),
                CommitRange(commit_a, commit_c, True, False),
            ),
            (
                (f"{commit_a}..{commit_a}",),
                CommitRange(commit_a, commit_a, True, False),
            ),
            ((f"{commit_a}..",), CommitRange(commit_a, "HEAD", True, False)),
            ((f"..{commit_c}",), CommitRange("HEAD", commit_c, True, False)),
            (
                (f"{commit_a}...{commit_c}",),
                CommitRange(commit_a, commit_c, True, True),
            ),
            ((f"{commit_b}^!",), CommitRange(commit_b, commit_b, False, False)),
            ((commit_b,), CommitRange(commit_b, commit_b, False, False)),
            (
                (commit_a, commit_c),
                CommitRange(commit_a, commit_c, True, False),
            ),
            (("-n", "1"), CommitRange("HEAD", "HEAD", False, False)),
            (("-n", "2"), CommitRange("HEAD", "HEAD", True, False)),
            (
                (commit_b, "-n", "1"),
                CommitRange(commit_b, commit_b, False, False),
            ),
            (
                (commit_b, "-n", "2"),
                CommitRange(commit_b, commit_b, True, False),
            ),
            (
                (f"{commit_a}..{commit_c}", "-n", "1"),
                CommitRange(commit_a, commit_c, True, False),
            ),
            (
                (f"{commit_a}...{commit_c}", "-n", "1"),
                CommitRange(commit_a, commit_c, True, True),
            ),
            (
                (commit_a, commit_c, "-n", "1"),
                CommitRange(commit_a, commit_c, True, False),
            ),
        ]
        for cli_args, expected in cases:
            with self.subTest(cli_args=cli_args):
                resolved = self._resolve_cli_range(*cli_args)
                self.assertEqual(resolved, expected)
                self.assertEqual(
                    resolved.is_single_commit, not expected.is_range
                )

        for invalid_args, max_count in (
            ([".."], None),
            (["..."], None),
            ([], None),
            ([commit_a, commit_b, commit_c], None),
            ([commit_a, commit_b, commit_c], 2),
        ):
            with (
                self.subTest(invalid_args=invalid_args, max_count=max_count),
                self.assertRaises(CommitDepsError),
            ):
                CommitRange.from_args(invalid_args, max_count=max_count)

        repo_dir = self.workspace / "range_repo"
        commit_1, commit_2 = _init_commits(repo_dir, "r1", "r2")
        _git(repo_dir, "checkout", "-b", "div", "--quiet")
        diverged_commit = _commit_file(repo_dir, "div.txt", "d\n", "r_div")
        _git(repo_dir, "checkout", "main", "--quiet")
        commit_3 = _commit_file(repo_dir, "f.txt", "3\n", "r3")
        commit_4 = _commit_file(repo_dir, "f.txt", "4\n", "r4")
        _git(repo_dir, "update-ref", "refs/remotes/origin/main", commit_4)

        self.assertEqual(
            self._run_cli_commits(repo_dir, f"{commit_4}..{commit_4}"), []
        )
        self.assertEqual(
            self._run_cli_commits(repo_dir, "origin/main..HEAD"), []
        )
        self.assertEqual(self._run_cli_commits(repo_dir, commit_4), [commit_4])
        self.assertEqual(
            self._run_cli_commits(repo_dir, f"{diverged_commit}...{commit_4}"),
            [commit_3, commit_4],
        )
        self.assertEqual(
            self._run_cli_commits(
                repo_dir, "--include-start", f"{diverged_commit}...{commit_4}"
            ),
            [commit_2, commit_3, commit_4],
        )
        self.assertEqual(
            self._run_cli_commits(
                repo_dir, f"{commit_1}..{commit_4}", "-n", "2"
            ),
            [commit_3, commit_4],
        )
        self.assertEqual(
            self._run_cli_commits(repo_dir, "-n", "2"), [commit_3, commit_4]
        )
        self.assertEqual(self._run_cli_commits(repo_dir, "-n", "1"), [commit_4])
        self.assertEqual(
            self._run_cli_commits(repo_dir, commit_3, "--max-count=2"),
            [commit_2, commit_3],
        )

    def test_commit_position_and_version_tag_hint(self) -> None:
        repo_dir = self.workspace / "cr_pos_repo"
        _init_repo(repo_dir)
        position_shas = [
            _commit_file(
                repo_dir,
                "a.txt",
                f"{pos_num}\n",
                f"pos {pos_num}\n\n"
                f"Cr-Commit-Position: refs/heads/main@{{#{pos_num}}}",
            )
            for pos_num in (100, 101, 102, 100001, 100002)
        ]
        _, pos_101_sha, pos_102_sha, _, pos_100002_sha = position_shas

        collector = CommitDepsCollector(repo_dir)
        position_entries = collector.collect(
            "refs/heads/main@{#100}", "refs/heads/main@{#102}"
        )
        self.assertEqual(
            [entry.commit for entry in position_entries],
            [pos_101_sha, pos_102_sha],
        )
        self.assertEqual(
            [entry.commit_position for entry in position_entries],
            ["refs/heads/main@{#101}", "refs/heads/main@{#102}"],
        )
        self.assertEqual(
            [entry.commit for entry in collector.collect("100001", "100002")],
            [pos_100002_sha],
        )
        self.assertEqual(
            self._run_cli_commits(
                repo_dir, "Cr-Commit-Position: refs/heads/main@{#100002}"
            ),
            [pos_100002_sha],
        )

        for missing_ref in ("refs/heads/main@{#999999}", "@{#999999}"):
            with self.assertRaises(CommitDepsError):
                collector.collect("HEAD~1", missing_ref)

        with self.assertRaises(CommitDepsError) as exc_context:
            collector.collect("154.0.1.2", "HEAD")
        self.assertIn("154.0.1.2", str(exc_context.exception))
        self.assertIn("tag", str(exc_context.exception))

    def test_branch_head_commit_position(self) -> None:
        repo_dir = self.workspace / "branch_head_repo"
        _init_repo(repo_dir)
        branch_c1 = _commit_file(
            repo_dir,
            "a.txt",
            "1\n",
            "m1\n\nCr-Commit-Position: refs/branch-heads/6099@{#1}",
        )
        branch_c2 = _commit_file(
            repo_dir,
            "a.txt",
            "2\n",
            "m2\n\nCr-Commit-Position: refs/branch-heads/6099@{#2}",
        )
        _git(
            repo_dir, "update-ref", "refs/remotes/branch-heads/6099", branch_c2
        )
        _git(repo_dir, "checkout", "--detach", "--quiet", branch_c1)
        self.assertEqual(
            self._run_cli_commits(
                repo_dir,
                "branch-heads/6099@{#1}",
                "cr-commit-position: refs/branch-heads/6099@{#2}",
            ),
            [branch_c2],
        )
        self.assertEqual(
            self._run_cli_commits(
                repo_dir,
                "Cr-Commit-Position: origin/branch-heads/6099@{#1}.."
                "Cr-Commit-Position: "
                "refs/remotes/origin/branch-heads/6099@{#2}",
            ),
            [branch_c2],
        )

    def test_cyclic_deps_rolls(self) -> None:
        repo_a = self.workspace / "cycle_a"
        repo_b = repo_a / "sub_b"
        _init_repo(repo_a)
        _init_repo(repo_b)

        repo_a_c0 = _commit_file(repo_a, "a.txt", "0\n", "a c0")
        _git(repo_a, "checkout", "-b", "placeholder", "--quiet")
        repo_a_c2 = _commit_file(repo_a, "a.txt", "2\n", "a c2 placeholder")
        _git(repo_a, "checkout", "main", "--quiet")

        repo_b_c1 = _commit_file(
            repo_b, "DEPS", _deps_content({"..": repo_a_c0}, [".."]), "b1"
        )
        repo_b_c2 = _commit_file(
            repo_b, "DEPS", _deps_content({"..": repo_a_c2}, [".."]), "b2"
        )
        repo_a_c1 = _commit_file(
            repo_a,
            "DEPS",
            _deps_content({"sub_b": repo_b_c1}, ["sub_b"]),
            "a1",
        )
        repo_a_c3 = _commit_file(
            repo_a,
            "DEPS",
            _deps_content({"sub_b": repo_b_c2}, ["sub_b"]),
            "a3",
        )
        _git(repo_a, "replace", repo_a_c2, repo_a_c3)

        self.assertEqual(
            CommitDepsCollector(repo_a).collect(repo_a_c0, repo_a_c2),
            [
                GitCommitEntry("cycle_a", 0, repo_a_c1, roll=True),
                GitCommitEntry("cycle_a/sub_b", 1, repo_b_c2, roll=True),
                GitCommitEntry("cycle_a", 0, repo_a_c2, roll=True),
            ],
        )

    def test_formats_table_csv_json(self) -> None:
        long_subject = "S" * 80
        sub_path = "src/v8/third_party/sub"
        entries = [
            GitCommitEntry(
                "src", 0, "a" * 40, True, "Roll V8", "refs/heads/main@{#10}"
            ),
            GitCommitEntry("src/v8", 1, "b" * 40, True, long_subject),
            GitCommitEntry(sub_path, 2, "c" * 40, False, "Sub"),
            GitCommitEntry("src", 0, "d" * 40, False, "Fix"),
        ]
        expected_pinned_shas = [
            {"src": "a" * 40},
            {"src": "a" * 40, "src/v8": "b" * 40},
            {"src": "a" * 40, "src/v8": "b" * 40, sub_path: "c" * 40},
            {"src": "d" * 40},
        ]
        expected_commands = [
            f"gclient sync --revision src@{'a' * 40}",
            (
                f"gclient sync --revision src@{'a' * 40} "
                f"--revision src/v8@{'b' * 40}"
            ),
            (
                f"gclient sync --revision src@{'a' * 40} "
                f"--revision src/v8@{'b' * 40} "
                f"--revision {sub_path}@{'c' * 40}"
            ),
            f"gclient sync --revision src@{'d' * 40}",
        ]
        self.assertEqual(
            GitCommitEntry.build_pinned_shas(entries), expected_pinned_shas
        )
        self.assertEqual(
            GitCommitEntry.build_sync_commands(entries), expected_commands
        )
        self.assertEqual(
            GitCommitEntry.build_sync_commands(entries, standalone=True),
            [f"git checkout --detach {entry.commit}" for entry in entries],
        )

        base_headers = ["Path", "Level", "commit hash", "roll", "subject"]
        for sync_command in (False, True):
            with self.subTest(sync_command=sync_command):
                table_output = OutputFormat.TABLE.format_entries(
                    entries, sync_command=sync_command
                )
                self.assertIn("| Path", table_output)
                self.assertIn("subject", table_output)
                self.assertIn("S" * 69 + "...", table_output)
                self.assertNotIn(long_subject, table_output)
                if sync_command:
                    self.assertIn("sync command", table_output)
                    for command in expected_commands:
                        self.assertIn(command, table_output)

                csv_text = OutputFormat.CSV.format_entries(
                    entries, sync_command=sync_command
                )
                csv_rows = list(csv.reader(io.StringIO(csv_text)))
                expected_headers = [
                    *base_headers,
                    *(["sync command"] if sync_command else []),
                ]
                self.assertEqual(csv_rows[0], expected_headers)
                self.assertEqual(csv_rows[2][4], long_subject)
                if sync_command:
                    self.assertEqual(
                        [row[5] for row in csv_rows[1:]], expected_commands
                    )

                json_items = json.loads(
                    OutputFormat.JSON.format_entries(
                        entries, sync_command=sync_command
                    )
                )
                expected_json = [
                    {
                        "commit": entry.commit,
                        "commit_position": entry.commit_position,
                        "level": entry.level,
                        "path": entry.path,
                        "pinned_shas": pinned_shas,
                        "roll": entry.roll,
                        "subject": entry.subject,
                        **({"sync_command": command} if sync_command else {}),
                    }
                    for entry, pinned_shas, command in zip(
                        entries, expected_pinned_shas, expected_commands
                    )
                ]
                self.assertEqual(json_items, expected_json)

    def test_main_cli_and_standalone_sync(self) -> None:
        repos = self._setup_nested_repos()
        exit_code, stdout, stderr = self._run_main(
            "--repo", str(repos.src_dir), "--format=json", repos.src_c3_roll
        )
        self.assertEqual((exit_code, stderr), (0, ""))
        json_plain = json.loads(stdout)
        self.assertEqual(
            [item["commit"] for item in json_plain],
            [entry.commit for entry in repos.roll_entries],
        )
        self.assertTrue(all("pinned_shas" in item for item in json_plain))
        self.assertTrue(all("subject" in item for item in json_plain))
        self.assertTrue(all("sync_command" not in item for item in json_plain))

        # Attached branch on .gclient repo warns on stderr with --sync-command.
        exit_code, stdout, stderr = self._run_main(
            "--repo",
            str(repos.src_dir),
            "--format=json",
            "--sync-command",
            repos.src_c3_roll,
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("git checkout --detach", stderr)
        self.assertIn(
            "gclient sync --revision", json.loads(stdout)[0]["sync_command"]
        )

        # Detached HEAD on .gclient repo emits no branch warning.
        _git(repos.src_dir, "checkout", "--detach", "--quiet")
        exit_code, _, stderr = self._run_main(
            "--repo", str(repos.src_dir), "--sync-command", repos.src_c4
        )
        self.assertEqual((exit_code, stderr), (0, ""))

        # Standalone non-.gclient repo emits `git checkout --detach <sha>`.
        solo_dir = self.workspace / "standalone_repo"
        (solo_c1,) = _init_commits(solo_dir, "solo 1")
        exit_code, stdout, stderr = self._run_main(
            "--repo", str(solo_dir), "--format=json", "--sync-command", solo_c1
        )
        self.assertEqual((exit_code, stderr), (0, ""))
        self.assertEqual(
            json.loads(stdout)[0]["sync_command"],
            f"git checkout --detach {solo_c1}",
        )

        with mock.patch.object(gclient_utils, "IsEnvCog", return_value=True):
            cog_code, _, cog_stderr = self._run_main("HEAD")
        self.assertEqual(cog_code, 1)
        self.assertIn("not supported in non-git environments", cog_stderr)

        error_code, _, error_stderr = self._run_main(
            "--repo", str(repos.src_dir)
        )
        self.assertEqual(error_code, 1)
        self.assertIn("Error:", error_stderr)

        # TTY pager is skipped when TERM=dumb, GIT_PAGER=cat, or entries=[].
        pager_buffer = io.BytesIO()
        less_context = contextlib.nullcontext(pager_buffer)
        with (
            mock.patch.object(setup_color, "init"),
            mock.patch.object(setup_color, "IS_TTY", True),
            mock.patch.object(shutil, "which", return_value="less"),
            mock.patch.object(
                git_common, "less", return_value=less_context
            ) as mock_less,
        ):
            for env_overrides, revision in (
                ({"TERM": "dumb", "GIT_PAGER": "less"}, repos.src_c4),
                ({"TERM": "xterm", "GIT_PAGER": "cat"}, repos.src_c4),
                ({"TERM": "xterm", "GIT_PAGER": "less"}, "HEAD..HEAD"),
            ):
                with mock.patch.dict(os.environ, env_overrides):
                    exit_code, _, _ = self._run_main(
                        "--repo", str(repos.src_dir), revision
                    )
                self.assertEqual(exit_code, 0)
            mock_less.assert_not_called()
            with mock.patch.dict(
                os.environ, {"TERM": "xterm", "GIT_PAGER": "less"}
            ):
                exit_code, _, _ = self._run_main(
                    "--repo", str(repos.src_dir), repos.src_c4
                )
            self.assertEqual(exit_code, 0)
            mock_less.assert_called_once()

    def test_parse_args_validation(self) -> None:
        args = InputParser.parse_args(
            [
                "  HEAD~2..HEAD  ",
                "--repo=-dash-dir",
                "--format=JSON",
                "--include-start",
                "--first-parent",
                "--max-level=0",
                "-n",
                "5",
                "--filter=  /src/v8/  ",
                "--filter=https://example.com/v8.git",
                "--fetch",
                "--sync-commands",
            ]
        )
        self.assertEqual(args.commits, ["HEAD~2..HEAD"])
        self.assertEqual(args.repo, Path("-dash-dir"))
        self.assertEqual(args.format, OutputFormat.JSON)
        self.assertTrue(args.include_start and args.first_parent)
        self.assertEqual((args.max_level, args.max_count), (0, 5))
        self.assertIn("https://example.com/v8.git", args.filter)
        self.assertTrue(args.fetch and args.sync_command)

        for bad_argv in (
            [""],
            ["   "],
            ["a\x00b"],
            ["--repo="],
            ["--max-level=-1"],
            ["--max-level=abc"],
            ["-n", "0"],
            ["--max-count=-1"],
            ["--filter="],
            ["--filter=/"],
            ["--filter=."],
            ["--format=xml"],
        ):
            with (
                self.subTest(argv=bad_argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                InputParser.parse_args(bad_argv)

    def _setup_nested_repos(self) -> _NestedRepos:
        workspace_dir = self.workspace / "nested_ws"
        _setup_gclient(workspace_dir)
        src_dir = workspace_dir / "src"
        v8_dir = workspace_dir / "src/v8"
        sub_c1, sub_c2, sub_c3 = _init_commits(
            v8_dir / "third_party/sub", "sub c1", "sub c2", "sub c3"
        )

        _init_repo(v8_dir)
        v8_c1 = _commit_file(
            v8_dir, "DEPS", _deps_content({"third_party/sub": sub_c1}), "v8 c1"
        )
        v8_c2 = _commit_file(v8_dir, "v8.cc", "int v = 1;\n", "v8 regular work")
        v8_c3_roll = _commit_file(
            v8_dir,
            "DEPS",
            _deps_content({"third_party/sub": sub_c3}),
            "Roll sub c1..c3",
        )

        _init_repo(src_dir)
        src_c1 = _commit_file(src_dir, "DEPS", _src_deps(v8_c1), "src init")
        src_c2 = _commit_file(src_dir, "main.cc", "int m = 1;\n", "src change")
        src_c3_roll = _commit_file(
            src_dir, "DEPS", _src_deps(v8_c3_roll), "Roll V8 v8_c1..v8_c3"
        )
        src_c4 = _commit_file(src_dir, "main.cc", "int m = 2;\n", "src after")

        sub_path = "src/v8/third_party/sub"
        roll_entries = (
            GitCommitEntry("src/v8", 1, v8_c2, False, "v8 regular work"),
            GitCommitEntry(sub_path, 2, sub_c2, False, "sub c2"),
            GitCommitEntry(sub_path, 2, sub_c3, False, "sub c3"),
            GitCommitEntry("src/v8", 1, v8_c3_roll, True, "Roll sub c1..c3"),
            GitCommitEntry("src", 0, src_c3_roll, True, "Roll V8 v8_c1..v8_c3"),
        )
        entries = (
            GitCommitEntry("src", 0, src_c2, False, "src change"),
            *roll_entries,
            GitCommitEntry("src", 0, src_c4, False, "src after"),
        )
        return _NestedRepos(
            src_dir,
            src_c1,
            src_c2,
            src_c3_roll,
            src_c4,
            v8_c2,
            v8_c3_roll,
            sub_c2,
            sub_c3,
            entries,
            roll_entries,
        )

    def _resolve_cli_range(self, *cli_args: str) -> CommitRange:
        args = InputParser.parse_args(list(cli_args))
        return CommitRange.from_args(args.commits, max_count=args.max_count)

    def _run_cli_commits(self, repo_dir: Path, *cli_args: str) -> list[str]:
        exit_code, stdout, _ = self._run_main(
            "--repo", str(repo_dir), "--format=json", *cli_args
        )
        self.assertEqual(exit_code, 0)
        return [item["commit"] for item in json.loads(stdout)]

    def _run_main(self, *cli_args: str) -> tuple[int, str, str]:
        with (
            contextlib.redirect_stdout(io.StringIO()) as stdout_buf,
            contextlib.redirect_stderr(io.StringIO()) as stderr_buf,
        ):
            exit_code = commit_deps.main(list(cli_args))
        return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


def _setup_gclient(workspace_dir: Path) -> None:
    _write_file(
        workspace_dir / ".gclient",
        'solutions = [{"name": "src", "url": "https://example.com/src.git"}]\n',
    )
    _write_file(
        workspace_dir / ".gclient_entries",
        'entries = {"src": "https://example.com/src.git"}\n',
    )


def _init_commits(repo_dir: Path, *messages: str) -> list[str]:
    _init_repo(repo_dir)
    return [
        _commit_file(repo_dir, "f.txt", f"{index}\n", message)
        for index, message in enumerate(messages)
    ]


def _init_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-b", "main", "--quiet")


def _commit_file(
    repo_dir: Path, rel_path: str, content: str, message: str
) -> str:
    _write_file(repo_dir / rel_path, content)
    _git(repo_dir, "add", rel_path)
    _git(repo_dir, "commit", "--quiet", "-m", message)
    return _git(repo_dir, "rev-parse", "HEAD")


def _deps_content(
    deps_map: dict[str, str], recursedeps: list[str] | None = None
) -> str:
    entries = "\n".join(
        f'  "{dep_path}": "https://example.com/repo.git@{commit_sha}",'
        for dep_path, commit_sha in deps_map.items()
    )
    recursedeps_line = (
        f"recursedeps = {json.dumps(recursedeps)}\n" if recursedeps else ""
    )
    return (
        f"use_relative_paths = True\ndeps = {{\n{entries}\n}}\n"
        f"{recursedeps_line}"
    )


def _commit_gitlink(
    repo_dir: Path, rel_path: str, target_sha: str, message: str
) -> str:
    _stage_gitlink(repo_dir, rel_path, target_sha)
    _git(repo_dir, "commit", "--quiet", "-m", message)
    return _git(repo_dir, "rev-parse", "HEAD")


def _stage_gitlink(repo_dir: Path, rel_path: str, target_sha: str) -> None:
    _git(
        repo_dir,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{target_sha},{rel_path}",
    )


def _temporal_deps(
    v8_sha: str, cipd_sha: str, gcs_sha256: str, recurse: bool
) -> str:
    recursedeps_line = 'recursedeps = ["src/v8"]\n' if recurse else ""
    return (
        "deps = {\n"
        f'  "src/v8": "https://example.com/v8.git@{v8_sha}",\n'
        '  "src/cipd_pkg": {\n'
        '    "packages": [\n'
        f'      {{"package": "pkg", "version": "git_revision:{cipd_sha}"}},\n'
        "    ],\n"
        '    "dep_type": "cipd",\n'
        "  },\n"
        '  "src/gcs_pkg": {\n'
        '    "dep_type": "gcs",\n'
        '    "bucket": "test-bucket",\n'
        '    "objects": [{\n'
        '      "object_name": "llvm.tar.xz",\n'
        f'      "sha256sum": "{gcs_sha256}",\n'
        '      "size_bytes": 123,\n'
        '      "generation": 1,\n'
        "    }],\n"
        "  },\n"
        "}\n"
        f"{recursedeps_line}"
    )


def _src_deps(v8_sha: str) -> str:
    return (
        f'vars = {{"v8_git": "{v8_sha}"}}\n'
        'deps = {"src/v8": "https://example.com/v8.git@" + Var("v8_git")}\n'
        'recursedeps = ["src/v8"]\n'
    )


def _write_file(file_path: Path, content: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def _git(repo_dir: Path, *git_args: str) -> str:
    git_env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.check_output(
        [git_common.GIT_EXE, "-C", str(repo_dir), *git_args],
        env=git_env,
        shell=False,
        stderr=subprocess.STDOUT,
        text=True,
    ).strip()


if __name__ == "__main__":
    unittest.main()
