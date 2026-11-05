#!/usr/bin/env vpython3
# coding=utf-8
# Copyright 2020 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Unit tests for gerrit_client.py."""

import contextlib
import io
import json
import logging
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gerrit_client
import gerrit_util


class TestGerritClient(unittest.TestCase):
    def setUp(self):
        # Keep tests independent of whether they run inside an AI agent.
        patcher = mock.patch("gclient_utils.IsEnvAi", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    @mock.patch("gerrit_util.GetGerritBranch", return_value="")
    def test_branch_info(self, util_mock):
        gerrit_client.main(
            [
                "branchinfo",
                "--host",
                "https://example.org/foo",
                "--project",
                "projectname",
                "--branch",
                "branchname",
            ]
        )
        util_mock.assert_called_once_with(
            "example.org", "projectname", "branchname"
        )

    @mock.patch("gerrit_util.CreateGerritBranch", return_value="")
    @mock.patch("gerrit_util.GetGerritBranch", return_value="")
    def test_branch(self, _, CreateGerritBranch_mock):
        gerrit_client.main(
            [
                "branch",
                "--host",
                "https://example.org/foo",
                "--project",
                "projectname",
                "--branch",
                "branchname",
                "--commit",
                "commitname",
            ]
        )
        CreateGerritBranch_mock.assert_called_once_with(
            "example.org", "projectname", "branchname", "commitname"
        )

    @mock.patch(
        "gerrit_util.CreateGerritTag", return_value={"ref": "refs/tags/v1"}
    )
    def test_tag(self, CreateGerritTag_mock):
        gerrit_client.main(
            [
                "tag",
                "--host",
                "https://example.org/foo",
                "--project",
                "projectname",
                "--tag",
                "v1",
                "--commit",
                "commitname",
            ]
        )
        CreateGerritTag_mock.assert_called_once_with(
            "example.org", "projectname", "v1", "commitname"
        )

    @mock.patch("gerrit_client.write_result")
    @mock.patch(
        "gerrit_util.GetGerritTag",
        return_value={"ref": "refs/tags/v1", "revision": "commitname"},
    )
    @mock.patch(
        "gerrit_util.CreateGerritTag",
        side_effect=gerrit_util.GerritError(
            200, 'Unexpected json output: Tag "v1" already exists'
        ),
    )
    def test_tag_conflict_same_commit(
        self, CreateGerritTag_mock, GetGerritTag_mock, write_result_mock
    ):
        gerrit_client.main(
            [
                "tag",
                "--host",
                "https://example.org/foo",
                "--project",
                "projectname",
                "--tag",
                "v1",
                "--commit",
                "commitname",
            ]
        )
        CreateGerritTag_mock.assert_called_once_with(
            "example.org", "projectname", "v1", "commitname"
        )
        GetGerritTag_mock.assert_called_once_with(
            "example.org", "projectname", "v1"
        )
        write_result_mock.assert_called_once_with(
            {"ref": "refs/tags/v1", "revision": "commitname"}, mock.ANY
        )

    @mock.patch(
        "gerrit_util.GetGerritTag",
        return_value={"ref": "refs/tags/v1", "revision": "othercommit"},
    )
    @mock.patch(
        "gerrit_util.CreateGerritTag",
        side_effect=gerrit_util.GerritError(
            200, 'Unexpected json output: Tag "v1" already exists'
        ),
    )
    def test_tag_conflict_different_commit(
        self, CreateGerritTag_mock, GetGerritTag_mock
    ):
        with self.assertRaisesRegex(
            gerrit_util.GerritError,
            "Conflict: tag 'v1' was created at 'othercommit' but "
            "the tag is not at the given commit 'commitname'",
        ):
            gerrit_client.main(
                [
                    "tag",
                    "--host",
                    "https://example.org/foo",
                    "--project",
                    "projectname",
                    "--tag",
                    "v1",
                    "--commit",
                    "commitname",
                ]
            )
        CreateGerritTag_mock.assert_called_once_with(
            "example.org", "projectname", "v1", "commitname"
        )
        GetGerritTag_mock.assert_called_once_with(
            "example.org", "projectname", "v1"
        )

    @mock.patch("gerrit_util.QueryChanges", return_value="")
    def test_changes(self, util_mock):
        gerrit_client.main(
            [
                "changes",
                "--host",
                "https://example.org/foo",
                "-p",
                "foo=bar",
                "-p",
                "baz=qux",
                "--limit",
                "10",
                "--start",
                "20",
                "-o",
                "op1",
                "-o",
                "op2",
            ]
        )
        util_mock.assert_called_once_with(
            "example.org",
            [("foo", "bar"), ("baz", "qux")],
            first_param=None,
            limit=10,
            start=20,
            o_params=["op1", "op2"],
        )

    @mock.patch("gerrit_util.QueryChanges", return_value="")
    def test_changes_query(self, util_mock):
        gerrit_client.main(
            [
                "changes",
                "--host",
                "https://example.org/foo",
                "--query",
                "is:owner is:open",
                "--limit",
                "10",
                "--start",
                "20",
            ]
        )
        util_mock.assert_called_once_with(
            "example.org",
            [],
            first_param="is:owner is:open",
            limit=10,
            start=20,
            o_params=None,
        )

    @mock.patch("gerrit_util.QueryChanges", return_value="")
    def test_changes_params_query(self, util_mock):
        gerrit_client.main(
            [
                "changes",
                "--host",
                "https://example.org/foo",
                "--query",
                "is:owner is:open",
                "-p",
                "foo=bar",
                "--limit",
                "10",
                "--start",
                "20",
            ]
        )
        util_mock.assert_called_once_with(
            "example.org",
            [("foo", "bar")],
            first_param="is:owner is:open",
            limit=10,
            start=20,
            o_params=None,
        )

    @mock.patch("gerrit_client.write_result")
    @mock.patch("gerrit_util.GetChangeChecks")
    def test_checks_revision_json(self, util_mock, write_result_mock):
        checks = [
            {"check_name": "ClangTidy", "results": [{"message": "finding"}]}
        ]
        util_mock.return_value = checks

        gerrit_client.main(
            [
                "checks",
                "--host",
                "https://example.org/foo",
                "--change",
                "8265612",
                "--revision",
                "1",
                "--json_file",
                "checks.json",
            ]
        )

        util_mock.assert_called_once_with(
            "example.org", "8265612", revision="1"
        )
        write_result_mock.assert_called_once()
        result, opt = write_result_mock.call_args.args
        self.assertIs(checks, result)
        self.assertEqual("checks.json", opt.json_file)

    @mock.patch("gerrit_util.GetChangeChecks", return_value=[])
    def test_checks_defaults(self, util_mock):
        gerrit_client.main(
            [
                "checks",
                "--host",
                "https://example.org/foo",
                "--change",
                "project~branch~Ichange",
            ]
        )

        util_mock.assert_called_once_with(
            "example.org",
            "project~branch~Ichange",
            revision="current",
        )

    @mock.patch("gerrit_util.GetChangeChecks")
    def test_checks_requires_change(self, util_mock):
        stderr = io.StringIO()
        with (
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as error,
        ):
            gerrit_client.main(["checks", "--host", "https://example.org"])

        self.assertEqual(2, error.exception.code)
        self.assertIn("--change is required", stderr.getvalue())
        util_mock.assert_not_called()

    @mock.patch("gerrit_client.write_result")
    @mock.patch("gerrit_util.GetChangeComments")
    def test_comments(self, util_mock, write_result_mock):
        comments = {"file.py": [{"message": "published comment"}]}
        util_mock.return_value = comments

        gerrit_client.main(
            [
                "comments",
                "--host",
                "https://example.org/foo",
                "--change",
                "project~branch~Ichange",
                "--json_file",
                "comments.json",
            ]
        )

        util_mock.assert_called_once_with(
            "example.org", "project~branch~Ichange"
        )
        write_result_mock.assert_called_once()
        result, opt = write_result_mock.call_args.args
        self.assertIs(comments, result)
        self.assertEqual("comments.json", opt.json_file)

    @mock.patch("gerrit_util.GetRelatedChanges", return_value="")
    def test_relatedchanges(self, util_mock):
        gerrit_client.main(
            [
                "relatedchanges",
                "--host",
                "https://example.org/foo",
                "--change",
                "foo-change-id",
                "--revision",
                "foo-revision-id",
            ]
        )
        util_mock.assert_called_once_with(
            "example.org", change="foo-change-id", revision="foo-revision-id"
        )

    @mock.patch("gerrit_util.CreateChange", return_value={})
    def test_createchange(self, util_mock):
        gerrit_client.main(
            [
                "createchange",
                "--host",
                "https://example.org/foo",
                "--project",
                "project",
                "--branch",
                "main",
                "--subject",
                "subject",
                "-p",
                "work_in_progress=true",
            ]
        )
        util_mock.assert_called_once_with(
            "example.org",
            "project",
            branch="main",
            subject="subject",
            params=[("work_in_progress", "true")],
        )

    @mock.patch("builtins.open", mock.mock_open())
    @mock.patch("gerrit_util.ChangeEdit", return_value="")
    def test_changeedit(self, util_mock):
        open().read.return_value = "test_data"
        gerrit_client.main(
            [
                "changeedit",
                "--host",
                "https://example.org/foo",
                "--change",
                "1",
                "--path",
                "path/to/file",
                "--file",
                "/my/foo",
            ]
        )
        util_mock.assert_called_once_with(
            "example.org", 1, "path/to/file", "test_data"
        )

    @mock.patch("gerrit_util.PublishChangeEdit", return_value="")
    def test_publishchangeedit(self, util_mock):
        gerrit_client.main(
            [
                "publishchangeedit",
                "--host",
                "https://example.org/foo",
                "--change",
                "1",
                "--notify",
                "yes",
            ]
        )
        util_mock.assert_called_once_with("example.org", 1, "yes")

    @mock.patch("gerrit_util.AbandonChange", return_value="")
    def test_abandon(self, util_mock):
        gerrit_client.main(
            [
                "abandon",
                "--host",
                "https://example.org/foo",
                "-c",
                "1",
                "-m",
                "bar",
            ]
        )
        util_mock.assert_called_once_with("example.org", 1, "bar")

    @mock.patch("gerrit_util.SetReview", return_value="")
    def test_setlabel(self, util_mock):
        gerrit_client.main(
            [
                "setlabel",
                "--host",
                "https://example.org/foo",
                "-c",
                "1",
                "-l",
                "some-label",
                "-2",
            ]
        )
        util_mock.assert_called_once_with(
            "example.org", 1, labels={"some-label": "-2"}
        )

    @mock.patch("gerrit_util.SetReview", return_value="")
    def test_addmessage(self, util_mock):
        gerrit_client.main(
            [
                "addmessage",
                "--host",
                "https://example.org/foo",
                "-c",
                "1",
                "-r",
                "2",
                "-m",
                "This is a message",
            ]
        )
        util_mock.assert_called_once_with(
            "example.org",
            1,
            revision="2",
            msg="This is a message",
            automatic_attention_set_update=None,
        )

    @mock.patch("gerrit_util.CreateDraft", return_value={})
    def test_adddraftcomment_defaults(self, util_mock):
        gerrit_client.main(
            [
                "adddraftcomment",
                "--host",
                "https://example.org/foo",
                "-c",
                "123",
                "-m",
                "Draft comment",
            ]
        )
        util_mock.assert_called_once_with(
            "example.org",
            "123",
            revision="current",
            body={"path": "/PATCHSET_LEVEL", "message": "Draft comment"},
        )

    @mock.patch("gerrit_client.write_result")
    @mock.patch("gerrit_util.CreateDraft")
    def test_adddraftcomment_options(self, util_mock, write_result_mock):
        draft = {"id": "draft-1", "message": "Inline reply"}
        util_mock.return_value = draft
        gerrit_client.main(
            [
                "adddraftcomment",
                "--host",
                "https://example.org/foo",
                "-c",
                "123",
                "-r",
                "2",
                "--path",
                "src/foo.cc",
                "--line",
                "42",
                "--side",
                "REVISION",
                "--in-reply-to",
                "comment-uuid",
                "--unresolved",
                "-m",
                "Inline reply",
                "--json_file",
                "draft.json",
            ]
        )
        util_mock.assert_called_once_with(
            "example.org",
            "123",
            revision="2",
            body={
                "path": "src/foo.cc",
                "line": 42,
                "side": "REVISION",
                "in_reply_to": "comment-uuid",
                "unresolved": True,
                "message": "Inline reply",
            },
        )
        write_result_mock.assert_called_once()
        result, opt = write_result_mock.call_args.args
        self.assertIs(draft, result)
        self.assertEqual("draft.json", opt.json_file)

    @mock.patch("gerrit_util.CreateDraft", return_value={})
    def test_adddraftcomment_body_and_override(self, util_mock):
        gerrit_client.main(
            [
                "adddraftcomment",
                "--host",
                "https://example.org/foo",
                "-c",
                "123",
                "--body",
                '{"path": "src/foo.cc", "line": 10, "message": "From body"}',
                "--resolved",
            ]
        )
        util_mock.assert_called_once_with(
            "example.org",
            "123",
            revision="current",
            body={
                "path": "src/foo.cc",
                "line": 10,
                "message": "From body",
                "unresolved": False,
            },
        )

    @mock.patch("gerrit_util.CreateDraft", return_value={})
    @mock.patch("gerrit_util.GetChangeComments")
    def test_adddraftcomment_in_reply_to_resolves_parent(
        self, get_comments_mock, create_draft_mock
    ):
        get_comments_mock.return_value = {
            "/PATCHSET_LEVEL": [{"id": "ps-uuid", "patch_set": 1}],
            "src/foo.cc": [
                {
                    "id": "comment-uuid",
                    "patch_set": 2,
                    "line": 42,
                    "side": "PARENT",
                    "range": {
                        "start_line": 42,
                        "start_character": 0,
                        "end_line": 42,
                        "end_character": 10,
                    },
                }
            ],
        }
        gerrit_client.main(
            [
                "adddraftcomment",
                "--host",
                "https://example.org/foo",
                "-c",
                "123",
                "--in-reply-to",
                "comment-uuid",
                "--resolved",
                "-m",
                "Done.",
            ]
        )
        get_comments_mock.assert_called_once_with("example.org", "123")
        create_draft_mock.assert_called_once_with(
            "example.org",
            "123",
            revision="2",
            body={
                "path": "src/foo.cc",
                "line": 42,
                "side": "PARENT",
                "range": {
                    "start_line": 42,
                    "start_character": 0,
                    "end_line": 42,
                    "end_character": 10,
                },
                "in_reply_to": "comment-uuid",
                "unresolved": False,
                "message": "Done.",
            },
        )

    @mock.patch("gerrit_util.GetChangeComments", return_value={})
    @mock.patch("gerrit_util.CreateDraft")
    def test_adddraftcomment_validation(self, util_mock, get_comments_mock):
        for args, expected_err in [
            (
                [
                    "adddraftcomment",
                    "--host",
                    "https://example.org",
                    "-m",
                    "hi",
                ],
                "--change is required",
            ),
            (
                [
                    "adddraftcomment",
                    "--host",
                    "https://example.org",
                    "-c",
                    "123",
                ],
                "--message is required",
            ),
            (
                [
                    "adddraftcomment",
                    "--host",
                    "https://example.org",
                    "-c",
                    "123",
                    "--body",
                    "{bad",
                ],
                "Invalid --body JSON",
            ),
            (
                [
                    "adddraftcomment",
                    "--host",
                    "https://example.org",
                    "-c",
                    "123",
                    "--body",
                    "[]",
                ],
                "--body must be a JSON object",
            ),
            (
                [
                    "adddraftcomment",
                    "--host",
                    "https://example.org",
                    "-c",
                    "123",
                    "--line",
                    "42",
                    "-m",
                    "hi",
                ],
                "--path is required when --line is specified",
            ),
            (
                [
                    "adddraftcomment",
                    "--host",
                    "https://example.org",
                    "-c",
                    "123",
                    "--in-reply-to",
                    "missing-uuid",
                    "-m",
                    "hi",
                ],
                "Could not find comment with UUID missing-uuid to reply to",
            ),
        ]:
            stderr = io.StringIO()
            with (
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit) as error,
            ):
                gerrit_client.main(args)
            self.assertEqual(2, error.exception.code)
            self.assertIn(expected_err, stderr.getvalue())
        util_mock.assert_not_called()
        get_comments_mock.assert_called_once_with("example.org", "123")

    @mock.patch("gerrit_util.CallGerritApi", return_value={})
    def test_rawapi(self, util_mock):
        gerrit_client.main(
            [
                "rawapi",
                "--host",
                "https://example.org/foo",
                "--path",
                "/changes/123/revisions/current/drafts",
                "--method",
                "PUT",
                "--accept_status",
                "200,201",
                "--body",
                '{"path": "/PATCHSET_LEVEL", "message": "Draft"}',
            ]
        )
        util_mock.assert_called_once_with(
            "example.org",
            "/changes/123/revisions/current/drafts",
            reqtype="PUT",
            accept_statuses=[200, 201],
            body={"path": "/PATCHSET_LEVEL", "message": "Draft"},
        )

    @mock.patch("gerrit_util.GetPatch", return_value=b"formatted patch\n")
    def test_patch_defaults(self, util_mock):
        with mock.patch("gerrit_client.sys.stdout") as stdout_mock:
            gerrit_client.main(
                [
                    "patch",
                    "--host",
                    "https://example.org/foo",
                    "--change",
                    "Ichange",
                ]
            )

        util_mock.assert_called_once_with(
            "example.org",
            "Ichange",
            revision="current",
            path=None,
            parent=None,
            context=None,
        )
        stdout_mock.buffer.write.assert_called_once_with(b"formatted patch\n")

    @mock.patch("gerrit_client.write_result")
    @mock.patch("gerrit_util.GetPatch", return_value=b"file patch\n")
    def test_patch_options(self, util_mock, write_result_mock):
        with mock.patch("gerrit_client.sys.stdout") as stdout_mock:
            gerrit_client.main(
                [
                    "patch",
                    "--host",
                    "https://example.org/foo",
                    "--change",
                    "123",
                    "--revision",
                    "7",
                    "--path",
                    "src/file name.cc",
                    "--parent",
                    "2",
                    "--context",
                    "0",
                    "--json_file",
                    "ignored.json",
                ]
            )

        util_mock.assert_called_once_with(
            "example.org",
            "123",
            revision="7",
            path="src/file name.cc",
            parent=2,
            context=0,
        )
        stdout_mock.buffer.write.assert_called_once_with(b"file patch\n")
        write_result_mock.assert_not_called()


class TestTrustedAuthorsOnly(unittest.TestCase):
    TRUSTED = {"_account_id": 1, "name": "Tru", "email": "a@chromium.org"}
    BOT = {"_account_id": 2, "email": "b@x.com", "tags": ["SERVICE_USER"]}
    EXTERNAL = {"_account_id": 3, "name": "Evil", "email": "c@example.com"}
    HOST = ["--host", "https://example.org/foo"]
    R = gerrit_client.REDACTED

    def setUp(self):
        patcher = mock.patch("gclient_utils.IsEnvAi", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_trusted_accounts(self):
        trusted = gerrit_client.IsTrustedAccount
        self.assertTrue(trusted(self.TRUSTED))
        self.assertTrue(trusted(self.BOT))
        self.assertTrue(trusted({"_account_id": 4, "email": "d@Microsoft.com"}))
        self.assertFalse(trusted(self.EXTERNAL))
        self.assertFalse(trusted({"_account_id": 5}))
        self.assertFalse(trusted({"_account_id": 6, "email": "e@gmail.com"}))
        self.assertFalse(
            trusted({"_account_id": 7, "email": "f@chromium.org.example.com"})
        )

    def test_redacts_untrusted_comments(self):
        comments = gerrit_client.RedactUntrusted(
            {
                "a.cc": [
                    {"author": self.TRUSTED, "message": "lgtm", "line": 3},
                    {"author": self.BOT, "message": "bot"},
                    {
                        "author": self.EXTERNAL,
                        "message": "evil",
                        "line": 4,
                        "updated": "2026-01-01",
                        "fix_suggestions": [{"description": "evil"}],
                    },
                ]
            }
        )["a.cc"]
        self.assertEqual("lgtm", comments[0]["message"])
        self.assertEqual("Tru", comments[0]["author"]["name"])
        self.assertEqual("bot", comments[1]["message"])
        external = comments[2]
        self.assertEqual(self.R, external["message"])
        self.assertEqual(self.R, external["fix_suggestions"][0]["description"])
        self.assertEqual(self.R, external["author"]["name"])
        self.assertEqual("c@example.com", external["author"]["email"])
        self.assertEqual(
            (4, "2026-01-01"), (external["line"], external["updated"])
        )

    def test_redacts_untrusted_accounts_on_trusted_changes(self):
        change = gerrit_client.RedactUntrusted(
            {
                "owner": self.TRUSTED,
                "subject": "trusted",
                "reviewers": {"CC": [{"_account_id": 3, "status": "evil"}]},
                "messages": [{"author": self.EXTERNAL, "message": "evil"}],
            }
        )
        self.assertEqual("trusted", change["subject"])
        self.assertEqual(self.R, change["reviewers"]["CC"][0]["status"])
        self.assertEqual(self.R, change["messages"][0]["message"])

    def test_subject_requires_trusted_uploaders(self):
        change = gerrit_client.RedactUntrusted(
            {
                "owner": self.TRUSTED,
                "subject": "evil",
                "revisions": {
                    "a": {
                        "uploader": self.EXTERNAL,
                        # Unverified commit authors cannot add trust.
                        "commit": {
                            "author": {"email": "x@chromium.org"},
                            "message": "evil",
                        },
                    }
                },
            }
        )
        self.assertEqual(self.R, change["subject"])
        self.assertEqual(self.R, change["revisions"]["a"]["commit"]["message"])

    def test_unattributed_content_fails_closed(self):
        related = gerrit_client.RedactUntrusted(
            {
                "changes": [
                    {
                        "project": "p",
                        "commit": {"commit": "abc", "subject": "evil"},
                    }
                ]
            }
        )["changes"][0]
        self.assertEqual("p", related["project"])
        self.assertEqual("abc", related["commit"]["commit"])
        self.assertEqual(self.R, related["commit"]["subject"])

    def _trusted_authors_only(self, *flags):
        parser = gerrit_client.OptionParser()
        opt, _ = parser.parse_args(self.HOST + list(flags))
        return opt.trusted_authors_only

    def test_flag_defaults_to_agent_detection(self):
        self.assertTrue(self._trusted_authors_only())
        self.assertFalse(
            self._trusted_authors_only("--trusted-authors-only=false")
        )
        with mock.patch("gclient_utils.IsEnvAi", return_value=False):
            self.assertFalse(self._trusted_authors_only())
            self.assertTrue(
                self._trusted_authors_only("--trusted-authors-only=true")
            )

    @mock.patch("gerrit_util.GetChangeComments")
    def test_json_file_is_redacted(self, comments_mock):
        comments_mock.return_value = {
            "a.cc": [{"author": self.EXTERNAL, "message": "external"}]
        }
        with mock.patch("gerrit_client.open", mock.mock_open()) as open_mock:
            gerrit_client.main(
                ["comments", "-c", "1", "--json_file", "out.json"] + self.HOST
            )
        written = json.loads(open_mock().write.call_args.args[0])
        self.assertEqual(self.R, written["a.cc"][0]["message"])

    @mock.patch("gerrit_util.QueryChanges", return_value=[])
    def test_changes_requests_detailed_accounts_once(self, query_mock):
        gerrit_client.main(
            ["changes", "--query", "x", "-o", "DETAILED_ACCOUNTS"] + self.HOST
        )
        self.assertEqual(
            ["DETAILED_ACCOUNTS"], query_mock.call_args.kwargs["o_params"]
        )

    def _detail(self, owner, *uploaders):
        return {
            "owner": owner,
            "current_revision": "sha%d" % (len(uploaders) - 1),
            "revisions": {
                "sha%d" % i: {"uploader": u} for i, u in enumerate(uploaders)
            },
        }

    @mock.patch("gerrit_util.GetPatch", return_value=b"code")
    def test_patch_fetches_checked_revision(self, patch_mock):
        detail = self._detail(self.TRUSTED, self.BOT, self.TRUSTED)
        with (
            mock.patch("gerrit_util.GetChangeDetail", return_value=detail),
            mock.patch("gerrit_client.sys.stdout"),
        ):
            gerrit_client.main(["patch", "-c", "1"] + self.HOST)
        self.assertEqual("sha1", patch_mock.call_args.kwargs["revision"])

    @mock.patch("gerrit_util.GetPatch")
    @mock.patch("gerrit_util.GetFileContents")
    def test_code_refused_for_untrusted_authors(self, content_mock, patch_mock):
        external_owner = self._detail(self.EXTERNAL, self.TRUSTED)
        external_upload = self._detail(
            self.TRUSTED, self.TRUSTED, self.EXTERNAL
        )
        external_upload["revisions"]["sha0"]["real_uploader"] = self.EXTERNAL
        for detail in (external_owner, external_upload):
            for command in (["patch"], ["content", "--path", "a.cc"]):
                with (
                    mock.patch(
                        "gerrit_util.GetChangeDetail", return_value=detail
                    ),
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    gerrit_client.main(command + ["-c", "1"] + self.HOST)
        content_mock.assert_not_called()
        patch_mock.assert_not_called()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG if "-v" in sys.argv else logging.ERROR
    )
    unittest.main()
