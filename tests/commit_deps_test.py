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
import subprocess
import sys
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import commit_deps  # noqa: E402
from commit_deps import (  # noqa: E402
    CommitDepsCollector,
    CommitDepsError,
    CommitEntry,
    InputParser,
    OutputFormat,
    RevisionRange,
    SubrepoRoll,
)
import scm  # noqa: E402
from testing_support import trial_dir  # noqa: E402


@dataclasses.dataclass(frozen=True)
class _NestedRepos:
    src_dir: Path
    src_c1: str
    src_c2: str
    src_c3_roll: str
    src_c4: str
    entries: tuple[CommitEntry, ...]
    roll_entries: tuple[CommitEntry, ...]


class CommitDepsTest(trial_dir.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = Path(self.root_dir)

    def test_recursive_deps_expansion(self) -> None:
        repos = self._setup_nested_repos()
        collector = CommitDepsCollector(repos.src_dir)
        self.assertEqual(collector.display_path, "src")
        self.assertEqual(
            collector.collect(repos.src_c1, repos.src_c4),
            list(repos.entries),
        )
        self.assertEqual(
            collector.collect(repos.src_c2, repos.src_c4, include_start=True),
            list(repos.entries),
        )
        self.assertEqual(
            collector.collect(repos.src_c1, repos.src_c2, include_start=True),
            [
                CommitEntry("src", 0, repos.src_c1, roll=False),
                CommitEntry("src", 0, repos.src_c2, roll=False),
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

    def test_repo_resolution(self) -> None:
        repos = self._setup_nested_repos()
        collector = CommitDepsCollector(".", cwd=repos.src_dir)
        self.assertEqual(collector.repo_dir, repos.src_dir.resolve())
        self.assertEqual(collector.display_path, "src")

        subdir = repos.src_dir / "nested" / "dir"
        subdir.mkdir(parents=True)
        sub_collector = CommitDepsCollector(subdir)
        self.assertEqual(sub_collector.repo_dir, repos.src_dir.resolve())

        with self.assertRaises(CommitDepsError):
            collector.collect("deadbeef" * 5, repos.src_c4)

        for non_repo in (self.workspace, self.workspace / "missing"):
            with (
                self.subTest(non_repo=non_repo),
                self.assertRaises(CommitDepsError),
            ):
                CommitDepsCollector(non_repo)

    def test_gitlink_roll_detection(self) -> None:
        src_dir = self.workspace / "repo"
        sub_dir = src_dir / "submod"
        _init_repo(sub_dir)
        sub_c1 = _commit_file(sub_dir, "a.txt", "1\n", "c1")
        sub_c2 = _commit_file(sub_dir, "a.txt", "2\n", "c2")
        sub_c3 = _commit_file(sub_dir, "a.txt", "3\n", "c3")

        gitlink_diff = scm.DiffTreeEntry(
            "160000", "160000", sub_c1, sub_c2, "submod"
        )
        self.assertEqual(
            SubrepoRoll.from_gitlink(gitlink_diff),
            SubrepoRoll("submod", sub_c1, sub_c2),
        )

        other_dir = src_dir / "other"
        _init_repo(other_dir)
        other_c1 = _commit_file(other_dir, "b.txt", "1\n", "b1")
        other_c2 = _commit_file(other_dir, "b.txt", "2\n", "b2")

        _init_repo(src_dir)
        repo_c1 = _commit_gitlink(src_dir, "submod", sub_c1, "Add submod at c1")
        repo_c2 = _commit_gitlink(
            src_dir, "submod", sub_c2, "Roll submod to c2"
        )

        collector = CommitDepsCollector(src_dir)
        self.assertEqual(
            collector.collect(repo_c1, repo_c2),
            [
                CommitEntry("repo", 0, repo_c2, roll=True),
                CommitEntry("repo/submod", 1, sub_c2, roll=False),
            ],
        )

        # Same-commit DEPS + gitlink roll merging (gitlink takes precedence on
        # "submod", while DEPS-only "other" roll is also preserved in order).
        repo_c3 = _commit_file(
            src_dir,
            "DEPS",
            _deps_content({"other": other_c1, "submod": sub_c1}),
            "Add DEPS",
        )
        _write_file(
            src_dir / "DEPS",
            _deps_content({"other": other_c2, "submod": sub_c2}),
        )
        _git(src_dir, "add", "DEPS")
        repo_c4 = _commit_gitlink(
            src_dir, "submod", sub_c3, "Roll DEPS and gitlink together"
        )
        self.assertEqual(
            collector.collect(repo_c3, repo_c4),
            [
                CommitEntry("repo", 0, repo_c4, roll=True),
                CommitEntry("repo/other", 1, other_c2, roll=False),
                CommitEntry("repo/submod", 1, sub_c3, roll=False),
            ],
        )

    def test_deps_cipd_filter_and_missing_subrepo(self) -> None:
        src_dir = self.workspace / "cipd_repo"
        _init_repo(src_dir)
        sha_1, sha_2 = "1" * 40, "2" * 40
        c1 = _commit_file(
            src_dir, "DEPS", _cipd_deps(sha_1, sha_1), "Init DEPS"
        )
        c2_cipd_only = _commit_file(
            src_dir,
            "DEPS",
            _cipd_deps(sha_2, sha_1),
            "Roll CIPD git_revision only",
        )
        c3_missing_sub = _commit_file(
            src_dir, "DEPS", _cipd_deps(sha_2, sha_2), "Roll missing subrepo"
        )
        c4_malformed = _commit_file(
            src_dir,
            "DEPS",
            f"deps = {{\n  broken syntax {'3' * 40}\n",
            "Malformed DEPS with 40-char SHA",
        )
        c5_non_sha_and_dot = _commit_file(
            src_dir,
            "DEPS",
            (
                "deps = {\n"
                f'  ".": "https://example.com/root.git@{"4" * 40}",\n'
                '  "src/ref": "https://example.com/ref.git@refs/heads/main",\n'
                "}\n"
            ),
            "DEPS with dot path and non-SHA ref",
        )

        collector = CommitDepsCollector(src_dir)
        self.assertEqual(
            collector.collect(c1, c5_non_sha_and_dot),
            [
                CommitEntry("cipd_repo", 0, c2_cipd_only, roll=False),
                CommitEntry("cipd_repo", 0, c3_missing_sub, roll=True),
                CommitEntry("cipd_repo", 0, c4_malformed, roll=False),
                CommitEntry("cipd_repo", 0, c5_non_sha_and_dot, roll=False),
            ],
        )

    def test_first_parent_and_merge_commit(self) -> None:
        repo_dir = self.workspace / "merge_repo"
        _init_repo(repo_dir)
        c1 = _commit_file(repo_dir, "file.txt", "base\n", "c1")
        _git(repo_dir, "checkout", "-b", "side", "--quiet")
        c_side = _commit_file(repo_dir, "side.txt", "side\n", "c_side")
        _git(repo_dir, "checkout", "-", "--quiet")
        c_main = _commit_file(repo_dir, "file.txt", "main\n", "c_main")
        _git(
            repo_dir,
            "merge",
            "--no-ff",
            "--quiet",
            "-m",
            "Merge side",
            "side",
        )
        c_merge = _git(repo_dir, "rev-parse", "HEAD")

        full_collector = CommitDepsCollector(repo_dir, first_parent=False)
        full_entries = full_collector.collect(c1, c_merge)
        self.assertEqual(
            {entry.commit for entry in full_entries},
            {c_main, c_side, c_merge},
        )
        # Single-commit lookup on a merge commit must use ^! so side-branch
        # commits do not leak even when first_parent=False.
        self.assertEqual(
            full_collector.collect(c_merge, c_merge),
            [CommitEntry("merge_repo", 0, c_merge, roll=False)],
        )

        fp_collector = CommitDepsCollector(repo_dir, first_parent=True)
        self.assertEqual(
            fp_collector.collect(c1, c_merge),
            [
                CommitEntry("merge_repo", 0, c_main, roll=False),
                CommitEntry("merge_repo", 0, c_merge, roll=False),
            ],
        )

    def test_cyclic_deps_rolls(self) -> None:
        repo_a = self.workspace / "cycle_a"
        repo_b = repo_a / "sub_b"
        _init_repo(repo_a)
        _init_repo(repo_b)

        a_c0 = _commit_file(repo_a, "a.txt", "0\n", "a c0")
        _git(repo_a, "checkout", "-b", "placeholder", "--quiet")
        a_c2 = _commit_file(repo_a, "a.txt", "2\n", "a c2 placeholder")
        _git(repo_a, "checkout", "-", "--quiet")

        b_c1 = _commit_file(
            repo_b,
            "DEPS",
            _deps_content({"..": a_c0}),
            "b init DEPS pointing to parent",
        )
        b_c2 = _commit_file(
            repo_b,
            "DEPS",
            _deps_content({"..": a_c2}),
            "b roll parent a_c0..a_c2",
        )
        a_c1 = _commit_file(
            repo_a, "DEPS", _deps_content({"sub_b": b_c1}), "a init DEPS"
        )
        a_c3 = _commit_file(
            repo_a,
            "DEPS",
            _deps_content({"sub_b": b_c2}),
            "a roll sub_b b_c1..b_c2",
        )
        _git(repo_a, "replace", a_c2, a_c3)

        collector = CommitDepsCollector(repo_a)
        self.assertEqual(
            collector.collect(a_c0, a_c2),
            [
                CommitEntry("cycle_a", 0, a_c1, roll=False),
                CommitEntry("cycle_a", 0, a_c2, roll=True),
                CommitEntry("cycle_a/sub_b", 1, b_c2, roll=True),
            ],
        )

    def test_formats_table_csv_json(self) -> None:
        entries = [
            CommitEntry("src", 0, "a" * 40, roll=True),
            CommitEntry("src/v8", 1, "b" * 40, roll=True),
            CommitEntry("src/v8/third_party/sub", 2, "c" * 40, roll=False),
            CommitEntry("src", 0, "d" * 40, roll=False),
        ]
        expected_cmds = [
            f"gclient sync --revision src@{'a' * 40}",
            (
                f"gclient sync --revision src@{'a' * 40} "
                f"--revision src/v8@{'b' * 40}"
            ),
            (
                f"gclient sync --revision src@{'a' * 40} "
                f"--revision src/v8@{'b' * 40} "
                f"--revision src/v8/third_party/sub@{'c' * 40}"
            ),
            f"gclient sync --revision src@{'d' * 40}",
        ]
        self.assertEqual(
            CommitEntry.build_sync_commands(entries),
            expected_cmds,
        )

        for sync_command in (False, True):
            with self.subTest(sync_command=sync_command):
                table_out = OutputFormat.TABLE.format_entries(
                    entries, sync_command=sync_command
                )
                self.assertIn("| Path", table_out)
                self.assertIn(
                    f"| src/v8                 | 1     | {'b' * 40} | true  |",
                    table_out,
                )
                if sync_command:
                    self.assertIn("sync command", table_out)
                    for cmd in expected_cmds:
                        self.assertIn(cmd, table_out)

                csv_rows = list(
                    csv.reader(
                        io.StringIO(
                            OutputFormat.CSV.format_entries(
                                entries, sync_command=sync_command
                            )
                        )
                    )
                )
                expected_headers = [
                    "Path",
                    "Level",
                    "commit hash",
                    "roll",
                    *(["sync command"] if sync_command else []),
                ]
                self.assertEqual(csv_rows[0], expected_headers)
                self.assertEqual(len(csv_rows), 5)
                if sync_command:
                    self.assertEqual(
                        [r[4] for r in csv_rows[1:]], expected_cmds
                    )

                json_items = json.loads(
                    OutputFormat.JSON.format_entries(
                        entries, sync_command=sync_command
                    )
                )
                expected_json = [
                    {
                        **dataclasses.asdict(entry),
                        **({"sync_command": cmd} if sync_command else {}),
                    }
                    for entry, cmd in zip(entries, expected_cmds)
                ]
                self.assertEqual(json_items, expected_json)

    def test_positional_git_log_ranges(self) -> None:
        c1, c3, c4 = "a" * 40, "b" * 40, "c" * 40

        cases = [
            ((f"{c1}..{c4}",), RevisionRange(c1, c4)),
            ((f"{c1}..",), RevisionRange(c1, "HEAD")),
            ((f"..{c4}",), RevisionRange("HEAD", c4)),
            ((f"{c1}...{c4}",), RevisionRange(c1, c4)),
            ((f"{c3}^!",), RevisionRange(c3, c3)),
            ((c3,), RevisionRange(c3, c3)),
            ((c1, c4), RevisionRange(c1, c4)),
        ]
        for cli_args, expected in cases:
            with self.subTest(cli_args=cli_args):
                resolved = self._resolve_cli_range(*cli_args)
                self.assertEqual(resolved, expected)
                self.assertEqual(
                    resolved.is_single_commit, expected.start == expected.end
                )

        for revs in ([], [c1, c3, c4]):
            with self.subTest(revs=revs), self.assertRaises(CommitDepsError):
                RevisionRange.from_args(revs)

    def test_main_cli(self) -> None:
        repos = self._setup_nested_repos()

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            exit_code = commit_deps.main(
                [
                    "--repo",
                    str(repos.src_dir),
                    "--format=json",
                    repos.src_c3_roll,
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            [dataclasses.asdict(entry) for entry in repos.roll_entries],
        )

        with contextlib.redirect_stdout(io.StringIO()) as stdout_sync:
            sync_exit_code = commit_deps.main(
                [
                    "--repo",
                    str(repos.src_dir),
                    "--format=json",
                    "--sync-command",
                    repos.src_c3_roll,
                ]
            )
        self.assertEqual(sync_exit_code, 0)
        json_sync = json.loads(stdout_sync.getvalue())
        self.assertEqual(
            [item["sync_command"] for item in json_sync],
            CommitEntry.build_sync_commands(repos.roll_entries),
        )

        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            error_exit_code = commit_deps.main(["--repo", str(repos.src_dir)])
        self.assertEqual(error_exit_code, 1)
        self.assertIn("Error:", stderr.getvalue())

    def test_parse_args_early_validation(self) -> None:
        args = InputParser.parse_args(
            [
                "  HEAD~2..HEAD  ",
                "--repo=  ./src  ",
                "--format=JSON",
                "--include-start",
                "--first-parent",
                "--max-level=0",
                "--sync-command",
            ]
        )
        self.assertEqual(args.revisions, ["HEAD~2..HEAD"])
        self.assertEqual(args.repo, Path("./src"))
        self.assertEqual(args.format, OutputFormat.JSON)
        self.assertTrue(args.include_start)
        self.assertTrue(args.first_parent)
        self.assertEqual(args.max_level, 0)
        self.assertTrue(args.sync_command)

        alias_args = InputParser.parse_args(["HEAD", "--sync-commands"])
        self.assertTrue(alias_args.sync_command)

        invalid_arg_lists = [
            [""],
            ["   "],
            ["a b"],
            ["a\x00b"],
            ["--", "-bad"],
            ["--repo="],
            ["--repo=   "],
            ["--repo=-bad"],
            ["--max-level=-1"],
            ["--max-level=abc"],
            ["--format=xml"],
            ["--format="],
        ]
        for bad_argv in invalid_arg_lists:
            self._assert_invalid_cli_args(bad_argv)

    def _setup_nested_repos(self) -> _NestedRepos:
        _write_file(
            self.workspace / ".gclient",
            'solutions = [{"name": "src", '
            '"url": "https://example.com/src.git"}]\n',
        )
        _write_file(
            self.workspace / ".gclient_entries",
            'entries = {"src": "https://example.com/src.git"}\n',
        )

        src_dir = self.workspace / "src"
        v8_dir = src_dir / "v8"
        sub_dir = v8_dir / "third_party" / "sub"

        _init_repo(sub_dir)
        sub_c1 = _commit_file(sub_dir, "sub.cc", "int s = 1;\n", "sub c1")
        sub_c2 = _commit_file(sub_dir, "sub.cc", "int s = 2;\n", "sub c2")
        sub_c3 = _commit_file(sub_dir, "sub.cc", "int s = 3;\n", "sub c3")

        _init_repo(v8_dir)
        v8_c1 = _commit_file(
            v8_dir,
            "DEPS",
            _deps_content({"third_party/sub": sub_c1}),
            "v8 init DEPS",
        )
        v8_c2 = _commit_file(v8_dir, "v8.cc", "int v = 1;\n", "v8 regular work")
        v8_c3_roll = _commit_file(
            v8_dir,
            "DEPS",
            _deps_content({"third_party/sub": sub_c3}),
            "Roll sub c1..c3",
        )

        _init_repo(src_dir)
        src_c1 = _commit_file(
            src_dir, "DEPS", _src_deps(v8_c1), "src init DEPS"
        )
        src_c2 = _commit_file(src_dir, "main.cc", "int m = 1;\n", "src change")
        src_c3_roll = _commit_file(
            src_dir,
            "DEPS",
            _src_deps(v8_c3_roll),
            "Roll V8 v8_c1..v8_c3",
        )
        src_c4 = _commit_file(src_dir, "main.cc", "int m = 2;\n", "src after")

        roll_entries = (
            CommitEntry("src", 0, src_c3_roll, roll=True),
            CommitEntry("src/v8", 1, v8_c2, roll=False),
            CommitEntry("src/v8", 1, v8_c3_roll, roll=True),
            CommitEntry("src/v8/third_party/sub", 2, sub_c2, roll=False),
            CommitEntry("src/v8/third_party/sub", 2, sub_c3, roll=False),
        )
        entries = (
            CommitEntry("src", 0, src_c2, roll=False),
            *roll_entries,
            CommitEntry("src", 0, src_c4, roll=False),
        )
        return _NestedRepos(
            src_dir=src_dir,
            src_c1=src_c1,
            src_c2=src_c2,
            src_c3_roll=src_c3_roll,
            src_c4=src_c4,
            entries=entries,
            roll_entries=roll_entries,
        )

    def _resolve_cli_range(self, *cli_args: str) -> RevisionRange:
        args = InputParser.parse_args(list(cli_args))
        return RevisionRange.from_args(args.revisions)

    def _assert_invalid_cli_args(self, bad_argv: list[str]) -> None:
        with (
            self.subTest(argv=bad_argv),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            InputParser.parse_args(bad_argv)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")


def _commit_file(repo_dir: Path, rel_path: str, content: str, msg: str) -> str:
    _write_file(repo_dir / rel_path, content)
    _git(repo_dir, "add", rel_path)
    _git(repo_dir, "commit", "--quiet", "-m", msg)
    return _git(repo_dir, "rev-parse", "HEAD")


def _commit_gitlink(
    repo_dir: Path, rel_path: str, target_sha: str, msg: str
) -> str:
    _git(
        repo_dir,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{target_sha},{rel_path}",
    )
    _git(repo_dir, "commit", "--quiet", "-m", msg)
    return _git(repo_dir, "rev-parse", "HEAD")


def _deps_content(deps_map: dict[str, str]) -> str:
    entries = "\n".join(
        f'  "{path}": "https://example.com/repo.git@{rev}",'
        for path, rev in deps_map.items()
    )
    return f"use_relative_paths = True\ndeps = {{\n{entries}\n}}\n"


def _cipd_deps(cipd_rev: str, sub_rev: str) -> str:
    return (
        "deps = {\n"
        '  "src/cipd_pkg": {\n'
        '    "packages": [\n'
        f'      {{"package": "pkg", "version": "git_revision:{cipd_rev}"}},\n'
        "    ],\n"
        '    "dep_type": "cipd",\n'
        "  },\n"
        f'  "src/missing": "https://example.com/missing.git@{sub_rev}",\n'
        "}\n"
    )


def _src_deps(v8_rev: str) -> str:
    return (
        "vars = {\n"
        f'  "v8_revision": "{v8_rev}",\n'
        "}\n"
        "deps = {\n"
        '  "src/v8": "https://example.com/v8.git" + "@"\n'
        '  + Var("v8_revision"),\n'
        "}\n"
    )


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(cwd: Path | str, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "developer@chromium.org",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "developer@chromium.org",
    }
    return subprocess.check_output(
        ["git", "-C", str(cwd), *args],
        env=env,
        shell=sys.platform.startswith("win"),
        stderr=subprocess.STDOUT,
        text=True,
    ).strip()


if __name__ == "__main__":
    unittest.main()
