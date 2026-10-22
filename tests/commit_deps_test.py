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
from testing_support import trial_dir  # noqa: E402


@dataclasses.dataclass(frozen=True)
class _NestedRepos:
    src_dir: Path
    src: tuple[str, str, str, str]
    entries: tuple[commit_deps.CommitEntry, ...]


class CommitDepsTest(trial_dir.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = Path(self.root_dir)

    def test_recursive_deps_expansion(self) -> None:
        repos = self._setup_nested_repos()
        src_c1, _src_c2, _src_c3_roll, src_c4 = repos.src

        collector = commit_deps.CommitDepsCollector(
            repos.src_dir, allow_fetch=False
        )
        self.assertEqual(collector.display_path, "src")
        self.assertEqual(
            collector.collect(src_c1, src_c4),
            list(repos.entries),
        )

    def test_repo_resolution(self) -> None:
        repos = self._setup_nested_repos()
        collector = commit_deps.CommitDepsCollector(
            ".", cwd=repos.src_dir, allow_fetch=False
        )
        self.assertEqual(collector.repo_dir, repos.src_dir.resolve())
        self.assertEqual(collector.display_path, "src")

        for non_repo in (self.workspace, self.workspace / "missing"):
            with self.assertRaises(commit_deps.CommitDepsError):
                commit_deps.CommitDepsCollector(non_repo, allow_fetch=False)

    def test_gitlink_roll_detection(self) -> None:
        src_dir = self.workspace / "repo"
        sub_dir = src_dir / "submod"
        _init_repo(sub_dir)
        sub_c1 = _commit_file(sub_dir, "a.txt", "1\n", "c1")
        sub_c2 = _commit_file(sub_dir, "a.txt", "2\n", "c2")

        _init_repo(src_dir)
        _git(
            src_dir,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{sub_c1},submod",
        )
        _git(src_dir, "commit", "--quiet", "-m", "Add submod at c1")
        r_c1 = _git(src_dir, "rev-parse", "HEAD")

        _git(
            src_dir,
            "update-index",
            "--cacheinfo",
            f"160000,{sub_c2},submod",
        )
        _git(src_dir, "commit", "--quiet", "-m", "Roll submod to c2")
        r_c2 = _git(src_dir, "rev-parse", "HEAD")

        collector = commit_deps.CommitDepsCollector(src_dir, allow_fetch=False)
        entries = collector.collect(r_c1, r_c2)
        self.assertEqual(
            entries,
            [
                commit_deps.CommitEntry("repo", 0, r_c2, roll=True),
                commit_deps.CommitEntry("repo/submod", 1, sub_c2, roll=False),
            ],
        )

    def test_formats_table_csv_json(self) -> None:
        entries = [
            commit_deps.CommitEntry("src", 0, "a" * 40, roll=True),
            commit_deps.CommitEntry("src/v8", 1, "b" * 40, roll=False),
        ]

        table_out = commit_deps.format_output(entries, "table")
        self.assertIn("| Path   | Level | commit hash", table_out)
        self.assertIn(f"| src    | 0     | {'a' * 40} | true  |", table_out)
        self.assertIn(f"| src/v8 | 1     | {'b' * 40} | false |", table_out)

        csv_out = commit_deps.format_output(entries, "csv")
        rows = list(csv.reader(io.StringIO(csv_out)))
        self.assertEqual(
            rows,
            [
                ["Path", "Level", "commit hash", "roll"],
                ["src", "0", "a" * 40, "true"],
                ["src/v8", "1", "b" * 40, "false"],
            ],
        )

        json_out = commit_deps.format_output(entries, "json")
        self.assertEqual(
            json.loads(json_out),
            [
                {"path": "src", "level": 0, "commit": "a" * 40, "roll": True},
                {
                    "path": "src/v8",
                    "level": 1,
                    "commit": "b" * 40,
                    "roll": False,
                },
            ],
        )

    def test_sync_commands_output(self) -> None:
        entries = [
            commit_deps.CommitEntry("src", 0, "a" * 40, roll=True),
            commit_deps.CommitEntry("src/v8", 1, "b" * 40, roll=True),
            commit_deps.CommitEntry(
                "src/v8/third_party/sub", 2, "c" * 40, roll=False
            ),
            commit_deps.CommitEntry("src", 0, "d" * 40, roll=False),
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
            commit_deps.build_sync_commands(entries), expected_cmds
        )

        csv_rows = list(
            csv.reader(
                io.StringIO(
                    commit_deps.format_output(entries, "csv", sync_command=True)
                )
            )
        )
        self.assertEqual(
            csv_rows[0],
            ["Path", "Level", "commit hash", "roll", "sync command"],
        )
        self.assertEqual([r[4] for r in csv_rows[1:]], expected_cmds)

        json_items = json.loads(
            commit_deps.format_output(entries, "json", sync_command=True)
        )
        self.assertEqual(
            [item["sync_command"] for item in json_items], expected_cmds
        )

    def test_max_level(self) -> None:
        repos = self._setup_nested_repos()
        src_c1, _src_c2, _src_c3_roll, src_c4 = repos.src

        for max_level in (0, 1):
            collector = commit_deps.CommitDepsCollector(
                repos.src_dir, allow_fetch=False, max_level=max_level
            )
            self.assertEqual(
                collector.collect(src_c1, src_c4),
                [e for e in repos.entries if e.level <= max_level],
            )

    def test_positional_git_log_ranges(self) -> None:
        repos = self._setup_nested_repos()
        src_c1, _src_c2, src_c3_roll, src_c4 = repos.src
        collector = commit_deps.CommitDepsCollector(
            repos.src_dir, allow_fetch=False
        )

        cases = [
            ((f"{src_c1}..{src_c4}",), (src_c1, src_c4)),
            ((f"{src_c1}..",), (src_c1, "HEAD")),
            ((f"{src_c1}...{src_c4}",), (src_c1, src_c4)),
            ((f"{src_c3_roll}^!",), (src_c3_roll, src_c3_roll)),
            ((src_c3_roll,), (src_c3_roll, src_c3_roll)),
            ((src_c1, src_c4), (src_c1, src_c4)),
        ]
        for cli_args, expected in cases:
            self.assertEqual(
                self._resolve_cli_range(collector, repos.src_dir, *cli_args),
                expected,
            )

        invalid_ranges = [
            (src_c1, None, [f"{src_c1}..{src_c4}"]),
            (None, None, [src_c1, src_c3_roll, src_c4]),
            (None, None, []),
        ]
        for start, end, revs in invalid_ranges:
            with self.assertRaises(commit_deps.CommitDepsError):
                collector.resolve_revision_range(start, end, revs)

    def test_main_cli(self) -> None:
        repos = self._setup_nested_repos()
        _src_c1, _src_c2, src_c3_roll, _src_c4 = repos.src

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            rc = commit_deps.main(
                [
                    "--repo",
                    str(repos.src_dir),
                    "--format=json",
                    "--no-fetch",
                    src_c3_roll,
                ]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            [dataclasses.asdict(e) for e in repos.entries[1:6]],
        )

        with contextlib.redirect_stdout(io.StringIO()) as stdout_sync:
            rc_sync = commit_deps.main(
                [
                    "--repo",
                    str(repos.src_dir),
                    "--format=json",
                    "--sync-command",
                    "--no-fetch",
                    src_c3_roll,
                ]
            )
        self.assertEqual(rc_sync, 0)
        json_sync = json.loads(stdout_sync.getvalue())
        self.assertEqual(len(json_sync), 5)
        self.assertIn("sync_command", json_sync[0])

        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            err_rc = commit_deps.main(
                ["--repo", str(repos.src_dir), "--no-fetch"]
            )
        self.assertEqual(err_rc, 1)
        self.assertIn("Error:", stderr.getvalue())

    def test_parse_args_early_validation(self) -> None:
        args = commit_deps.parse_args(
            [
                "  HEAD~2..HEAD  ",
                "--repo=  ./src  ",
                "--start=  HEAD~2  ",
                "--end=  HEAD  ",
                "--format=JSON",
                "--max-level=0",
                "--sync-command",
            ]
        )
        self.assertEqual(args.revisions, ["HEAD~2..HEAD"])
        self.assertEqual(args.repo, Path("./src"))
        self.assertEqual(args.start, "HEAD~2")
        self.assertEqual(args.end, "HEAD")
        self.assertEqual(args.format, commit_deps.OutputFormat.JSON)
        self.assertEqual(args.max_level, 0)
        self.assertTrue(args.sync_command)

        invalid_arg_lists = [
            [""],
            ["   "],
            ["a b"],
            ["a\x00b"],
            ["--start="],
            ["--start=a b"],
            ["--end=-bad"],
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
            v8_dir, "DEPS", self._v8_deps(sub_c1), "v8 init DEPS"
        )
        v8_c2 = _commit_file(v8_dir, "v8.cc", "int v = 1;\n", "v8 regular work")
        v8_c3_roll = _commit_file(
            v8_dir, "DEPS", self._v8_deps(sub_c3), "Roll sub c1..c3"
        )

        _init_repo(src_dir)
        src_c1 = _commit_file(
            src_dir, "DEPS", self._src_deps(v8_c1), "src init DEPS"
        )
        src_c2 = _commit_file(src_dir, "main.cc", "int m = 1;\n", "src change")
        src_c3_roll = _commit_file(
            src_dir,
            "DEPS",
            self._src_deps(v8_c3_roll),
            "Roll V8 v8_c1..v8_c3",
        )
        src_c4 = _commit_file(src_dir, "main.cc", "int m = 2;\n", "src after")

        entries = (
            commit_deps.CommitEntry("src", 0, src_c2, roll=False),
            commit_deps.CommitEntry("src", 0, src_c3_roll, roll=True),
            commit_deps.CommitEntry("src/v8", 1, v8_c2, roll=False),
            commit_deps.CommitEntry("src/v8", 1, v8_c3_roll, roll=True),
            commit_deps.CommitEntry(
                "src/v8/third_party/sub", 2, sub_c2, roll=False
            ),
            commit_deps.CommitEntry(
                "src/v8/third_party/sub", 2, sub_c3, roll=False
            ),
            commit_deps.CommitEntry("src", 0, src_c4, roll=False),
        )
        return _NestedRepos(
            src_dir=src_dir,
            src=(src_c1, src_c2, src_c3_roll, src_c4),
            entries=entries,
        )

    @staticmethod
    def _v8_deps(sub_rev: str) -> str:
        return (
            "use_relative_paths = True\n"
            "deps = {\n"
            f'  "third_party/sub": "https://example.com/sub.git@{sub_rev}",\n'
            "}\n"
        )

    @staticmethod
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

    @staticmethod
    def _resolve_cli_range(
        collector: commit_deps.CommitDepsCollector,
        repo_dir: Path,
        *cli_args: str,
    ) -> tuple[str, str]:
        args = commit_deps.parse_args(["--repo", str(repo_dir), *cli_args])
        return collector.resolve_revision_range(
            args.start, args.end, args.revisions
        )

    def _assert_invalid_cli_args(self, bad_argv: list[str]) -> None:
        with (
            self.subTest(argv=bad_argv),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            commit_deps.parse_args(bad_argv)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")


def _commit_file(repo_dir: Path, rel_path: str, content: str, msg: str) -> str:
    _write_file(repo_dir / rel_path, content)
    _git(repo_dir, "add", rel_path)
    _git(repo_dir, "commit", "--quiet", "-m", msg)
    return _git(repo_dir, "rev-parse", "HEAD")


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
