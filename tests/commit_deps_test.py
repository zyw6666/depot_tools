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
    src_c1: str
    src_c2: str
    src_c3_roll: str
    src_c4: str
    entries: tuple[commit_deps.CommitEntry, ...]
    roll_entries: tuple[commit_deps.CommitEntry, ...]


class CommitDepsTest(trial_dir.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = Path(self.root_dir)

    def test_recursive_deps_expansion(self) -> None:
        repos = self._setup_nested_repos()
        collector = commit_deps.CommitDepsCollector(repos.src_dir)
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
                commit_deps.CommitEntry("src", 0, repos.src_c1, roll=False),
                commit_deps.CommitEntry("src", 0, repos.src_c2, roll=False),
            ],
        )

    def test_repo_resolution(self) -> None:
        repos = self._setup_nested_repos()
        collector = commit_deps.CommitDepsCollector(".", cwd=repos.src_dir)
        self.assertEqual(collector.repo_dir, repos.src_dir.resolve())
        self.assertEqual(collector.display_path, "src")

        with self.assertRaises(commit_deps.CommitDepsError):
            collector.collect("deadbeef" * 5, repos.src_c4)

        for non_repo in (self.workspace, self.workspace / "missing"):
            with self.assertRaises(commit_deps.CommitDepsError):
                commit_deps.CommitDepsCollector(non_repo)

    def test_gitlink_roll_detection(self) -> None:
        src_dir = self.workspace / "repo"
        sub_dir = src_dir / "submod"
        _init_repo(sub_dir)
        sub_c1 = _commit_file(sub_dir, "a.txt", "1\n", "c1")
        sub_c2 = _commit_file(sub_dir, "a.txt", "2\n", "c2")

        _init_repo(src_dir)
        repo_c1 = _commit_gitlink(src_dir, "submod", sub_c1, "Add submod at c1")
        repo_c2 = _commit_gitlink(
            src_dir, "submod", sub_c2, "Roll submod to c2"
        )

        collector = commit_deps.CommitDepsCollector(src_dir)
        entries = collector.collect(repo_c1, repo_c2)
        self.assertEqual(
            entries,
            [
                commit_deps.CommitEntry("repo", 0, repo_c2, roll=True),
                commit_deps.CommitEntry("repo/submod", 1, sub_c2, roll=False),
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

        collector = commit_deps.CommitDepsCollector(src_dir)
        self.assertEqual(
            collector.collect(c1, c3_missing_sub),
            [
                commit_deps.CommitEntry(
                    "cipd_repo", 0, c2_cipd_only, roll=False
                ),
                commit_deps.CommitEntry(
                    "cipd_repo", 0, c3_missing_sub, roll=True
                ),
            ],
        )

    def test_commit_log_and_diff_tree_parsing(self) -> None:
        self.assertEqual(commit_deps.CommitLogEntry.parse_log(None), [])
        self.assertEqual(commit_deps.CommitLogEntry.parse_log(""), [])

        sha_a = "A" * 40
        sha_b = "b" * 40
        raw_log = "\n".join(
            [
                f"COMMIT:{sha_a}",
                f":100644 100644 {sha_a} {sha_b} M\tDEPS",
                f":160000 160000 {sha_a} {sha_b} M\tsrc\\submod",
                f":000000 100644 {commit_deps.ZERO_SHA} {sha_b} A\tnew.cc",
                f":100644 100644 {sha_a} {sha_a} M\tunchanged.cc",
                "invalid line",
            ]
        )
        entries = commit_deps.CommitLogEntry.parse_log(raw_log)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].sha, sha_a.lower())
        deps_diff, gitlink_diff = entries[0].diffs
        self.assertTrue(deps_diff.is_deps_file)
        self.assertFalse(deps_diff.is_gitlink_roll)
        self.assertTrue(gitlink_diff.is_gitlink_roll)
        self.assertEqual(
            gitlink_diff.to_subrepo_roll(),
            commit_deps.SubrepoRoll("src/submod", sha_a.lower(), sha_b),
        )

    def test_formats_table_csv_json(self) -> None:
        entries = [
            commit_deps.CommitEntry("src", 0, "a" * 40, roll=True),
            commit_deps.CommitEntry("src/v8", 1, "b" * 40, roll=False),
        ]

        table_out = commit_deps.OutputFormat.TABLE.format_entries(entries)
        self.assertIn("| Path   | Level | commit hash", table_out)
        self.assertIn(f"| src    | 0     | {'a' * 40} | true  |", table_out)
        self.assertIn(f"| src/v8 | 1     | {'b' * 40} | false |", table_out)

        csv_out = commit_deps.OutputFormat.CSV.format_entries(entries)
        rows = list(csv.reader(io.StringIO(csv_out)))
        self.assertEqual(
            rows,
            [
                ["Path", "Level", "commit hash", "roll"],
                ["src", "0", "a" * 40, "true"],
                ["src/v8", "1", "b" * 40, "false"],
            ],
        )

        json_out = commit_deps.OutputFormat.JSON.format_entries(entries)
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
            commit_deps.CommitEntry.build_sync_commands(entries),
            expected_cmds,
        )

        table_out = commit_deps.OutputFormat.TABLE.format_entries(
            entries, sync_command=True
        )
        self.assertIn("sync command", table_out)
        for cmd in expected_cmds:
            self.assertIn(cmd, table_out)

        csv_rows = list(
            csv.reader(
                io.StringIO(
                    commit_deps.OutputFormat.CSV.format_entries(
                        entries, sync_command=True
                    )
                )
            )
        )
        self.assertEqual(
            csv_rows[0],
            ["Path", "Level", "commit hash", "roll", "sync command"],
        )
        self.assertEqual([r[4] for r in csv_rows[1:]], expected_cmds)

        json_items = json.loads(
            commit_deps.OutputFormat.JSON.format_entries(
                entries, sync_command=True
            )
        )
        self.assertEqual(
            [item["sync_command"] for item in json_items], expected_cmds
        )

    def test_max_level(self) -> None:
        repos = self._setup_nested_repos()
        for max_level in (0, 1):
            collector = commit_deps.CommitDepsCollector(
                repos.src_dir, max_level=max_level
            )
            self.assertEqual(
                collector.collect(repos.src_c1, repos.src_c4),
                [e for e in repos.entries if e.level <= max_level],
            )

    def test_positional_git_log_ranges(self) -> None:
        c1, c3, c4 = "a" * 40, "b" * 40, "c" * 40

        cases = [
            ((f"{c1}..{c4}",), commit_deps.RevisionRange(c1, c4)),
            ((f"{c1}..",), commit_deps.RevisionRange(c1, "HEAD")),
            ((f"..{c4}",), commit_deps.RevisionRange("HEAD", c4)),
            ((f"{c1}...{c4}",), commit_deps.RevisionRange(c1, c4)),
            ((f"{c3}^!",), commit_deps.RevisionRange(c3, c3)),
            ((c3,), commit_deps.RevisionRange(c3, c3)),
            ((c1, c4), commit_deps.RevisionRange(c1, c4)),
            (
                (f"--start={c1}", f"--end={c4}"),
                commit_deps.RevisionRange(c1, c4),
            ),
            ((f"--start={c1}",), commit_deps.RevisionRange(c1, "HEAD")),
            ((f"--start={c1}..{c4}",), commit_deps.RevisionRange(c1, c4)),
            ((f"--start={c3}^!",), commit_deps.RevisionRange(c3, c3)),
        ]
        for cli_args, expected in cases:
            resolved = self._resolve_cli_range(*cli_args)
            self.assertEqual(resolved, expected)
            self.assertEqual(resolved.start, expected.start)
            self.assertEqual(resolved.end, expected.end)
            self.assertEqual(
                resolved.is_single_commit, expected.start == expected.end
            )

        invalid_ranges = [
            (c1, None, [f"{c1}..{c4}"]),
            (None, c4, []),
            (None, None, [c1, c3, c4]),
            (None, None, []),
        ]
        for start, end, revs in invalid_ranges:
            with self.assertRaises(commit_deps.CommitDepsError):
                commit_deps.RevisionRange.from_args(start, end, revs)

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
        self.assertEqual(len(json_sync), len(repos.roll_entries))
        self.assertIn("sync_command", json_sync[0])

        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            error_exit_code = commit_deps.main(["--repo", str(repos.src_dir)])
        self.assertEqual(error_exit_code, 1)
        self.assertIn("Error:", stderr.getvalue())

    def test_parse_args_early_validation(self) -> None:
        args = commit_deps.InputParser.parse_args(
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
        v8_c1 = _commit_file(v8_dir, "DEPS", _v8_deps(sub_c1), "v8 init DEPS")
        v8_c2 = _commit_file(v8_dir, "v8.cc", "int v = 1;\n", "v8 regular work")
        v8_c3_roll = _commit_file(
            v8_dir, "DEPS", _v8_deps(sub_c3), "Roll sub c1..c3"
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
            commit_deps.CommitEntry("src", 0, src_c3_roll, roll=True),
            commit_deps.CommitEntry("src/v8", 1, v8_c2, roll=False),
            commit_deps.CommitEntry("src/v8", 1, v8_c3_roll, roll=True),
            commit_deps.CommitEntry(
                "src/v8/third_party/sub", 2, sub_c2, roll=False
            ),
            commit_deps.CommitEntry(
                "src/v8/third_party/sub", 2, sub_c3, roll=False
            ),
        )
        entries = (
            commit_deps.CommitEntry("src", 0, src_c2, roll=False),
            *roll_entries,
            commit_deps.CommitEntry("src", 0, src_c4, roll=False),
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

    def _resolve_cli_range(
        self,
        *cli_args: str,
    ) -> commit_deps.RevisionRange:
        args = commit_deps.InputParser.parse_args(list(cli_args))
        return commit_deps.RevisionRange.from_args(
            args.start, args.end, args.revisions
        )

    def _assert_invalid_cli_args(self, bad_argv: list[str]) -> None:
        with (
            self.subTest(argv=bad_argv),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            commit_deps.InputParser.parse_args(bad_argv)


def _v8_deps(sub_rev: str) -> str:
    return (
        "use_relative_paths = True\n"
        "deps = {\n"
        f'  "third_party/sub": "https://example.com/sub.git@{sub_rev}",\n'
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
