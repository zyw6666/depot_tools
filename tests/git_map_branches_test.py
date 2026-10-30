#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Tests for `git_map_branches`."""

import os
import shutil
import sys
import unittest

DEPOT_TOOLS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DEPOT_TOOLS_ROOT)

from testing_support import git_test_utils  # noqa: E402

import git_common  # noqa: E402
import git_map_branches  # noqa: E402


class GitMapBranchesTest(git_test_utils.GitRepoReadWriteTestBase):
    """Tests for `git_map_branches` execution and `--show-worktree` option."""

    REPO_SCHEMA = """
    A B C
      B D
    """

    @classmethod
    def getRepoContent(cls, commit):
        return {commit: {"data": commit.encode("utf-8")}}

    @classmethod
    def setUpClass(cls):
        super(GitMapBranchesTest, cls).setUpClass()
        git_common.TEST_MODE = True

    def setUp(self):
        super(GitMapBranchesTest, self).setUp()
        self.repo.git("checkout", "branch_C")

    def tearDown(self):
        try:
            self.repo.git("worktree", "prune")
        except Exception:
            pass
        super(GitMapBranchesTest, self).tearDown()

    def testDefaultOutput(self):
        """Verifies default output without worktree option."""
        stdout, _ = self.repo.capture_stdio(
            git_map_branches.main, ["--no-color"]
        )
        lines = stdout.splitlines()
        self.assertTrue(any("branch_C *" in line for line in lines))
        self.assertTrue(any("branch_D" in line for line in lines))
        self.assertNotIn(self.repo.repo_path, stdout)

    def testShowWorktreeSingle(self):
        """Verifies `--show-worktree` displays path for checked out branch."""
        stdout, _ = self.repo.capture_stdio(
            git_map_branches.main, ["-w", "--no-color"]
        )
        lines = stdout.splitlines()
        c_line = next(line for line in lines if "branch_C *" in line)
        self.assertIn(f"({self.repo.repo_path})", c_line)

        d_line = next(line for line in lines if "branch_D" in line)
        self.assertNotIn("(", d_line)

    def testShowWorktreeLinked(self):
        """Verifies `--show-worktree` displays path for linked worktrees."""
        linked_wt = self.repo.repo_path + "_linked"
        self.addCleanup(shutil.rmtree, linked_wt, ignore_errors=True)
        self.repo.git("worktree", "add", linked_wt, "branch_D")

        stdout, _ = self.repo.capture_stdio(
            git_map_branches.main, ["--show-worktree", "--no-color"]
        )
        lines = stdout.splitlines()
        c_line = next(line for line in lines if "branch_C *" in line)
        self.assertIn(f"({self.repo.repo_path})", c_line)

        d_line = next(line for line in lines if "branch_D" in line)
        self.assertIn(f"({linked_wt})", d_line)

    def testShowWorktreeAliases(self):
        """Verifies `-w`, `--show-worktree`, and `--show-worktrees` match."""
        out_short, _ = self.repo.capture_stdio(
            git_map_branches.main, ["-w", "--no-color"]
        )
        out_singular, _ = self.repo.capture_stdio(
            git_map_branches.main, ["--show-worktree", "--no-color"]
        )
        out_plural, _ = self.repo.capture_stdio(
            git_map_branches.main, ["--show-worktrees", "--no-color"]
        )
        self.assertEqual(out_short, out_singular)
        self.assertEqual(out_singular, out_plural)

    def testShowWorktreeWithSubject(self):
        """Verifies worktree column appears before subject column."""
        linked_wt = self.repo.repo_path + "_linked"
        self.addCleanup(shutil.rmtree, linked_wt, ignore_errors=True)
        self.repo.git("worktree", "add", linked_wt, "branch_D")

        stdout, _ = self.repo.capture_stdio(
            git_map_branches.main,
            ["-w", "--show-subject", "--no-color"],
        )
        lines = stdout.splitlines()
        d_line = next(line for line in lines if "branch_D" in line)
        self.assertIn(f"({linked_wt})", d_line)
        self.assertIn("D", d_line)
        wt_index = d_line.index(f"({linked_wt})")
        subject_index = d_line.index("D", wt_index + len(f"({linked_wt})"))
        self.assertGreater(subject_index, wt_index)


if __name__ == "__main__":
    unittest.main()
