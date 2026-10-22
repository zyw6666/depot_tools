#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

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
import tempfile
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import commit_deps  # noqa: E402


@dataclasses.dataclass(frozen=True)
class _NestedRepos:
    src_dir: Path
    src: tuple[str, str, str, str]
    v8: tuple[str, str, str]
    sub: tuple[str, str, str]


def _git(cwd: Path | str, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "developer@chromium.org",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "developer@chromium.org",
    }
    return subprocess.check_output(
        ["git", "-C", str(cwd), *args],
        env=env,
        stderr=subprocess.STDOUT,
        text=True,
    ).strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit_file(repo_dir: Path, rel_path: str, content: str, msg: str) -> str:
    _write_file(repo_dir / rel_path, content)
    _git(repo_dir, "add", rel_path)
    _git(repo_dir, "commit", "--quiet", "-m", msg)
    return _git(repo_dir, "rev-parse", "HEAD")


class CommitDepsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.workspace = Path(self.temp_dir.name)
        commit_deps._get_blob_content.cache_clear()
        commit_deps._parse_deps_git_revs.cache_clear()

    def _setup_nested_repos(self) -> _NestedRepos:
        """Creates a 3-level gclient-like hierarchy:
        src (Level 0) -> src/v8 (Level 1) -> src/v8/third_party/sub (Level 2)
        """
        _write_file(
            self.workspace / ".gclient",
            'solutions = [{"name": "src", "url": "https://example.com/src.git"}]\n',
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
        v8_deps_1 = (
            "use_relative_paths = True\n"
            "deps = {\n"
            f'  "third_party/sub": "https://example.com/sub.git@{sub_c1}",\n'
            "}\n"
        )
        v8_c1 = _commit_file(v8_dir, "DEPS", v8_deps_1, "v8 init DEPS")
        v8_c2 = _commit_file(v8_dir, "v8.cc", "int v = 1;\n", "v8 regular work")
        v8_deps_2 = (
            "use_relative_paths = True\n"
            "deps = {\n"
            f'  "third_party/sub": "https://example.com/sub.git@{sub_c3}",\n'
            "}\n"
        )
        v8_c3_roll = _commit_file(v8_dir, "DEPS", v8_deps_2, "Roll sub c1..c3")

        _init_repo(src_dir)
        src_deps_1 = (
            "vars = {\n"
            f'  "v8_revision": "{v8_c1}",\n'
            "}\n"
            "deps = {\n"
            '  "src/v8": "https://example.com/v8.git" + "@"\n'
            '  + Var("v8_revision"),\n'
            "}\n"
        )
        src_c1 = _commit_file(src_dir, "DEPS", src_deps_1, "src init DEPS")
        src_c2 = _commit_file(src_dir, "main.cc", "int m = 1;\n", "src change")
        src_deps_2 = (
            "vars = {\n"
            f'  "v8_revision": "{v8_c3_roll}",\n'
            "}\n"
            "deps = {\n"
            '  "src/v8": "https://example.com/v8.git" + "@"\n'
            '  + Var("v8_revision"),\n'
            "}\n"
        )
        src_c3_roll = _commit_file(
            src_dir, "DEPS", src_deps_2, "Roll V8 v8_c1..v8_c3"
        )
        src_c4 = _commit_file(src_dir, "main.cc", "int m = 2;\n", "src after")

        return _NestedRepos(
            src_dir=src_dir,
            src=(src_c1, src_c2, src_c3_roll, src_c4),
            v8=(v8_c1, v8_c2, v8_c3_roll),
            sub=(sub_c1, sub_c2, sub_c3),
        )

    def test_recursive_deps_expansion(self) -> None:
        repos = self._setup_nested_repos()
        src_c1, src_c2, src_c3_roll, src_c4 = repos.src
        _v8_c1, v8_c2, v8_c3_roll = repos.v8
        _sub_c1, sub_c2, sub_c3 = repos.sub

        repo_dir, display_path, gclient_root = commit_deps.resolve_repo(
            repos.src_dir
        )
        self.assertEqual(display_path, "src")

        entries = commit_deps.collect_commit_deps(
            repo_dir=repo_dir,
            display_path=display_path,
            start_rev=src_c1,
            end_rev=src_c4,
            gclient_root=gclient_root,
            allow_fetch=False,
        )

        expected = [
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
        ]
        self.assertEqual(entries, expected)

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

        entries = commit_deps.collect_commit_deps(
            repo_dir=src_dir,
            display_path="repo",
            start_rev=r_c1,
            end_rev=r_c2,
            allow_fetch=False,
        )
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

    def test_max_level(self) -> None:
        repos = self._setup_nested_repos()
        src_c1, src_c2, src_c3_roll, src_c4 = repos.src
        _v8_c1, v8_c2, v8_c3_roll = repos.v8

        entries_level_0 = commit_deps.collect_commit_deps(
            repo_dir=repos.src_dir,
            display_path="src",
            start_rev=src_c1,
            end_rev=src_c4,
            allow_fetch=False,
            max_level=0,
        )
        self.assertEqual(
            entries_level_0,
            [
                commit_deps.CommitEntry("src", 0, src_c2, roll=False),
                commit_deps.CommitEntry("src", 0, src_c3_roll, roll=True),
                commit_deps.CommitEntry("src", 0, src_c4, roll=False),
            ],
        )

        entries_level_1 = commit_deps.collect_commit_deps(
            repo_dir=repos.src_dir,
            display_path="src",
            start_rev=src_c1,
            end_rev=src_c4,
            allow_fetch=False,
            max_level=1,
        )
        self.assertEqual(
            entries_level_1,
            [
                commit_deps.CommitEntry("src", 0, src_c2, roll=False),
                commit_deps.CommitEntry("src", 0, src_c3_roll, roll=True),
                commit_deps.CommitEntry("src/v8", 1, v8_c2, roll=False),
                commit_deps.CommitEntry("src/v8", 1, v8_c3_roll, roll=True),
                commit_deps.CommitEntry("src", 0, src_c4, roll=False),
            ],
        )

    def test_positional_git_log_ranges(self) -> None:
        repos = self._setup_nested_repos()
        src_c1, _src_c2, src_c3_roll, src_c4 = repos.src

        def resolve(*cli_args: str) -> tuple[str, str]:
            args = commit_deps.parse_args(
                ["--repo", str(repos.src_dir), *cli_args]
            )
            return commit_deps.resolve_revision_range(
                args.start,
                args.end,
                args.revisions,
                repo_dir=repos.src_dir,
                allow_fetch=False,
            )

        self.assertEqual(resolve(f"{src_c1}..{src_c4}"), (src_c1, src_c4))
        self.assertEqual(resolve(f"{src_c1}.."), (src_c1, "HEAD"))
        self.assertEqual(resolve(f"{src_c1}...{src_c4}"), (src_c1, src_c4))
        self.assertEqual(
            resolve(f"{src_c3_roll}^!"), (src_c3_roll, src_c3_roll)
        )
        self.assertEqual(resolve(src_c3_roll), (src_c3_roll, src_c3_roll))
        self.assertEqual(resolve(src_c1, src_c4), (src_c1, src_c4))

        with self.assertRaises(commit_deps.CommitDepsError):
            commit_deps.resolve_revision_range(
                src_c1, None, [f"{src_c1}..{src_c4}"]
            )
        with self.assertRaises(commit_deps.CommitDepsError):
            commit_deps.resolve_revision_range(
                None, None, [src_c1, src_c3_roll, src_c4]
            )
        with self.assertRaises(commit_deps.CommitDepsError):
            commit_deps.resolve_revision_range(None, None, [])

    def test_main_cli(self) -> None:
        repos = self._setup_nested_repos()
        _src_c1, _src_c2, src_c3_roll, _src_c4 = repos.src
        _v8_c1, v8_c2, v8_c3_roll = repos.v8
        _sub_c1, sub_c2, sub_c3 = repos.sub

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
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
            [
                {
                    "path": "src",
                    "level": 0,
                    "commit": src_c3_roll,
                    "roll": True,
                },
                {"path": "src/v8", "level": 1, "commit": v8_c2, "roll": False},
                {
                    "path": "src/v8",
                    "level": 1,
                    "commit": v8_c3_roll,
                    "roll": True,
                },
                {
                    "path": "src/v8/third_party/sub",
                    "level": 2,
                    "commit": sub_c2,
                    "roll": False,
                },
                {
                    "path": "src/v8/third_party/sub",
                    "level": 2,
                    "commit": sub_c3,
                    "roll": False,
                },
            ],
        )

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            err_rc = commit_deps.main(
                ["--repo", str(repos.src_dir), "--no-fetch"]
            )
        self.assertEqual(err_rc, 1)
        self.assertIn("Error:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
