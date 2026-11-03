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
        self.workspace = Path(self.root_dir)

    def test_recursive_deps_expansion(self) -> None:
        repos = self._setup_nested_repos()
        collector = CommitDepsCollector(repos.src_dir)
        self.assertEqual(collector.display_path, "src")
        self.assertFalse(collector.is_standalone)
        collected = collector.collect(repos.src_c1, repos.src_c4)
        self.assertEqual(collected, list(repos.entries))
        self.assertEqual(
            [e.subject for e in collected],
            [e.subject for e in repos.entries],
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
                leveled = CommitDepsCollector(
                    repos.src_dir, max_level=max_level
                )
                self.assertEqual(
                    leveled.collect(repos.src_c1, repos.src_c4),
                    [e for e in repos.entries if e.level <= max_level],
                )

    def test_multi_subrepo_roll_pinned_shas(self) -> None:
        ws = self.workspace / "multi_ws"
        _setup_gclient(ws)
        src_dir = ws / "src"
        a0, a1, a2 = _init_commits(
            src_dir / "third_party/angle", "a0", "a1", "a2"
        )
        v0, v1, v2 = _init_commits(src_dir / "v8", "v0", "v1", "v2")

        _init_repo(src_dir)
        s0 = _commit_file(
            src_dir,
            "DEPS",
            _deps_content({"third_party/angle": a0, "v8": v0}),
            "s0",
        )
        s1 = _commit_file(
            src_dir,
            "DEPS",
            _deps_content({"third_party/angle": a2, "v8": v2}),
            "s1 multi roll",
        )

        entries = CommitDepsCollector(src_dir).collect(s0, s1)
        self.assertEqual(
            entries,
            [
                GitCommitEntry("src/third_party/angle", 1, a1, roll=False),
                GitCommitEntry("src/third_party/angle", 1, a2, roll=False),
                GitCommitEntry("src/v8", 1, v1, roll=False),
                GitCommitEntry("src/v8", 1, v2, roll=False),
                GitCommitEntry("src", 0, s1, roll=True),
            ],
        )
        self.assertEqual(
            GitCommitEntry.build_pinned_shas(entries),
            [
                {"src": s0, "src/third_party/angle": a1},
                {"src": s0, "src/third_party/angle": a2},
                {"src": s0, "src/third_party/angle": a2, "src/v8": v1},
                {"src": s0, "src/third_party/angle": a2, "src/v8": v2},
                {"src": s1},
            ],
        )

    def test_recursedeps_custom_deps_file_and_subrepo_target(self) -> None:
        ws = self.workspace / "recurse_ws"
        _setup_gclient(ws)
        src_dir = ws / "src"
        skia_dir = src_dir / "third_party/skia"
        custom_dir = src_dir / "third_party/custom"
        leaf_c1, leaf_c2 = _init_commits(skia_dir / "leaf", "l1", "l2")
        in_c1, in_c2 = _init_commits(custom_dir / "inner", "i1", "i2")

        _init_repo(skia_dir)
        skia_c1 = _commit_file(
            skia_dir, "DEPS", _deps_content({"leaf": leaf_c1}), "s1"
        )
        skia_c2 = _commit_file(
            skia_dir, "DEPS", _deps_content({"leaf": leaf_c2}), "s2"
        )

        _init_repo(custom_dir)
        cust_c1 = _commit_file(
            custom_dir, "DEPS.chromium", _deps_content({"inner": in_c1}), "c1"
        )
        cust_c2 = _commit_file(
            custom_dir, "DEPS.chromium", _deps_content({"inner": in_c2}), "c2"
        )

        _init_repo(src_dir)
        deps_tpl = (
            "deps = {{\n"
            '  "src/third_party/skia": "https://example.com/skia.git@{skia}",\n'
            '  "src/third_party/custom": '
            '"https://example.com/custom.git@{cust}",\n'
            "}}\n"
            'recursedeps = [("src/third_party/custom", "DEPS.chromium")]\n'
        )
        src_c1 = _commit_file(
            src_dir, "DEPS", deps_tpl.format(skia=skia_c1, cust=cust_c1), "init"
        )
        src_c2 = _commit_file(
            src_dir, "DEPS", deps_tpl.format(skia=skia_c2, cust=cust_c2), "roll"
        )

        skia_p, cust_p = "src/third_party/skia", "src/third_party/custom"
        entries = CommitDepsCollector(src_dir).collect(src_c1, src_c2)
        self.assertEqual(
            entries,
            [
                GitCommitEntry(f"{cust_p}/inner", 2, in_c2, roll=False),
                GitCommitEntry(cust_p, 1, cust_c2, roll=True),
                GitCommitEntry(skia_p, 1, skia_c2, roll=False),
                GitCommitEntry("src", 0, src_c2, roll=True),
            ],
        )

        # --repo on non-recursedeps subrepo suppresses nested DEPS & pins src.
        skia_sub = CommitDepsCollector(skia_dir).collect(skia_c1, skia_c2)
        self.assertEqual(
            skia_sub, [GitCommitEntry(skia_p, 0, skia_c2, roll=False)]
        )
        self.assertEqual(
            GitCommitEntry.build_pinned_shas(skia_sub),
            [{"src": src_c2, skia_p: skia_c2}],
        )

        # --repo on recursedeps subrepo honors DEPS.chromium & pins src.
        cust_sub = CommitDepsCollector(custom_dir).collect(cust_c1, cust_c2)
        self.assertEqual(
            cust_sub,
            [
                GitCommitEntry(f"{cust_p}/inner", 1, in_c2, roll=False),
                GitCommitEntry(cust_p, 0, cust_c2, roll=True),
            ],
        )
        self.assertEqual(
            GitCommitEntry.build_pinned_shas(cust_sub),
            [
                {"src": src_c2, cust_p: cust_c1, f"{cust_p}/inner": in_c2},
                {"src": src_c2, cust_p: cust_c2},
            ],
        )

    def test_conditions_vars_and_gitmodules(self) -> None:
        ws = self.workspace / "cond_ws"
        _write_file(
            ws / ".gclient",
            "solutions = [{\n"
            '  "name": "src",\n'
            '  "url": "https://example.com/src.git",\n'
            '  "custom_vars": {"enable_extra": True},\n'
            "}]\n"
            'target_os = ["android"]\n',
        )
        _write_file(
            ws / ".gclient_entries",
            'entries = {"src": "https://example.com/src.git"}\n',
        )
        src_dir = ws / "src"
        angle_dir = src_dir / "third_party/angle"
        st_1, st_2 = _init_commits(angle_dir / "standalone_dep", "st1", "st2")
        ac_1, ac_2 = _init_commits(angle_dir / "active_dep", "ac1", "ac2")
        ios_1, ios_2 = _init_commits(src_dir / "ios_only", "ios1", "ios2")
        and_1, and_2 = _init_commits(src_dir / "android_only", "and1", "and2")

        _init_repo(angle_dir)
        angle_deps = (
            "use_relative_paths = True\n"
            'vars = {"build_with_chromium": False, "enable_extra": False}\n'
            "deps = {\n"
            '  "standalone_dep": {\n'
            '    "url": "https://example.com/st.git@{st}",\n'
            '    "condition": "not build_with_chromium",\n'
            "  },\n"
            '  "active_dep": {\n'
            '    "url": "https://example.com/ac.git@{ac}",\n'
            '    "condition": "build_with_chromium and enable_extra",\n'
            "  },\n"
            "}\n"
        )
        ang_1 = _commit_file(
            angle_dir,
            "DEPS",
            angle_deps.replace("{st}", st_1).replace("{ac}", ac_1),
            "ang1",
        )
        ang_2 = _commit_file(
            angle_dir,
            "DEPS",
            angle_deps.replace("{st}", st_2).replace("{ac}", ac_2),
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
        _stage_gitlink(src_dir, "third_party/angle", ang_1)
        _stage_gitlink(src_dir, "ios_only", ios_1)
        src_c1 = _commit_gitlink(src_dir, "android_only", and_1, "init sync")
        src_c2_ios = _commit_gitlink(src_dir, "ios_only", ios_2, "roll ios")
        _stage_gitlink(src_dir, "third_party/angle", ang_2)
        src_c3 = _commit_gitlink(src_dir, "android_only", and_2, "roll active")

        ang_p = "src/third_party/angle"
        self.assertEqual(
            CommitDepsCollector(src_dir).collect(src_c1, src_c3),
            [
                GitCommitEntry("src", 0, src_c2_ios, roll=False),
                GitCommitEntry("src/android_only", 1, and_2, roll=False),
                GitCommitEntry(f"{ang_p}/active_dep", 2, ac_2, roll=False),
                GitCommitEntry(ang_p, 1, ang_2, roll=True),
                GitCommitEntry("src", 0, src_c3, roll=True),
            ],
        )

    def test_fetch_warnings_and_url_filter(self) -> None:
        ws = self.workspace / "fetch_ws"
        _setup_gclient(ws)
        src_dir, v8_dir = ws / "src", ws / "src/v8"
        upstream_v8 = self.workspace / "upstream_v8"
        v1, v2, v3 = _init_commits(upstream_v8, "v1", "v2", "v3")

        _init_repo(v8_dir)
        _git(v8_dir, "remote", "add", "origin", str(upstream_v8))
        _git(v8_dir, "fetch", "--quiet", "origin", v1)
        _git(v8_dir, "checkout", "--detach", "--quiet", v1)

        m1, m2 = "1" * 40, "2" * 40
        _init_repo(src_dir)
        deps_tpl = (
            "deps = {{\n"
            '  "src/missing": "https://example.com/missing.git@{miss}",\n'
            '  "src/v8": "https://example.com/v8.git@{v8}",\n'
            "}}\n"
        )
        c1 = _commit_file(src_dir, "DEPS", deps_tpl.format(miss=m1, v8=v1), "1")
        c2 = _commit_file(src_dir, "DEPS", deps_tpl.format(miss=m2, v8=v1), "2")
        c3 = _commit_file(src_dir, "DEPS", deps_tpl.format(miss=m2, v8=v3), "3")

        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            unfetched = CommitDepsCollector(src_dir).collect(c1, c3)
        err_text = stderr.getvalue()
        self.assertIn("src/missing", err_text)
        self.assertIn("subrepository checkout not found locally.", err_text)
        self.assertIn("src/v8", err_text)
        self.assertIn("commit not fetched locally.", err_text)
        self.assertEqual(
            unfetched,
            [
                GitCommitEntry("src", 0, c2, roll=True),
                GitCommitEntry("src", 0, c3, roll=True),
            ],
        )

        with contextlib.redirect_stderr(io.StringIO()) as fetch_stderr:
            fetched = CommitDepsCollector(
                src_dir, filters=["https://example.com/v8.git"], fetch=True
            ).collect(c1, c3)
        self.assertEqual(fetch_stderr.getvalue(), "")
        self.assertEqual(
            fetched,
            [
                GitCommitEntry("src", 0, c2, roll=False),
                GitCommitEntry("src/v8", 1, v2, roll=False),
                GitCommitEntry("src/v8", 1, v3, roll=False),
                GitCommitEntry("src", 0, c3, roll=True),
            ],
        )

    def test_worktree_and_symlinked_workspace(self) -> None:
        repos = self._setup_nested_repos()
        wt_dir = self.workspace / "external_wt"
        _git(
            repos.src_dir,
            "worktree",
            "add",
            "--detach",
            "--quiet",
            str(wt_dir),
            repos.src_c4,
        )
        self.assertFalse((wt_dir / "v8/.git").exists())
        wt_collector = CommitDepsCollector(wt_dir)
        self.assertFalse(wt_collector.is_standalone)
        self.assertEqual(wt_collector.display_path, "src")
        self.assertEqual(
            wt_collector.collect(repos.src_c1, repos.src_c4),
            list(repos.entries),
        )

        phys_dir = self.workspace / "phys_src"
        p1, p2 = _init_commits(phys_dir, "p1", "p2")
        sym_ws = self.workspace / "sym_ws"
        _setup_gclient(sym_ws)
        sym_src = sym_ws / "src"
        try:
            sym_src.symlink_to(phys_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            if sys.platform.startswith("win"):
                return
            raise
        sym_collector = CommitDepsCollector(sym_src)
        self.assertFalse(sym_collector.is_standalone)
        self.assertEqual(sym_collector.display_path, "src")
        self.assertEqual(
            sym_collector.collect(p1, p2),
            [GitCommitEntry("src", 0, p2, roll=False)],
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
            with self.subTest(p=non_repo), self.assertRaises(CommitDepsError):
                CommitDepsCollector(non_repo)

    def test_temporal_deps_cipd_gcs_gitlinks_and_roll_revert(self) -> None:
        ws = self.workspace / "temporal_ws"
        _setup_gclient(ws)
        src_dir, v8_dir = ws / "src", ws / "src/v8"
        l1, l2 = _init_commits(v8_dir / "leaf", "l1", "l2")
        sm1, sm2 = _init_commits(src_dir / "submod", "sm1", "sm2")

        _init_repo(v8_dir)
        v1 = _commit_file(v8_dir, "DEPS", _deps_content({"leaf": l1}), "v1")
        v2 = _commit_file(v8_dir, "v.cc", "2\n", "v2")
        v3 = _commit_file(v8_dir, "DEPS", _deps_content({"leaf": l2}), "v3")

        gitlink_diff = scm.DiffTreeEntry("160000", "160000", sm1, sm2, "submod")
        self.assertEqual(
            SubrepoRoll.from_diff_entry(gitlink_diff),
            SubrepoRoll("submod", sm1, sm2),
        )

        _init_repo(src_dir)
        h_a, h_b = "a" * 64, "b" * 64
        c1 = _commit_file(
            src_dir, "DEPS", _temporal_deps(v1, "1" * 40, h_a, True), "c1"
        )
        c2_roll = _commit_file(
            src_dir, "DEPS", _temporal_deps(v3, "1" * 40, h_a, True), "c2"
        )
        c3_drop = _commit_file(
            src_dir, "DEPS", _temporal_deps(v3, "1" * 40, h_a, False), "c3"
        )
        c4_cipd = _commit_file(
            src_dir, "DEPS", _temporal_deps(v3, "2" * 40, h_a, False), "c4"
        )
        c5_gcs = _commit_file(
            src_dir, "DEPS", _temporal_deps(v3, "2" * 40, h_b, False), "c5"
        )
        c6_add_gl = _commit_gitlink(src_dir, "submod", sm1, "c6 add submod")
        c7_roll_gl = _commit_gitlink(src_dir, "submod", sm2, "c7 roll submod")
        _git(src_dir, "rm", "--cached", "--quiet", "submod")
        _git(src_dir, "commit", "--quiet", "-m", "c8 delete submod")
        c8_del_gl = _git(src_dir, "rev-parse", "HEAD")
        c9_revert = _commit_file(
            src_dir, "DEPS", _temporal_deps(v1, "2" * 40, h_b, False), "c9"
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
            CommitDepsCollector(src_dir).collect(c1, c11_non_sha),
            [
                GitCommitEntry("src/v8", 1, v2, roll=False),
                GitCommitEntry("src/v8/leaf", 2, l2, roll=False),
                GitCommitEntry("src/v8", 1, v3, roll=True),
                GitCommitEntry("src", 0, c2_roll, roll=True),
                GitCommitEntry("src", 0, c3_drop, roll=False),
                GitCommitEntry("src", 0, c4_cipd, roll=True),
                GitCommitEntry("src", 0, c5_gcs, roll=True),
                GitCommitEntry("src", 0, c6_add_gl, roll=True),
                GitCommitEntry("src/submod", 1, sm2, roll=False),
                GitCommitEntry("src", 0, c7_roll_gl, roll=True),
                GitCommitEntry("src", 0, c8_del_gl, roll=True),
                GitCommitEntry("src/v8", 1, v2, roll=False),
                GitCommitEntry("src/v8", 1, v1, roll=False),
                GitCommitEntry("src", 0, c9_revert, roll=True),
                GitCommitEntry("src", 0, c10_malformed, roll=False),
                GitCommitEntry("src", 0, c11_non_sha, roll=False),
            ],
        )

    def test_merge_commit_no_duplicate_rolls_and_include_start(self) -> None:
        repo_dir = self.workspace / "merge_repo"
        s1, s2 = _init_commits(repo_dir / "sub", "s1", "s2")

        _init_repo(repo_dir)
        c1 = _commit_file(repo_dir, "DEPS", _deps_content({"sub": s1}), "root")
        _git(repo_dir, "checkout", "-b", "side", "--quiet")
        _commit_file(repo_dir, "DEPS", _deps_content({"sub": s2}), "side roll")
        _git(repo_dir, "checkout", "main", "--quiet")
        c_main = _commit_file(repo_dir, "file.txt", "main\n", "c_main")
        _git(repo_dir, "merge", "--no-ff", "--quiet", "-m", "Merge", "side")
        c_merge = _git(repo_dir, "rev-parse", "HEAD")
        c_after = _commit_file(repo_dir, "file.txt", "after\n", "c_after")

        full = CommitDepsCollector(repo_dir, first_parent=False)
        full_entries = full.collect(c1, c_merge)
        self.assertEqual(
            [e for e in full_entries if e.path == "merge_repo/sub"],
            [GitCommitEntry("merge_repo/sub", 1, s2, roll=False)],
        )
        self.assertEqual(
            [e for e in full_entries if e.commit == c_merge],
            [GitCommitEntry("merge_repo", 0, c_merge, roll=False)],
        )
        self.assertEqual(
            CommitDepsCollector(repo_dir, first_parent=True).collect(
                c1, c_merge
            ),
            [
                GitCommitEntry("merge_repo", 0, c_main, roll=False),
                GitCommitEntry("merge_repo/sub", 1, s2, roll=False),
                GitCommitEntry("merge_repo", 0, c_merge, roll=True),
            ],
        )

        # Single-commit & --include-start on merge/root must not leak ^2 or
        # pass Windows-unsafe '^' revision suffixes to scm.GIT.
        seen_specs: list[str] = []
        orig_resolve, orig_log = (
            scm.GIT.ResolveCommit,
            scm.GIT.GetCommitLogDiffs,
        )

        def spy_resolve(cwd: str, rev: str) -> str | None:
            seen_specs.append(rev)
            return orig_resolve(cwd, rev)

        def spy_log(
            cwd: str,
            commit_range: str,
            *,
            first_parent: bool = False,
            max_count: int | None = None,
            single_commit: bool = False,
        ) -> list[scm.CommitLogEntry]:
            seen_specs.append(commit_range)
            return orig_log(
                cwd,
                commit_range,
                first_parent=first_parent,
                max_count=max_count,
                single_commit=single_commit,
            )

        with (
            mock.patch.object(
                scm.GIT, "ResolveCommit", side_effect=spy_resolve
            ),
            mock.patch.object(
                scm.GIT, "GetCommitLogDiffs", side_effect=spy_log
            ),
        ):
            self.assertEqual(
                full.collect(c_merge),
                [
                    GitCommitEntry("merge_repo/sub", 1, s2, roll=False),
                    GitCommitEntry("merge_repo", 0, c_merge, roll=True),
                ],
            )
            inc_merge = full.collect(c_merge, c_after, include_start=True)
            self.assertEqual(
                full.collect(c1, c_main, include_start=True),
                [
                    GitCommitEntry("merge_repo", 0, c1, roll=True),
                    GitCommitEntry("merge_repo", 0, c_main, roll=False),
                ],
            )
        self.assertEqual(
            [e.commit for e in inc_merge if e.level == 0], [c_merge, c_after]
        )
        self.assertFalse(any("^" in spec for spec in seen_specs))

    def test_ranges_empty_merge_base_and_max_count(self) -> None:
        c1, c3, c4 = "a" * 40, "b" * 40, "c" * 40
        cases = [
            ((f"{c1}..{c4}",), CommitRange(c1, c4, True, False)),
            ((f"{c1}..{c1}",), CommitRange(c1, c1, True, False)),
            ((f"{c1}..",), CommitRange(c1, "HEAD", True, False)),
            ((f"..{c4}",), CommitRange("HEAD", c4, True, False)),
            ((f"{c1}...{c4}",), CommitRange(c1, c4, True, True)),
            ((f"{c3}^!",), CommitRange(c3, c3, False, False)),
            ((c3,), CommitRange(c3, c3, False, False)),
            ((c1, c4), CommitRange(c1, c4, True, False)),
        ]
        for cli_args, expected in cases:
            with self.subTest(cli_args=cli_args):
                resolved = self._resolve_cli_range(*cli_args)
                self.assertEqual(resolved, expected)
                self.assertEqual(
                    resolved.is_single_commit, not expected.is_range
                )

        for bad in ([".."], ["..."], [], [c1, c3, c4]):
            with self.subTest(bad=bad), self.assertRaises(CommitDepsError):
                if len(bad) == 1 and "." in bad[0]:
                    CommitRange.parse(bad[0])
                else:
                    CommitRange.from_args(bad)

        repo_dir = self.workspace / "range_repo"
        r1, r2 = _init_commits(repo_dir, "r1", "r2")
        _git(repo_dir, "checkout", "-b", "div", "--quiet")
        r_div = _commit_file(repo_dir, "div.txt", "d\n", "r_div")
        _git(repo_dir, "checkout", "main", "--quiet")
        r3 = _commit_file(repo_dir, "f.txt", "3\n", "r3")
        r4 = _commit_file(repo_dir, "f.txt", "4\n", "r4")
        _git(repo_dir, "update-ref", "refs/remotes/origin/main", r4)

        self.assertEqual(self._run_cli_commits(repo_dir, f"{r4}..{r4}"), [])
        self.assertEqual(
            self._run_cli_commits(repo_dir, "origin/main..HEAD"), []
        )
        self.assertEqual(self._run_cli_commits(repo_dir, r4), [r4])
        self.assertEqual(
            self._run_cli_commits(repo_dir, f"{r_div}...{r4}"), [r3, r4]
        )
        self.assertEqual(
            self._run_cli_commits(
                repo_dir, "--include-start", f"{r_div}...{r4}"
            ),
            [r2, r3, r4],
        )
        self.assertEqual(
            self._run_cli_commits(repo_dir, f"{r1}..{r4}", "-n", "2"), [r3, r4]
        )
        self.assertEqual(self._run_cli_commits(repo_dir, "-n", "2"), [r3, r4])
        self.assertEqual(self._run_cli_commits(repo_dir, "-n", "1"), [r4])
        self.assertEqual(
            self._run_cli_commits(repo_dir, r3, "--max-count=2"), [r2, r3]
        )

    def test_commit_position_and_version_tag_hint(self) -> None:
        repo_dir = self.workspace / "cr_pos_repo"
        _init_repo(repo_dir)
        shas = [
            _commit_file(
                repo_dir,
                "a.txt",
                f"{n}\n",
                f"pos {n}\n\nCr-Commit-Position: refs/heads/main@{{#{n}}}",
            )
            for n in (100, 101, 102, 100001, 100002)
        ]
        _, p101, p102, _, p100002 = shas

        collector = CommitDepsCollector(repo_dir)
        pos_entries = collector.collect(
            "refs/heads/main@{#100}", "refs/heads/main@{#102}"
        )
        self.assertEqual([e.commit for e in pos_entries], [p101, p102])
        self.assertEqual(
            [e.commit_position for e in pos_entries],
            ["refs/heads/main@{#101}", "refs/heads/main@{#102}"],
        )
        self.assertEqual(
            [e.commit for e in collector.collect("100001", "100002")], [p100002]
        )
        self.assertEqual(
            self._run_cli_commits(
                repo_dir, "Cr-Commit-Position: refs/heads/main@{#100002}"
            ),
            [p100002],
        )

        for missing in ("refs/heads/main@{#999999}", "@{#999999}"):
            with self.assertRaises(CommitDepsError):
                collector.collect("HEAD~1", missing)

        with self.assertRaises(CommitDepsError) as ctx:
            collector.collect("154.0.1.2", "HEAD")
        self.assertIn("154.0.1.2", str(ctx.exception))
        self.assertIn("tag", str(ctx.exception))

    def test_cyclic_deps_rolls(self) -> None:
        repo_a = self.workspace / "cycle_a"
        repo_b = repo_a / "sub_b"
        _init_repo(repo_a)
        _init_repo(repo_b)

        a_c0 = _commit_file(repo_a, "a.txt", "0\n", "a c0")
        _git(repo_a, "checkout", "-b", "placeholder", "--quiet")
        a_c2 = _commit_file(repo_a, "a.txt", "2\n", "a c2 placeholder")
        _git(repo_a, "checkout", "main", "--quiet")

        b_c1 = _commit_file(
            repo_b, "DEPS", _deps_content({"..": a_c0}, [".."]), "b1"
        )
        b_c2 = _commit_file(
            repo_b, "DEPS", _deps_content({"..": a_c2}, [".."]), "b2"
        )
        a_c1 = _commit_file(
            repo_a, "DEPS", _deps_content({"sub_b": b_c1}, ["sub_b"]), "a1"
        )
        a_c3 = _commit_file(
            repo_a, "DEPS", _deps_content({"sub_b": b_c2}, ["sub_b"]), "a3"
        )
        _git(repo_a, "replace", a_c2, a_c3)

        self.assertEqual(
            CommitDepsCollector(repo_a).collect(a_c0, a_c2),
            [
                GitCommitEntry("cycle_a", 0, a_c1, roll=True),
                GitCommitEntry("cycle_a/sub_b", 1, b_c2, roll=True),
                GitCommitEntry("cycle_a", 0, a_c2, roll=True),
            ],
        )

    def test_formats_table_csv_json(self) -> None:
        long_subject = "S" * 80
        sub_p = "src/v8/third_party/sub"
        entries = [
            GitCommitEntry(
                "src", 0, "a" * 40, True, "Roll V8", "refs/heads/main@{#10}"
            ),
            GitCommitEntry("src/v8", 1, "b" * 40, True, long_subject),
            GitCommitEntry(sub_p, 2, "c" * 40, False, "Sub"),
            GitCommitEntry("src", 0, "d" * 40, False, "Fix"),
        ]
        expected_pinned_shas = [
            {"src": "a" * 40},
            {"src": "a" * 40, "src/v8": "b" * 40},
            {"src": "a" * 40, "src/v8": "b" * 40, sub_p: "c" * 40},
            {"src": "d" * 40},
        ]
        expected_cmds = [
            f"gclient sync --revision src@{'a' * 40}",
            (
                f"gclient sync --revision src@{'a' * 40} "
                f"--revision src/v8@{'b' * 40}"
            ),
            (
                f"gclient sync --revision src@{'a' * 40} "
                f"--revision src/v8@{'b' * 40} --revision {sub_p}@{'c' * 40}"
            ),
            f"gclient sync --revision src@{'d' * 40}",
        ]
        self.assertEqual(
            GitCommitEntry.build_pinned_shas(entries), expected_pinned_shas
        )
        self.assertEqual(
            GitCommitEntry.build_sync_commands(entries), expected_cmds
        )
        self.assertEqual(
            GitCommitEntry.build_sync_commands(entries, standalone=True),
            [f"git checkout --detach {e.commit}" for e in entries],
        )

        for sync_command in (False, True):
            with self.subTest(sync_command=sync_command):
                table_out = OutputFormat.TABLE.format_entries(
                    entries, sync_command=sync_command
                )
                self.assertIn("| Path", table_out)
                self.assertIn("subject", table_out)
                self.assertIn("S" * 69 + "...", table_out)
                self.assertNotIn(long_subject, table_out)
                if sync_command:
                    self.assertIn("sync command", table_out)
                    for cmd in expected_cmds:
                        self.assertIn(cmd, table_out)

                csv_text = OutputFormat.CSV.format_entries(
                    entries, sync_command=sync_command
                )
                csv_rows = list(csv.reader(io.StringIO(csv_text)))
                expected_headers = [
                    "Path",
                    "Level",
                    "commit hash",
                    "roll",
                    "subject",
                    *(["sync command"] if sync_command else []),
                ]
                self.assertEqual(csv_rows[0], expected_headers)
                self.assertEqual(csv_rows[2][4], long_subject)
                if sync_command:
                    self.assertEqual(
                        [r[5] for r in csv_rows[1:]], expected_cmds
                    )

                json_items = json.loads(
                    OutputFormat.JSON.format_entries(
                        entries, sync_command=sync_command
                    )
                )
                expected_json = [
                    {
                        "path": entry.path,
                        "level": entry.level,
                        "commit": entry.commit,
                        "roll": entry.roll,
                        "subject": entry.subject,
                        "commit_position": entry.commit_position,
                        "pinned_shas": shas,
                        **({"sync_command": cmd} if sync_command else {}),
                    }
                    for entry, shas, cmd in zip(
                        entries, expected_pinned_shas, expected_cmds
                    )
                ]
                self.assertEqual(json_items, expected_json)

    def test_main_cli_and_standalone_sync(self) -> None:
        repos = self._setup_nested_repos()
        code, out, err = self._run_main(
            "--repo", str(repos.src_dir), "--format=json", repos.src_c3_roll
        )
        self.assertEqual((code, err), (0, ""))
        json_plain = json.loads(out)
        self.assertEqual(
            [item["commit"] for item in json_plain],
            [e.commit for e in repos.roll_entries],
        )
        self.assertTrue(all("pinned_shas" in item for item in json_plain))
        self.assertTrue(all("subject" in item for item in json_plain))
        self.assertTrue(all("sync_command" not in item for item in json_plain))

        # Attached branch on .gclient repo warns on stderr with --sync-command.
        code, out, err = self._run_main(
            "--repo",
            str(repos.src_dir),
            "--format=json",
            "--sync-command",
            repos.src_c3_roll,
        )
        self.assertEqual(code, 0)
        self.assertIn("git checkout --detach", err)
        self.assertIn(
            "gclient sync --revision", json.loads(out)[0]["sync_command"]
        )

        # Detached HEAD on .gclient repo emits no branch warning.
        _git(repos.src_dir, "checkout", "--detach", "--quiet")
        code, _, err = self._run_main(
            "--repo", str(repos.src_dir), "--sync-command", repos.src_c4
        )
        self.assertEqual((code, err), (0, ""))

        # Standalone non-.gclient repo emits `git checkout --detach <sha>`.
        solo_dir = self.workspace / "standalone_repo"
        (solo_c1,) = _init_commits(solo_dir, "solo 1")
        code, out, err = self._run_main(
            "--repo", str(solo_dir), "--format=json", "--sync-command", solo_c1
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            json.loads(out)[0]["sync_command"],
            f"git checkout --detach {solo_c1}",
        )

        with mock.patch.object(gclient_utils, "IsEnvCog", return_value=True):
            cog_code, _, cog_err = self._run_main("HEAD")
        self.assertEqual(cog_code, 1)
        self.assertIn("not supported in non-git environments", cog_err)

        err_code, _, err_text = self._run_main("--repo", str(repos.src_dir))
        self.assertEqual(err_code, 1)
        self.assertIn("Error:", err_text)

        # TTY pager is skipped when TERM=dumb, GIT_PAGER=cat, or entries=[].
        pager_buf = io.BytesIO()
        less_cm = contextlib.nullcontext(pager_buf)
        with (
            mock.patch.object(setup_color, "init"),
            mock.patch.object(setup_color, "IS_TTY", True),
            mock.patch.object(shutil, "which", return_value="less"),
            mock.patch.object(
                git_common, "less", return_value=less_cm
            ) as mock_less,
        ):
            for env_ovr, rev in (
                ({"TERM": "dumb", "GIT_PAGER": "less"}, repos.src_c4),
                ({"TERM": "xterm", "GIT_PAGER": "cat"}, repos.src_c4),
                ({"TERM": "xterm", "GIT_PAGER": "less"}, "HEAD..HEAD"),
            ):
                with mock.patch.dict(os.environ, env_ovr):
                    p_code, _, _ = self._run_main(
                        "--repo", str(repos.src_dir), rev
                    )
                self.assertEqual(p_code, 0)
            mock_less.assert_not_called()
            with mock.patch.dict(
                os.environ, {"TERM": "xterm", "GIT_PAGER": "less"}
            ):
                p_code, _, _ = self._run_main(
                    "--repo", str(repos.src_dir), repos.src_c4
                )
            self.assertEqual(p_code, 0)
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
        ws = self.workspace / "nested_ws"
        _setup_gclient(ws)
        src_dir, v8_dir = ws / "src", ws / "src/v8"
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

        sub_p = "src/v8/third_party/sub"
        roll_entries = (
            GitCommitEntry("src/v8", 1, v8_c2, False, "v8 regular work"),
            GitCommitEntry(sub_p, 2, sub_c2, False, "sub c2"),
            GitCommitEntry(sub_p, 2, sub_c3, False, "sub c3"),
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
        code, out, _ = self._run_main(
            "--repo", str(repo_dir), "--format=json", *cli_args
        )
        self.assertEqual(code, 0)
        return [item["commit"] for item in json.loads(out)]

    def _run_main(self, *cli_args: str) -> tuple[int, str, str]:
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            code = commit_deps.main(list(cli_args))
        return code, out.getvalue(), err.getvalue()


def _setup_gclient(ws: Path) -> None:
    _write_file(
        ws / ".gclient",
        'solutions = [{"name": "src", "url": "https://example.com/src.git"}]\n',
    )
    _write_file(
        ws / ".gclient_entries",
        'entries = {"src": "https://example.com/src.git"}\n',
    )


def _init_commits(repo_dir: Path, *msgs: str) -> list[str]:
    _init_repo(repo_dir)
    return [
        _commit_file(repo_dir, "f.txt", f"{i}\n", m) for i, m in enumerate(msgs)
    ]


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main", "--quiet")


def _commit_file(repo_dir: Path, rel_path: str, content: str, msg: str) -> str:
    _write_file(repo_dir / rel_path, content)
    _git(repo_dir, "add", rel_path)
    _git(repo_dir, "commit", "--quiet", "-m", msg)
    return _git(repo_dir, "rev-parse", "HEAD")


def _deps_content(
    deps_map: dict[str, str], recursedeps: list[str] | None = None
) -> str:
    entries = "\n".join(
        f'  "{path}": "https://example.com/repo.git@{sha}",'
        for path, sha in deps_map.items()
    )
    rec = f"recursedeps = {json.dumps(recursedeps)}\n" if recursedeps else ""
    return f"use_relative_paths = True\ndeps = {{\n{entries}\n}}\n{rec}"


def _commit_gitlink(
    repo_dir: Path, rel_path: str, target_sha: str, msg: str
) -> str:
    _stage_gitlink(repo_dir, rel_path, target_sha)
    _git(repo_dir, "commit", "--quiet", "-m", msg)
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
    rec = 'recursedeps = ["src/v8"]\n' if recurse else ""
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
        f"{rec}"
    )


def _src_deps(v8_sha: str) -> str:
    return (
        f'vars = {{"v8_git": "{v8_sha}"}}\n'
        'deps = {"src/v8": "https://example.com/v8.git@" + Var("v8_git")}\n'
        'recursedeps = ["src/v8"]\n'
    )


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(repo_dir: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.check_output(
        [git_common.GIT_EXE, "-C", str(repo_dir), *args],
        env=env,
        shell=False,
        stderr=subprocess.STDOUT,
        text=True,
    ).strip()


if __name__ == "__main__":
    unittest.main()
