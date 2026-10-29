#!/usr/bin/env vpython3
# Copyright 2015 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import ast
import codecs
import copy
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "recipes",
        "recipe_modules",
        "bot_update",
        "resources",
    ),
)
import bot_update


class MockedPopen(object):
    """A fake instance of a called subprocess.

    This is meant to be used in conjunction with MockedCall.
    """

    def __init__(self, args=None, kwargs=None):
        self.args = args or []
        self.kwargs = kwargs or {}
        self.return_value = None
        self.fails = False

    def returns(self, rv):
        """Set the return value when this popen is called.

        rv can be a string, or a callable (eg function).
        """
        self.return_value = rv
        return self

    def check(self, args, kwargs):
        """Check to see if the given args/kwargs call match this instance.

        This does a partial match, so that a call to "git clone foo" will match
        this instance if this instance was recorded as "git clone"
        """
        if any(
            input_arg != expected_arg
            for (input_arg, expected_arg) in zip(args, self.args)
        ):
            return False
        return self.return_value

    def __call__(self, args, kwargs):
        """Actually call this popen instance."""
        if hasattr(self.return_value, "__call__"):
            return self.return_value(*args, **kwargs)
        return self.return_value


class MockedCall(object):
    """A fake instance of bot_update.call().

    This object is pre-seeded with "answers" in self.expectations.  The type
    is a MockedPopen object, or any object with a __call__() and check() method.
    The check() method is used to check to see if the correct popen object is
    chosen (can be a partial match, eg a "git clone" popen module would match
    a "git clone foo" call).
    By default, if no answers have been pre-seeded, the call() returns successful
    with an empty string.
    """

    def __init__(self, fake_filesystem):
        self.expectations = []
        self.records = []

    def expect(self, args=None, kwargs=None):
        args = args or []
        kwargs = kwargs or {}
        popen = MockedPopen(args, kwargs)
        self.expectations.append(popen)
        return popen

    def __call__(self, *args, **kwargs):
        self.records.append((args, kwargs))
        for popen in self.expectations:
            if popen.check(args, kwargs):
                self.expectations.remove(popen)
                return popen(args, kwargs)
        return ""


class MockedGclientSync:
    """A class producing a callable instance of gclient sync."""

    def __init__(self, fake_filesystem):
        self.records = []

    def __call__(self, *args, **_):
        self.records.append(args)


class FakeFile:
    def __init__(self):
        self.contents = ""

    def write(self, buf):
        if isinstance(buf, bytes):
            buf = buf.decode("utf-8")
        self.contents += buf

    def read(self):
        return self.contents

    def __enter__(self):
        return self

    def __exit__(self, _, __, ___):
        pass


class FakeFilesystem:
    def __init__(self):
        self.files = {}

    def open(self, target, mode="r", encoding=None):
        if "w" in mode:
            self.files[target] = FakeFile()
            return self.files[target]
        return self.files[target]


def fake_git(*args, **kwargs):
    return bot_update.call("git", *args, **kwargs)


class BotUpdateUnittests(unittest.TestCase):
    DEFAULT_PARAMS = {
        "solutions": [{"name": "somename", "url": "https://fake.com"}],
        "revisions": {},
        "first_sln": "somename",
        "target_os": None,
        "target_os_only": None,
        "target_cpu": None,
        "patch_root": None,
        "patch_refs": [],
        "gerrit_rebase_patch_ref": None,
        "no_fetch_tags": False,
        "no_history": False,
        "shallow": False,
        "refs": [],
        "git_cache_dir": "",
        "cleanup_dir": None,
        "gerrit_reset": None,
        "enforce_fetch": False,
        "experiments": [],
    }

    def setUp(self):
        self.old_platform = sys.platform
        sys.platform = "linux2"  # For consistency, ya know?
        self.filesystem = FakeFilesystem()
        self.call = MockedCall(self.filesystem)
        self.gclient = MockedGclientSync(self.filesystem)
        self.call.expect(
            (sys.executable, "-u", bot_update.GCLIENT_PATH, "sync")
        ).returns(self.gclient)
        self.old_call = getattr(bot_update, "call")
        self.old_git = getattr(bot_update, "git")
        self.params = copy.deepcopy(self.DEFAULT_PARAMS)
        setattr(bot_update, "call", self.call)
        setattr(bot_update, "git", fake_git)

        self.old_os_cwd = os.getcwd
        setattr(os, "getcwd", lambda: "/b/build/foo/build")

        setattr(bot_update, "open", self.filesystem.open)
        self.old_codecs_open = codecs.open
        setattr(codecs, "open", self.filesystem.open)

    def tearDown(self):
        sys.platform = self.old_platform
        setattr(bot_update, "call", self.old_call)
        setattr(bot_update, "git", self.old_git)
        setattr(os, "getcwd", self.old_os_cwd)
        delattr(bot_update, "open")
        setattr(codecs, "open", self.old_codecs_open)

    def overrideSetupForWindows(self):
        sys.platform = "win"
        self.call.expect(
            (sys.executable, "-u", bot_update.GCLIENT_PATH, "sync")
        ).returns(self.gclient)

    def testBasic(self):
        bot_update.ensure_checkout(**self.params)
        return self.call.records

    def testBasicCachepackOffloading(self):
        os.environ["PACKFILE_OFFLOADING"] = "1"
        bot_update.ensure_checkout(**self.params)
        os.environ.pop("PACKFILE_OFFLOADING")
        return self.call.records

    def testBasicRevision(self):
        self.params["revisions"] = {
            "src": "HEAD",
            "src/v8": "deadbeef",
            "somename": "DNE",
            "https://fake.com.git": "DNE",
        }
        bot_update.ensure_checkout(**self.params)
        args = self.gclient.records[0]
        idx_first_revision = args.index("--revision")
        idx_second_revision = args.index("--revision", idx_first_revision + 1)
        idx_third_revision = args.index("--revision", idx_second_revision + 1)
        self.assertEqual(args[idx_first_revision + 1], "somename@unmanaged")
        self.assertEqual(
            args[idx_second_revision + 1], "src@refs/remotes/origin/main"
        )
        self.assertEqual(args[idx_third_revision + 1], "src/v8@deadbeef")
        self.assertNotIn("--revision", args[idx_third_revision + 2 :])
        return self.call.records

    def testTagsByDefault(self):
        bot_update.ensure_checkout(**self.params)
        found = False
        for record in self.call.records:
            args = record[0]
            if args[:3] == ("git", "cache", "populate"):
                self.assertFalse("--no-fetch-tags" in args)
                found = True
        self.assertTrue(found)
        return self.call.records

    def testNoTags(self):
        params = self.params
        params["no_fetch_tags"] = True
        bot_update.ensure_checkout(**params)
        found = False
        for record in self.call.records:
            args = record[0]
            if args[:3] == ("git", "cache", "populate"):
                self.assertTrue("--no-fetch-tags" in args)
                found = True
        self.assertTrue(found)
        return self.call.records

    def testGclientNoSyncExperiment(self):
        ref = "refs/changes/12/345/6"
        repo = "https://chromium.googlesource.com/v8/v8"
        self.params["patch_refs"] = ["%s@%s" % (repo, ref)]
        self.params["experiments"] = bot_update.EXP_NO_SYNC
        bot_update.ensure_checkout(**self.params)
        args = self.gclient.records[0]
        idx = args.index("--experiment")
        self.assertEqual(args[idx + 1], bot_update.EXP_NO_SYNC)

    def testApplyPatchOnGclient(self):
        ref = "refs/changes/12/345/6"
        repo = "https://chromium.googlesource.com/v8/v8"
        self.params["patch_refs"] = ["%s@%s" % (repo, ref)]
        bot_update.ensure_checkout(**self.params)
        args = self.gclient.records[0]
        idx = args.index("--patch-ref")
        self.assertEqual(args[idx + 1], self.params["patch_refs"][0])
        self.assertNotIn("--patch-ref", args[idx + 1 :])
        # Assert we're not patching in bot_update.py
        for record in self.call.records:
            self.assertNotIn("git fetch " + repo, " ".join(record[0]))

    def testPatchRefs(self):
        self.params["patch_refs"] = [
            "https://chromium.googlesource.com/chromium/src@refs/changes/12/345/6",
            "https://chromium.googlesource.com/v8/v8@refs/changes/1/234/56",
        ]
        bot_update.ensure_checkout(**self.params)
        args = self.gclient.records[0]
        patch_refs = set(
            args[i + 1]
            for i in range(len(args))
            if args[i] == "--patch-ref" and i + 1 < len(args)
        )
        self.assertIn(self.params["patch_refs"][0], patch_refs)
        self.assertIn(self.params["patch_refs"][1], patch_refs)

    def expectCommitMessage(self, message: str, revision: str = "HEAD") -> None:
        """Makes the next `git log` for `revision` return `message`."""
        self.call.expect(
            ("git", "log", "--format=%B", "-n1", revision)
        ).returns(message)

    def expectNotAnAncestor(self, attempts: int = 2) -> None:
        """Makes the next `attempts` `git merge-base --is-ancestor` calls fail."""

        def fail(*_args: object, **_kwargs: object) -> None:
            raise bot_update.SubprocessFailed("merge-base failed", 1, "")

        for _ in range(attempts):
            self.call.expect(("git", "merge-base", "--is-ancestor")).returns(
                fail
            )

    def ancestryChecks(self) -> list[tuple[tuple[str, ...], dict[str, str]]]:
        """Returns the `git merge-base --is-ancestor` calls made so far."""
        return [
            (args, kwargs)
            for args, kwargs in self.call.records
            if args[:3] == ("git", "merge-base", "--is-ancestor")
        ]

    def testGetCommitPositionAcceptsFooterOnItsBranch(self) -> None:
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.assertEqual(
            bot_update.get_commit_position("src"), "refs/heads/main@{#123}"
        )
        # The revision must be reachable from the remote ref of the footer.
        self.assertEqual(
            self.ancestryChecks(),
            [
                (
                    (
                        "git",
                        "merge-base",
                        "--is-ancestor",
                        "HEAD",
                        "refs/remotes/origin/main",
                    ),
                    {"cwd": "src"},
                )
            ],
        )

    def testGetCommitPositionAcceptsOriginalFooter(self) -> None:
        self.expectCommitMessage(
            "Subject\n\n"
            "Cr-Original-Commit-Position: refs/branch-heads/4000@{#7}\n",
            revision="deadbeef",
        )
        self.assertEqual(
            bot_update.get_commit_position("src", "deadbeef"),
            "refs/branch-heads/4000@{#7}",
        )
        self.assertEqual(
            self.ancestryChecks()[0][0][3:],
            ("deadbeef", "refs/remotes/branch-heads/4000"),
        )

    def testGetCommitPositionWithoutFooter(self) -> None:
        self.expectCommitMessage("Subject\n\nBug: 1234\n")
        self.assertIsNone(bot_update.get_commit_position("src"))
        self.assertEqual(self.ancestryChecks(), [])

    def testGetCommitPositionRejectsMalformedFooter(self) -> None:
        for value in (
            "123",
            "main@{#123}",
            "refs/heads/main@{#12a}",
            "refs/heads/main@{#123}x",
            "refs/heads/ma in@{#123}",
            "refs/heads/main@{#1}@{#2}",
            "refs/remotes/origin/main@{#999999}",
            "refs/tags/132.0.6824.0@{#123}",
            "refs/heads/main~0@{#123}",
            "refs/heads/main..other@{#123}",
            "refs/heads//main@{#123}",
            "refs/heads/main/@{#123}",
            "refs/heads/main.@{#123}",
            "refs/heads/HEAD@{#123}",
            "refs/heads/head@{#123}",
            "refs/heads/a/.b@{#123}",
            "refs/heads/a./b@{#123}",
            "refs/heads/a.lock@{#123}",
            "refs/heads/a.LOCK@{#123}",
            "refs/heads/a.lock/b@{#123}",
            "refs/heads/main@{#1234567890}",
            "refs/heads/main@{#١٢٣}",
        ):
            with self.subTest(value=value):
                self.expectCommitMessage(
                    f"Subject\n\nCr-Commit-Position: {value}\n"
                )
                self.assertIsNone(bot_update.get_commit_position("src"))
                self.assertEqual(self.ancestryChecks(), [])

    def testGetCommitPositionRejectsGerritChangeRef(self) -> None:
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/changes/12/345/6@{#123}\n"
        )
        self.assertIsNone(
            bot_update.get_commit_position(
                "src", expected_branch="refs/changes/12/345/6"
            )
        )
        self.assertEqual(self.ancestryChecks(), [])

    def testGetCommitPositionRejectsMismatchedBranch(self) -> None:
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.assertIsNone(
            bot_update.get_commit_position(
                "src", expected_branch="refs/remotes/origin/experimental"
            )
        )
        self.assertEqual(self.ancestryChecks(), [])

    def testGetCommitPositionAcceptsMatchingBranch(self) -> None:
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/branch-heads/4000@{#7}\n"
        )
        self.assertEqual(
            bot_update.get_commit_position(
                "src", expected_branch="refs/remotes/branch-heads/4000"
            ),
            "refs/branch-heads/4000@{#7}",
        )
        self.assertEqual(len(self.ancestryChecks()), 1)

    def testGetCommitPositionRejectsNonAncestor(self) -> None:
        # E.g. an unmerged patchset or a cherry-pick onto another branch whose
        # message claims a position on main.
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.expectNotAnAncestor()
        self.assertIsNone(bot_update.get_commit_position("src"))
        self.assertEqual(len(self.ancestryChecks()), 2)

    def testGetCommitPositionFetchesStaleRemoteRefAndRetriesAncestry(
        self,
    ) -> None:
        # On warm bots, gclient_scm may skip fetching when a DEPS pin SHA is
        # already present in the shared git cache, leaving the local tracking
        # ref behind until get_commit_position fetches it.
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/branch-heads/4000@{#7}\n"
        )
        self.expectNotAnAncestor(attempts=1)
        self.call.expect(
            ("git", "rev-parse", "--is-shallow-repository")
        ).returns("false\n")
        self.assertEqual(
            bot_update.get_commit_position("src"),
            "refs/branch-heads/4000@{#7}",
        )
        self.assertEqual(len(self.ancestryChecks()), 2)
        self.assertIn(
            (
                (
                    "git",
                    "fetch",
                    "--no-tags",
                    "origin",
                    "refs/branch-heads/4000",
                ),
                {"cwd": "src"},
            ),
            self.call.records,
        )

    def testGetCommitPositionFallsBackToUpstreamPushUrlWhenOriginLacksOrHasStaleRef(
        self,
    ) -> None:
        # 1. When origin is a local git_cache mirror populated without
        # --with_branch_heads, fetching refs/branch-heads/* from origin fails
        # and get_commit_position falls back to remote.origin.pushurl.
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/branch-heads/6824@{#1}\n"
        )
        self.expectNotAnAncestor(attempts=1)
        self.call.expect(
            ("git", "rev-parse", "--is-shallow-repository")
        ).returns("false\n")
        self.call.expect(
            ("git", "remote", "get-url", "--push", "origin")
        ).returns("https://chromium.googlesource.com/chromium/src.git\n")
        self.call.expect(("git", "remote", "get-url", "origin")).returns(
            "/b/s/w/ir/cache/git/chromium.googlesource.com-chromium-src.git\n"
        )

        def fail_origin_fetch(*_args: object, **_kwargs: object) -> None:
            raise bot_update.SubprocessFailed("fetch origin failed", 128, "")

        self.call.expect(("git", "fetch", "--no-tags", "origin")).returns(
            fail_origin_fetch
        )
        self.assertEqual(
            bot_update.get_commit_position("src"),
            "refs/branch-heads/6824@{#1}",
        )
        self.assertEqual(len(self.ancestryChecks()), 2)
        self.assertIn(
            (
                (
                    "git",
                    "fetch",
                    "--no-tags",
                    "https://chromium.googlesource.com/chromium/src.git",
                    "refs/branch-heads/6824",
                ),
                {"cwd": "src"},
            ),
            self.call.records,
        )

        # 2. When origin has an older (stale) branch ref in the local git_cache
        # mirror, fetching from origin succeeds (exit 0) but ancestry fails;
        # non-shallow checkouts must still fall back to remote.origin.pushurl.
        self.call.records.clear()
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/branch-heads/6824@{#2}\n"
        )
        self.expectNotAnAncestor(attempts=2)
        self.call.expect(
            ("git", "rev-parse", "--is-shallow-repository")
        ).returns("false\n")
        self.call.expect(
            ("git", "remote", "get-url", "--push", "origin")
        ).returns("https://chromium.googlesource.com/chromium/src.git\n")
        self.call.expect(("git", "remote", "get-url", "origin")).returns(
            "/b/s/w/ir/cache/git/chromium.googlesource.com-chromium-src.git\n"
        )
        self.assertEqual(
            bot_update.get_commit_position("src"),
            "refs/branch-heads/6824@{#2}",
        )
        self.assertEqual(len(self.ancestryChecks()), 3)
        self.assertIn(
            (
                (
                    "git",
                    "fetch",
                    "--no-tags",
                    "https://chromium.googlesource.com/chromium/src.git",
                    "refs/branch-heads/6824",
                ),
                {"cwd": "src"},
            ),
            self.call.records,
        )

    def testGetCommitPositionAcceptsShallowRepositoryWhenAncestryUnavailable(
        self,
    ) -> None:
        # Older pinned commits in a shallow checkout lie beyond the shallow
        # graft boundary (so HEAD<=branch is false and merge-base exits 1),
        # and should still be accepted once cp_ref is fetched into FETCH_HEAD.
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.expectNotAnAncestor(attempts=2)
        self.call.expect(
            ("git", "rev-parse", "--is-shallow-repository")
        ).returns("true\n")

        def no_merge_base(*_args: object, **_kwargs: object) -> None:
            raise bot_update.SubprocessFailed("no merge base", 1, "")

        self.call.expect(("git", "merge-base", "FETCH_HEAD", "HEAD")).returns(
            no_merge_base
        )
        self.assertEqual(
            bot_update.get_commit_position("src"),
            "refs/heads/main@{#123}",
        )
        self.assertEqual(len(self.ancestryChecks()), 2)
        self.assertIn(
            (
                (
                    "git",
                    "fetch",
                    "--no-tags",
                    "origin",
                    "refs/heads/main",
                ),
                {"cwd": "src"},
            ),
            self.call.records,
        )

    def testGetCommitPositionRejectsShallowDescendantAheadOfBranch(
        self,
    ) -> None:
        # An unmerged patchset checked out on top of or diverged from cp_branch
        # in a shallow checkout shares a merge-base with FETCH_HEAD (exit 0) and
        # must be rejected; fatal git errors (exit 128) must also fail closed.
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.expectNotAnAncestor(attempts=2)
        self.call.expect(
            ("git", "rev-parse", "--is-shallow-repository")
        ).returns("true\n")
        self.assertIsNone(bot_update.get_commit_position("src"))
        self.assertEqual(len(self.ancestryChecks()), 2)
        for args, _ in self.call.records:
            self.assertNotIn(
                "https://chromium.googlesource.com/chromium/src.git", args
            )

        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.expectNotAnAncestor(attempts=2)
        self.call.expect(
            ("git", "rev-parse", "--is-shallow-repository")
        ).returns("true\n")

        def fatal_merge_base(*_args: object, **_kwargs: object) -> None:
            raise bot_update.SubprocessFailed("fatal git error", 128, "")

        self.call.expect(("git", "merge-base", "FETCH_HEAD", "HEAD")).returns(
            fatal_merge_base
        )
        self.assertIsNone(bot_update.get_commit_position("src"))

        # Depth-1 (--no-history) FETCH_HEAD has no parent commit (FETCH_HEAD~1
        # fails rev-parse --verify) and must not vacuously pass the shallow
        # merge-base fallback.
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.expectNotAnAncestor(attempts=2)
        self.call.expect(
            ("git", "rev-parse", "--is-shallow-repository")
        ).returns("true\n")

        def no_parent(*_args: object, **_kwargs: object) -> None:
            raise bot_update.SubprocessFailed("no parent", 128, "")

        self.call.expect(
            (
                "git",
                "rev-parse",
                "--verify",
                "FETCH_HEAD~1",
            )
        ).returns(no_parent)
        self.assertIsNone(bot_update.get_commit_position("src"))

        # Orphan HEAD commit (HEAD~1 fails rev-parse --verify) must also fail
        # closed even when FETCH_HEAD~1 exists.
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.expectNotAnAncestor(attempts=2)
        self.call.expect(
            ("git", "rev-parse", "--is-shallow-repository")
        ).returns("true\n")
        self.call.expect(("git", "rev-parse", "--verify", "HEAD~1")).returns(
            no_parent
        )
        self.assertIsNone(bot_update.get_commit_position("src"))

        # When origin lacks cp_ref in a shallow checkout and get_commit_position
        # falls back to fetching --depth 10000 from remote.origin.pushurl, a
        # non-ancestor commit must be rejected without entering the shallow
        # merge-base fallback.
        self.call.records.clear()
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/branch-heads/6824@{#1}\n"
        )
        self.expectNotAnAncestor(attempts=2)
        self.call.expect(
            ("git", "rev-parse", "--is-shallow-repository")
        ).returns("true\n")
        self.call.expect(
            ("git", "remote", "get-url", "--push", "origin")
        ).returns("https://chromium.googlesource.com/chromium/src.git\n")
        self.call.expect(("git", "remote", "get-url", "origin")).returns(
            "/b/s/w/ir/cache/git/chromium.googlesource.com-chromium-src.git\n"
        )

        def fail_origin_fetch(*_args: object, **_kwargs: object) -> None:
            raise bot_update.SubprocessFailed("fetch origin failed", 128, "")

        self.call.expect(
            ("git", "fetch", "--depth", "10000", "--no-tags", "origin")
        ).returns(fail_origin_fetch)
        self.assertIsNone(bot_update.get_commit_position("src"))
        self.assertIn(
            (
                (
                    "git",
                    "fetch",
                    "--depth",
                    "10000",
                    "--no-tags",
                    "https://chromium.googlesource.com/chromium/src.git",
                    "refs/branch-heads/6824",
                ),
                {"cwd": "src"},
            ),
            self.call.records,
        )

        # When allow_shallow=False, a shallow repo still fetches from origin
        # without --depth 1 (preserving shallow history) and enforces full
        # ancestry without the shallow merge-base fallback.
        self.call.records.clear()
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.expectNotAnAncestor(attempts=2)
        self.call.expect(
            ("git", "rev-parse", "--is-shallow-repository")
        ).returns("true\n")
        self.assertIsNone(
            bot_update.get_commit_position("src", allow_shallow=False)
        )
        self.assertIn(
            (
                (
                    "git",
                    "fetch",
                    "--no-tags",
                    "origin",
                    "refs/heads/main",
                ),
                {"cwd": "src"},
            ),
            self.call.records,
        )

        # With no_history=True (--depth 1), get_commit_position only fetches
        # --depth 1 from origin and never fetches 10,000 commits from upstream;
        # leading-hyphen revision arguments are also rejected immediately.
        self.assertIsNone(
            bot_update.get_commit_position("src", revision="--bad")
        )
        self.call.records.clear()
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.expectNotAnAncestor(attempts=2)
        self.call.expect(
            ("git", "rev-parse", "--is-shallow-repository")
        ).returns("true\n")
        self.assertIsNone(
            bot_update.get_commit_position(
                "src", allow_shallow=False, no_history=True
            )
        )
        self.assertIn(
            (
                (
                    "git",
                    "fetch",
                    "--depth",
                    "1",
                    "--no-tags",
                    "origin",
                    "refs/heads/main",
                ),
                {"cwd": "src"},
            ),
            self.call.records,
        )

        # If rev-parse --is-shallow-repository fails, is_shallow defaults to
        # False and fails closed on non-ancestors. Also cover remote get-url
        # failure in _is_shallow_local_mirror.
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.expectNotAnAncestor(attempts=2)
        self.call.expect(
            ("git", "rev-parse", "--is-shallow-repository")
        ).returns(no_parent)
        self.call.expect(("git", "remote", "get-url", "origin")).returns(
            no_parent
        )
        self.assertEqual(bot_update._is_shallow_local_mirror("src"), False)
        self.assertIsNone(bot_update.get_commit_position("src"))

    MANIFEST = {
        "src": {
            "repository": "https://chromium.googlesource.com/chromium/src.git",
            "revision": "a" * 40,
        }
    }

    def testParseGotRevisionReportsValidCommitPosition(self) -> None:
        self.call.expect(("git", "rev-parse", "HEAD")).returns("a" * 40 + "\n")
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.assertEqual(
            bot_update.parse_got_revision(
                self.MANIFEST, {"got_revision": "src"}, {"src": "HEAD"}
            ),
            {
                "got_revision": "a" * 40,
                "got_revision_cp": "refs/heads/main@{#123}",
            },
        )

    def testParseGotRevisionWithoutRevisionsIsBackwardsCompatible(
        self,
    ) -> None:
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        properties = bot_update.parse_got_revision(
            self.MANIFEST, {"got_revision": "src"}
        )
        self.assertEqual(
            properties["got_revision_cp"], "refs/heads/main@{#123}"
        )

    def testParseGotRevisionOmitsFooterForOtherBranch(self) -> None:
        # Checkout requested at refs/heads/experimental, footer claims main.
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.assertEqual(
            bot_update.parse_got_revision(
                self.MANIFEST,
                {"got_revision": "src"},
                {"src": "refs/heads/experimental"},
            ),
            {"got_revision": ""},
        )
        self.assertEqual(self.ancestryChecks(), [])

    def testParseGotRevisionOmitsFooterForOtherBranchByUrl(self) -> None:
        # Revisions may be keyed by the normalized repo url (.git suffix) while
        # the manifest repository url from DEPS omits .git.
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        manifest_without_dot_git = {
            "src": {
                "repository": "https://chromium.googlesource.com/chromium/src",
                "revision": "a" * 40,
            }
        }
        properties = bot_update.parse_got_revision(
            manifest_without_dot_git,
            {"got_revision": "src", "got_cr_revision": "src"},
            {
                "src": "HEAD",
                "https://chromium.googlesource.com/chromium/src.git": (
                    "refs/branch-heads/4000:" + "a" * 40
                ),
            },
        )
        self.assertNotIn("got_revision_cp", properties)
        self.assertNotIn("got_cr_revision_cp", properties)
        self.assertEqual(self.ancestryChecks(), [])

    def testParseGotRevisionReportsFooterForRequestedBranch(self) -> None:
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/experimental@{#5}\n"
        )
        properties = bot_update.parse_got_revision(
            self.MANIFEST,
            {"got_revision": "src"},
            {"src": "refs/heads/experimental"},
        )
        self.assertEqual(
            properties["got_revision_cp"], "refs/heads/experimental@{#5}"
        )
        self.assertEqual(
            self.ancestryChecks()[0][0][3:],
            ("HEAD", "refs/remotes/origin/experimental"),
        )

    def testParseGotRevisionPinnedHashOnlyChecksAncestry(self) -> None:
        # A bare commit hash does not name a branch, so the footer's own branch
        # is used for the ancestry check.
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/branch-heads/4000@{#7}\n"
        )
        properties = bot_update.parse_got_revision(
            self.MANIFEST, {"got_revision": "src"}, {"src": "a" * 40}
        )
        self.assertEqual(
            properties["got_revision_cp"], "refs/branch-heads/4000@{#7}"
        )
        self.assertEqual(
            self.ancestryChecks()[0][0][3:],
            ("HEAD", "refs/remotes/branch-heads/4000"),
        )

    def testParseGotRevisionTagCheckoutPreservesBranchHeadFooter(
        self,
    ) -> None:
        # Official release tag checkouts carry a branch-heads commit position
        # footer; parse_got_revision must validate ancestry on that branch
        # rather than requiring the footer to literally match refs/tags/*.
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/branch-heads/6824@{#1}\n"
        )
        properties = bot_update.parse_got_revision(
            self.MANIFEST,
            {"got_revision": "src"},
            {"src": "refs/tags/132.0.6824.0"},
        )
        self.assertEqual(
            properties["got_revision_cp"], "refs/branch-heads/6824@{#1}"
        )
        self.assertEqual(
            self.ancestryChecks()[0][0][3:],
            ("HEAD", "refs/remotes/branch-heads/6824"),
        )
        for alias_ref in ("refs/heads/lkgr", "refs/heads/lkcr"):
            self.expectCommitMessage(
                "Subject\n\nCr-Commit-Position: refs/heads/main@{#42}\n"
            )
            props = bot_update.parse_got_revision(
                self.MANIFEST,
                {"got_revision": "src"},
                {"src": alias_ref},
            )
            self.assertEqual(props["got_revision_cp"], "refs/heads/main@{#42}")

    def testParseGotRevisionOmitsFooterOfNonAncestor(self) -> None:
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/heads/main@{#123}\n"
        )
        self.expectNotAnAncestor()
        properties = bot_update.parse_got_revision(
            self.MANIFEST, {"got_revision": "src"}, {"src": "a" * 40}
        )
        self.assertNotIn("got_revision_cp", properties)

    def testParseGotRevisionOmitsGerritChangeFooter(self) -> None:
        self.expectCommitMessage(
            "Subject\n\nCr-Commit-Position: refs/changes/12/345/6@{#999999}\n"
        )
        properties = bot_update.parse_got_revision(
            self.MANIFEST,
            {"got_revision": "src"},
            {"src": "refs/changes/12/345/6"},
        )
        self.assertNotIn("got_revision_cp", properties)
        self.assertEqual(self.ancestryChecks(), [])

    def testGitCheckoutBreaksLocks(self):
        self.overrideSetupForWindows()
        path = "/b/build/foo/build/.git"
        lockfile = "index.lock"
        removed = []
        old_os_walk = os.walk
        old_os_remove = os.remove
        setattr(os, "walk", lambda _: [(path, None, [lockfile])])
        setattr(os, "remove", removed.append)
        bot_update.ensure_checkout(**self.params)
        setattr(os, "walk", old_os_walk)
        setattr(os, "remove", old_os_remove)
        self.assertTrue(os.path.join(path, lockfile) in removed)

    def testGitSyncCorruptionWipesCache(self):
        call_count = [0]

        def mock_sync_stateful(*_args, **_kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise bot_update.GclientSyncFailed(
                    "gclient sync failed", 1, "error: bad object abcde123"
                )
            return ""

        self.call.expectations = []
        self.call.expect(
            (sys.executable, "-u", bot_update.GCLIENT_PATH, "sync")
        ).returns(mock_sync_stateful)
        self.call.expect(
            (sys.executable, "-u", bot_update.GCLIENT_PATH, "sync")
        ).returns(mock_sync_stateful)

        class Options(object):
            def __init__(self):
                self.git_cache_dir = "/fake/git/cache/dir"
                self.cleanup_dir = "/fake/cleanup/dir"
                self.experiments = ""
                self.patch_root = None
                self.patch_refs = []
                self.gerrit_no_rebase_patch_ref = False
                self.download_topics = False
                self.no_fetch_tags = False
                self.no_history = False
                self.shallow = False
                self.enforce_fetch = False
                self.refs = []
                self.clean_ignored = False
                self.gerrit_no_reset = False
                self.output_json = "/fake/output.json"
                self.revision_mapping = {}
                self.clobber = False

        options = Options()
        git_slns = self.params["solutions"]
        specs = {}
        revisions = {"somename": "HEAD"}
        step_text = "some step text"

        removed = []
        old_exists = os.path.exists
        setattr(
            os.path,
            "exists",
            lambda path: (
                True if path == "/fake/git/cache/dir" else old_exists(path)
            ),
        )
        self.addCleanup(setattr, os.path, "exists", old_exists)

        old_makedirs = os.makedirs
        setattr(os, "makedirs", lambda path, exist_ok=False: None)
        self.addCleanup(setattr, os, "makedirs", old_makedirs)

        old_remove = getattr(bot_update, "remove")
        setattr(
            bot_update,
            "remove",
            lambda path, clean_dir: removed.append((path, clean_dir)),
        )
        self.addCleanup(setattr, bot_update, "remove", old_remove)

        old_create_manifest = getattr(bot_update, "create_manifest")
        setattr(bot_update, "create_manifest", lambda: {})
        self.addCleanup(
            setattr, bot_update, "create_manifest", old_create_manifest
        )

        old_disk_usage = getattr(bot_update, "disk_usage")
        setattr(bot_update, "disk_usage", lambda: (10, 100, 10))
        self.addCleanup(setattr, bot_update, "disk_usage", old_disk_usage)

        bot_update.checkout(options, git_slns, specs, revisions, step_text)

        self.assertEqual(call_count[0], 2)
        self.assertEqual(
            removed, [("/fake/git/cache/dir", "/fake/cleanup/dir")]
        )

        # Verify allow_shallow passed to parse_got_revision in checkout().
        recorded_allow_shallow: list[bool] = []
        old_parse_got_revision = getattr(bot_update, "parse_got_revision")
        setattr(
            bot_update,
            "parse_got_revision",
            lambda _m, _rm, _r=None, allow_shallow=True, no_history=False: (
                recorded_allow_shallow.append(allow_shallow)
                or {"got_revision": "a" * 40}
            ),
        )
        self.addCleanup(
            setattr, bot_update, "parse_got_revision", old_parse_got_revision
        )
        for (
            shallow_opt,
            no_history_opt,
            patch_refs_opt,
            revs_opt,
            expected,
        ) in (
            (True, False, [], {"somename": "HEAD"}, True),
            (True, True, [], {"somename": "HEAD"}, False),
            (
                True,
                False,
                ["https://fake.com@refs/changes/1/1/1"],
                {"somename": "HEAD"},
                False,
            ),
            (True, False, [], {"somename": "refs/changes/1/1/1"}, False),
            (False, False, [], {"somename": "HEAD"}, False),
        ):
            options.shallow = shallow_opt
            options.no_history = no_history_opt
            options.patch_refs = patch_refs_opt
            bot_update.checkout(options, git_slns, specs, revs_opt, step_text)
            self.assertEqual(recorded_allow_shallow[-1], expected)

    def testCommitPositionRegexSyncedWithRecipeApi(self) -> None:
        api_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "recipes",
            "recipe_modules",
            "bot_update",
            "api.py",
        )
        with self.old_codecs_open(api_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=api_path)
        api_pattern = None
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_COMMIT_POSITION_RE"
                and isinstance(node.value, ast.Call)
                and node.value.args
            ):
                api_pattern = ast.literal_eval(node.value.args[0])
                break
        self.assertEqual(api_pattern, bot_update.COMMIT_POSITION_RE.pattern)

    def testParsesRevisions(self):

        revisions = [
            "f671d3baeb64d9dba628ad582e867cf1aebc0207",
            "src@deadbeef",
            "https://foo.googlesource.com/a/a/bar.git.git/@12345",
            "bar@refs/experimental/test@example.com/test",
        ]
        expected_results = {
            "root": "f671d3baeb64d9dba628ad582e867cf1aebc0207",
            "src": "deadbeef",
            "https://foo.googlesource.com/bar.git": "12345",
            "bar": "refs/experimental/test@example.com/test",
        }
        actual_results = bot_update.parse_revisions(revisions, "root")
        self.assertEqual(expected_results, actual_results)

        with self.assertRaises(ValueError):
            bot_update.parse_revisions(["--upload-pack=foo"], "root")
        with self.assertRaises(ValueError):
            bot_update.parse_revisions(["src@--upload-pack=foo"], "root")
        with self.assertRaises(ValueError):
            bot_update.parse_revisions(["-bad_root@HEAD"], "root")
        with self.assertRaises(ValueError):
            bot_update.parse_revisions(["/-bad_root@HEAD"], "root")
        with self.assertRaises(ValueError):
            bot_update.parse_revisions([], "-bad_root")
        with self.assertRaises(ValueError):
            bot_update.parse_revisions([], "/-bad_root")


class CallUnitTest(unittest.TestCase):
    def testCall(self):
        ret = bot_update.call(sys.executable, "-c", "print(1)")
        self.assertEqual("1\n", ret)

    def testCallOnWindowsSetsNoDefaultCurrentDirectoryInExePath(self):
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("subprocess.Popen") as mock_popen,
        ):
            proc = mock_popen.return_value
            proc.stdout.read.return_value = b""
            proc.wait.return_value = 0

            bot_update.call("git.exe", "status")
            _, kwargs = mock_popen.call_args
            self.assertEqual(
                kwargs["env"].get("NoDefaultCurrentDirectoryInExePath"), "1"
            )

            mock_popen.reset_mock()
            bot_update.call(
                "git.exe",
                "status",
                env={"NoDefaultCurrentDirectoryInExePath": "0"},
            )
            _, kwargs = mock_popen.call_args
            self.assertEqual(
                kwargs["env"].get("NoDefaultCurrentDirectoryInExePath"), "0"
            )

    def testGitOnWindowsResolvesGitExe(self):
        bot_update._win_git_executable.cache_clear()
        self.addCleanup(bot_update._win_git_executable.cache_clear)
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(bot_update, "call", return_value="") as mock_call,
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(
                bot_update.git_common,
                "win_find_git",
                wraps=bot_update.git_common.win_find_git,
            ) as mock_win_find_git,
        ):
            # 1. git.exe directly on PATH (and verify repeated calls hit cache).
            exe_dir = os.path.join(td, "exe_dir")
            os.makedirs(exe_dir)
            exe_path = os.path.join(exe_dir, "git.exe")
            with open(exe_path, "w") as f:
                f.write("exe")
            with mock.patch.dict(os.environ, {"PATH": exe_dir}):
                bot_update.git("status")
                bot_update.git("diff")
                self.assertEqual(mock_win_find_git.call_count, 1)
                self.assertEqual(
                    mock_call.call_args_list,
                    [
                        mock.call(exe_path, "status"),
                        mock.call(exe_path, "diff"),
                    ],
                )

            # 2. git.bat on PATH wrapping git.exe via %~dp0.
            mock_call.reset_mock()
            bat_dir = os.path.join(td, "bat_dir")
            os.makedirs(bat_dir)
            bat_path = os.path.join(bat_dir, "git.bat")
            bundled_exe = os.path.join(bat_dir, "bin", "git.exe")
            with open(bat_path, "w") as f:
                f.write(
                    '@echo off\n"%%~dp0%s" %%*\n'
                    % os.path.join("bin", "git.exe")
                )
            with mock.patch.dict(os.environ, {"PATH": bat_dir}):
                bot_update.git("fetch", "origin")
                mock_call.assert_called_once_with(
                    bundled_exe, "fetch", "origin"
                )

            # 3. Fallback to git.bat when Git is not found on PATH.
            mock_call.reset_mock()
            with mock.patch.dict(os.environ, {"PATH": ""}):
                bot_update.git("status")
                mock_call.assert_called_once_with("git.bat", "status")

    def testGitOnNonWindowsAndCacheCommand(self):
        with (
            mock.patch.object(bot_update, "call", return_value="") as mock_call,
            mock.patch.object(sys, "platform", "linux2"),
        ):
            bot_update.git("status")
            mock_call.assert_called_once_with("git", "status")

            mock_call.reset_mock()
            bot_update.git("cache", "populate", "https://example.com/repo")
            mock_call.assert_called_once_with(
                sys.executable,
                "-u",
                bot_update.GIT_CACHE_PATH,
                "populate",
                "https://example.com/repo",
            )

    def testGetCommitPositionWithRealGitRepo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            origin = os.path.join(td, "origin")
            os.makedirs(origin)
            bot_update.git("init", "-b", "main", origin)
            bot_update.git(
                "config", "user.email", "test@example.com", cwd=origin
            )
            bot_update.git("config", "user.name", "Test", cwd=origin)
            commits: list[str] = []
            for idx in range(1, 5):
                with open(os.path.join(origin, "f.txt"), "w") as f:
                    f.write(str(idx))
                bot_update.git("add", "f.txt", cwd=origin)
                bot_update.git(
                    "commit",
                    "-m",
                    f"c{idx}\n\nCr-Commit-Position: refs/heads/main@{{#{idx}}}",
                    cwd=origin,
                )
                commits.append(
                    bot_update.git("rev-parse", "HEAD", cwd=origin).strip()
                )

            mirror = os.path.join(td, "mirror")
            bot_update.git(
                "clone", "--bare", "--depth", "2", f"file://{origin}", mirror
            )
            clone = os.path.join(td, "clone")
            bot_update.git("clone", "--depth", "2", f"file://{mirror}", clone)
            bot_update.git("remote", "set-url", "origin", mirror, cwd=clone)
            self.assertEqual(
                bot_update.get_commit_position(clone),
                "refs/heads/main@{#4}",
            )
            # Fetch c2 (which has parent c1, so c2^ exists) into the shallow
            # clone without connecting it to the depth-2 main window (c4, c3).
            bot_update.git(
                "fetch",
                "--depth",
                "2",
                f"file://{origin}",
                commits[1],
                cwd=clone,
            )
            bot_update.git("checkout", commits[1], cwd=clone)
            self.assertEqual(
                bot_update.get_commit_position(clone, allow_shallow=True),
                "refs/heads/main@{#2}",
            )
            self.assertIsNone(
                bot_update.get_commit_position(clone, allow_shallow=False)
            )


if __name__ == "__main__":
    unittest.main()
