#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Unit tests for trajectory_import.py and git_cl.py Spec v6 features."""

from __future__ import annotations

import json
import optparse
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

import gerrit_util
import git_cl
import trajectory_import


class TrajectoryImportTest(unittest.TestCase):
    """Tests for turn parsing, tree reconstruction, CL_self/CL_peer, Simulate CQ, and Spec v6 gates."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="traj_test_")
        self.repo_root = os.path.join(self.tmpdir.name, "repo")
        self.brain_root = os.path.join(self.tmpdir.name, "brain")
        os.makedirs(self.repo_root, exist_ok=True)
        os.makedirs(self.brain_root, exist_ok=True)

        subprocess.run(
            ["git", "init", "-q"], cwd=self.repo_root, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "author@chromium.org"],
            cwd=self.repo_root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Author"],
            cwd=self.repo_root,
            check=True,
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_transcript(self, conv_id: str, steps: list[dict]) -> str:
        logs_dir = os.path.join(
            self.brain_root, conv_id, ".system_generated", "logs"
        )
        os.makedirs(logs_dir, exist_ok=True)
        transcript_path = os.path.join(logs_dir, "transcript_full.jsonl")
        with open(transcript_path, "w", encoding="utf-8") as f:
            for step in steps:
                f.write(json.dumps(step) + "\n")
        return transcript_path

    def testSelfReviewChangeIdDeterministicAndDistinct(self):
        canonical = "I1234567890abcdef1234567890abcdef12345678"
        self_id_1 = trajectory_import.SelfReviewChangeId(canonical)
        self_id_2 = trajectory_import.SelfReviewChangeId(canonical)
        self.assertEqual(self_id_1, self_id_2)
        self.assertNotEqual(self_id_1, canonical)
        self.assertTrue(self_id_1.startswith("I"))
        self.assertEqual(len(self_id_1), 41)

        desc = "Add feature X\n\nBug: 123\nChange-Id: " + canonical + "\n"
        updated_desc = trajectory_import.ReplaceChangeIdInDescription(
            desc, self_id_1
        )
        self.assertIn(f"Change-Id: {self_id_1}", updated_desc)
        self.assertNotIn(canonical, updated_desc)

    def testResolveSelfReviewHostChain(self):
        # 1. Explicit flag wins
        self.assertEqual(
            trajectory_import.ResolveSelfReviewHost(
                default_host="chromium-review.googlesource.com",
                explicit_host="chromium-internal-review.googlesource.com",
                git_config_getter=lambda k: "other-host.googlesource.com",
            ),
            "https://chromium-internal-review.googlesource.com",
        )
        # 2. Git config branch.<name>.gerritselfreviewserver wins when flag unset
        self.assertEqual(
            trajectory_import.ResolveSelfReviewHost(
                default_host="chromium-review.googlesource.com",
                explicit_host=None,
                git_config_getter=lambda k: (
                    "https://chromium-internal-review.googlesource.com/"
                    if k == trajectory_import.SELF_REVIEW_SERVER_CONFIG_KEY
                    else None
                ),
            ),
            "https://chromium-internal-review.googlesource.com",
        )
        # 3. Default host fallback
        self.assertEqual(
            trajectory_import.ResolveSelfReviewHost(
                default_host="chromium-review.googlesource.com",
            ),
            "https://chromium-review.googlesource.com",
        )

    def testResolveConversationIdChain(self):
        conv_id = "conv-abc-123"
        target_file = os.path.join(self.repo_root, "src", "foo.cc")
        self._write_transcript(
            conv_id,
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "content": "Create foo.cc",
                },
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [
                        {
                            "name": "write_file",
                            "args": {
                                "Path": target_file,
                                "Content": "int x = 1;\n",
                                "Overwrite": True,
                            },
                        }
                    ],
                },
            ],
        )

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTIGRAVITY_CONVERSATION_ID", None)
            self.assertEqual(
                trajectory_import.ResolveConversationId(
                    repo_root=self.repo_root,
                    explicit_id="explicit-999",
                    brain_root=self.brain_root,
                ),
                "explicit-999",
            )
            self.assertEqual(
                trajectory_import.ResolveConversationId(
                    repo_root=self.repo_root,
                    brain_root=self.brain_root,
                    git_config_getter=lambda k: "cfg-777",
                ),
                "cfg-777",
            )
            self.assertEqual(
                trajectory_import.ResolveConversationId(
                    repo_root=self.repo_root,
                    affected_files=["src/foo.cc"],
                    brain_root=self.brain_root,
                ),
                conv_id,
            )

    def testParseTrajectoryTurnsAndReconstructTurnTreesPinsHead(self):
        os.makedirs(os.path.join(self.repo_root, "src"), exist_ok=True)
        foo_path = os.path.join(self.repo_root, "src", "foo.cc")
        with open(foo_path, "w", encoding="utf-8") as f:
            f.write("int raw_ptr = 0;\n")
        subprocess.run(["git", "add", "."], cwd=self.repo_root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "Base commit"],
            cwd=self.repo_root,
            check=True,
        )
        base_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root, text=True
        ).strip()

        with open(foo_path, "w", encoding="utf-8") as f:
            f.write("int weak_ptr = 2;  // formatted by git cl format\n")
        subprocess.run(["git", "add", "."], cwd=self.repo_root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "WIP branch commit"],
            cwd=self.repo_root,
            check=True,
        )
        head_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root, text=True
        ).strip()
        head_tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD:"], cwd=self.repo_root, text=True
        ).strip()

        t_path = self._write_transcript(
            "conv-turns",
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "created_at": "2026-10-20T14:00:00Z",
                    "content": "Initialize raw_ptr to 1",
                },
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "Updated raw_ptr to 1.",
                    "tool_calls": [
                        {
                            "name": "edit_file",
                            "args": {
                                "Path": foo_path,
                                "Target": "int raw_ptr = 0;",
                                "Replacement": "int raw_ptr = 1;",
                            },
                        }
                    ],
                },
                {
                    "step_index": 2,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "created_at": "2026-10-20T14:02:00Z",
                    "content": "How does foo.cc work?",
                },
                {
                    "step_index": 3,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "It defines raw_ptr.",
                    "tool_calls": [
                        {"name": "read_file", "args": {"Path": foo_path}}
                    ],
                },
                {
                    "step_index": 4,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "created_at": "2026-10-20T14:05:00Z",
                    "content": "Switch raw_ptr to weak_ptr",
                },
                {
                    "step_index": 5,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "Switched to weak_ptr.",
                    "tool_calls": [
                        {
                            "name": "edit_file",
                            "args": {
                                "Path": foo_path,
                                "Target": "int raw_ptr = 1;",
                                "Replacement": "int weak_ptr = 2;",
                            },
                        }
                    ],
                },
            ],
        )

        turns = trajectory_import.ParseTrajectoryTurns(
            t_path, repo_root=self.repo_root
        )
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0].turn_index, 1)
        self.assertIn("[self-review][turn-1]", turns[0].short_title)
        self.assertEqual(turns[1].turn_index, 2)
        self.assertEqual(turns[1].user_prompt, "Switch raw_ptr to weak_ptr")

        snapshots = trajectory_import.ReconstructTurnTrees(
            repo_root=self.repo_root,
            base_commit=base_commit,
            head_commit=head_commit,
            turns=turns,
        )
        self.assertEqual(len(snapshots), 2)
        self.assertNotEqual(snapshots[0].tree_sha, snapshots[1].tree_sha)
        self.assertEqual(snapshots[1].tree_sha, head_tree)

        turn1_blob = subprocess.check_output(
            ["git", "show", f"{snapshots[0].tree_sha}:src/foo.cc"],
            cwd=self.repo_root,
            text=True,
        )
        self.assertEqual(turn1_blob, "int raw_ptr = 1;\n")

        # Also verify Sparse Root Tree construction for chrome-internal/playground
        base_sparse_tree, sparse_turn_trees = (
            trajectory_import.BuildSparseBaseAndTurnTrees(
                repo_root=self.repo_root,
                base_commit=base_commit,
                head_commit=head_commit,
                snapshots=snapshots,
                affected_files=["src/foo.cc"],
            )
        )
        self.assertEqual(len(sparse_turn_trees), 2)
        self.assertNotEqual(base_sparse_tree, sparse_turn_trees[0])
        self.assertNotEqual(sparse_turn_trees[0], sparse_turn_trees[1])
        base_sparse_blob = subprocess.check_output(
            ["git", "show", f"{base_sparse_tree}:src/foo.cc"],
            cwd=self.repo_root,
            text=True,
        )
        self.assertEqual(base_sparse_blob, "int raw_ptr = 0;\n")
        final_sparse_blob = subprocess.check_output(
            ["git", "show", f"{sparse_turn_trees[1]}:src/foo.cc"],
            cwd=self.repo_root,
            text=True,
        )
        self.assertEqual(
            final_sparse_blob,
            "int weak_ptr = 2;  // formatted by git cl format\n",
        )
        remote_url, branch, use_sparse = (
            trajectory_import.ResolveSelfReviewRemoteAndBranch(
                trajectory_import.DEFAULT_CHROME_INTERNAL_SELF_REVIEW_HOST,
                "https://chromium.googlesource.com/chromium/src.git",
                "refs/heads/main",
            )
        )
        self.assertTrue(use_sparse)
        self.assertEqual(
            remote_url,
            trajectory_import.DEFAULT_CHROME_INTERNAL_PLAYGROUND_REMOTE,
        )
        self.assertEqual(
            branch,
            trajectory_import.DEFAULT_CHROME_INTERNAL_PLAYGROUND_BRANCH,
        )

    def testPublishTurnSteeringCommentPreservesHumanAttribution(self):
        turn2 = trajectory_import.TrajectoryTurn(
            turn_index=2,
            step_index=4,
            created_at="2026-10-20T14:05:00Z",
            user_prompt="Switch raw_ptr to weak_ptr",
            planner_summary="Switched to weak_ptr.",
            short_title="[self-review][turn-2] Switch raw_ptr to weak_ptr",
            file_edits=[
                trajectory_import.FileEditOp(
                    rel_path="src/foo.cc",
                    abs_path="/repo/src/foo.cc",
                    op="edit",
                    start_line=10,
                    end_line=12,
                )
            ],
            modified_files=["src/foo.cc"],
        )

        with mock.patch("gerrit_util.CreateHttpConn") as mock_conn, mock.patch(
            "gerrit_util.ReadHttpJsonResponse", return_value={}
        ), mock.patch("gclient_utils.IsEnvAi", return_value=True):
            trajectory_import.PublishTurnSteeringComment(
                host="chromium-review.googlesource.com",
                change="123456",
                prior_patchset=1,
                next_turn=turn2,
            )
            self.assertTrue(mock_conn.called)
            _, kwargs = mock_conn.call_args
            body = kwargs["body"]
            self.assertEqual(body["notify"], "NONE")
            self.assertTrue(body["ignore_automatic_attention_set_rules"])
            comment = body["comments"]["src/foo.cc"][0]
            self.assertFalse(comment["unresolved"])
            self.assertFalse(comment["is_ai"])
            self.assertIn("Switch raw_ptr to weak_ptr", comment["message"])

    def testRunSimulateCqAndBuildSelfReviewMetadataV6WireContract(self):
        calls = []

        def fake_runner(cmd, cwd):
            calls.append(cmd)
            if cmd[:3] == ["git", "cl", "presubmit"]:
                return 0, "Presubmit checks passed (0.8s)"
            if "tools/autotest.py" in cmd:
                return 0, "12/12 tests passed in foo_unittest.cc"
            return 0, "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

        sim = trajectory_import.RunSimulateCq(
            repo_root=self.repo_root,
            affected_files=["src/foo.cc", "src/foo_unittest.cc"],
            out_dir="out/Default",
            head_commit="deadbeef1234",
            tree_sha="4b825dc642cb6eb9a060e54bf8d69288fbee4904",
            run_cmd_fn=fake_runner,
        )
        self.assertEqual(sim["status"], "PASSED")
        self.assertEqual(sim["commit_sha"], "deadbeef1234")
        self.assertEqual(
            sim["tree_sha"], "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        )
        self.assertIn("Presubmit checks passed", sim["presubmit_summary"])
        self.assertIn("12/12 tests passed", sim["autotest_summary"])
        self.assertEqual(len(calls), 2)

        receipt = trajectory_import.ComputeAuthorReviewReceipt(
            turns=[
                trajectory_import.TrajectoryTurn(
                    turn_index=1,
                    step_index=0,
                    created_at="",
                    user_prompt="p1",
                    planner_summary="s1",
                    short_title="t1",
                    modified_files=["src/foo.cc"],
                ),
                trajectory_import.TrajectoryTurn(
                    turn_index=2,
                    step_index=2,
                    created_at="",
                    user_prompt="p2",
                    planner_summary="s2",
                    short_title="t2",
                    modified_files=["src/foo_unittest.cc"],
                ),
            ],
            total_affected_files=["src/foo.cc", "src/foo_unittest.cc"],
            gerrit_turns=1,
            checkins_acknowledged=2,
        )
        self.assertEqual(receipt["score"], 1.0)

        meta = trajectory_import.BuildSelfReviewMetadata(
            turn_index=2,
            rationale_scope="milestone_promoted",
            trajectory_id="conv-123",
            self_review_host="https://chromium-internal-review.googlesource.com",
            self_review_change_num=700001,
            peer_review_change_num=700002,
            promoted_tree_sha="4b825dc642cb6eb9a060e54bf8d69288fbee4904",
            forced_promotion=False,
            simulate_cq=sim,
            check_in_prompts=[],
            author_review_receipt=receipt,
        )
        self.assertEqual(
            meta["self_review_host"],
            "https://chromium-internal-review.googlesource.com",
        )
        self.assertEqual(meta["self_review_change_num"], 700001)
        self.assertEqual(meta["peer_review_change_num"], 700002)
        self.assertEqual(
            meta["promoted_tree_sha"],
            "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
        )
        self.assertFalse(meta["forced_promotion"])
        self.assertEqual(meta["simulate_cq"]["status"], "PASSED")
        self.assertEqual(
            meta["simulate_cq"]["tree_sha"],
            "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
        )
        self.assertEqual(meta["author_review_receipt"]["score"], 1.0)

    def testVerifyPromotionReadinessGateAndForcePenalty(self):
        tree_sha = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        unready_meta = trajectory_import.BuildSelfReviewMetadata(
            turn_index=2,
            rationale_scope="milestone_promoted",
            trajectory_id="conv-gate",
            self_review_host="https://chromium-internal-review.googlesource.com",
            self_review_change_num=700001,
            simulate_cq={
                "status": "FAILED",
                "tree_sha": tree_sha,
            },
            check_in_prompts=[
                {
                    "id": "checkin-1",
                    "path": "src/foo.cc",
                    "start_line": 1,
                    "end_line": 5,
                    "prompt": "Confirm lifetime",
                    "acknowledged": False,
                }
            ],
            author_review_receipt={
                "score": 0.95,
                "local_turns": 2,
                "gerrit_turns": 1,
                "verified_files_count": 1,
                "total_files_count": 1,
                "checkins_acknowledged": 0,
                "checkins_total": 1,
            },
        )

        # 1. Without --force, VerifyPromotionReadiness must raise PromotionReadinessError
        with self.assertRaises(trajectory_import.PromotionReadinessError) as ctx:
            trajectory_import.VerifyPromotionReadiness(
                unready_meta, current_tree_sha=tree_sha, force=False
            )
        self.assertIn("checkin-1", str(ctx.exception))
        self.assertIn("Simulate CQ status is 'FAILED'", str(ctx.exception))

        # 2. With --force, VerifyPromotionReadiness succeeds, sets forced_promotion=True,
        # and caps score at 0.65.
        passed, forced, reasons = trajectory_import.VerifyPromotionReadiness(
            unready_meta, current_tree_sha=tree_sha, force=True
        )
        self.assertFalse(passed)
        self.assertTrue(forced)
        self.assertEqual(len(reasons), 2)
        self.assertTrue(unready_meta["forced_promotion"])
        self.assertEqual(unready_meta["promoted_tree_sha"], tree_sha)
        self.assertEqual(
            unready_meta["author_review_receipt"]["score"],
            trajectory_import.FORCED_PROMOTION_SCORE_CAP,
        )

        # 3. When 100% of check-ins are acknowledged and Simulate CQ passed at tree_sha,
        # promotion succeeds cleanly with forced_promotion=False and full score.
        ready_meta = trajectory_import.BuildSelfReviewMetadata(
            turn_index=2,
            rationale_scope="milestone_promoted",
            trajectory_id="conv-gate",
            self_review_host="https://chromium-internal-review.googlesource.com",
            self_review_change_num=700001,
            simulate_cq={
                "status": "PASSED",
                "tree_sha": tree_sha,
            },
            check_in_prompts=[
                {
                    "id": "checkin-1",
                    "path": "src/foo.cc",
                    "start_line": 1,
                    "end_line": 5,
                    "prompt": "Confirm lifetime",
                    "acknowledged": True,
                }
            ],
            author_review_receipt={
                "score": 0.95,
                "local_turns": 2,
                "gerrit_turns": 1,
                "verified_files_count": 1,
                "total_files_count": 1,
                "checkins_acknowledged": 1,
                "checkins_total": 1,
            },
        )
        passed, forced, reasons = trajectory_import.VerifyPromotionReadiness(
            ready_meta, current_tree_sha=tree_sha, force=False
        )
        self.assertTrue(passed)
        self.assertFalse(forced)
        self.assertEqual(reasons, [])
        self.assertFalse(ready_meta["forced_promotion"])
        self.assertEqual(ready_meta["author_review_receipt"]["score"], 0.95)

    def testOptInDraftReplyStagingSyncAndArchive(self):
        drafts = [
            {
                "path": "src/foo.cc",
                "line": 14,
                "in_reply_to": "comment-uuid-1",
                "message": "Done: switched to WeakPtr.",
            }
        ]
        with mock.patch("gerrit_util.CreateDraft") as mock_draft:
            # Default (stage_enabled=False): zero drafts staged on CL_peer!
            count_off = trajectory_import.StageDraftRepliesOnPeerCl(
                host="chromium-review.googlesource.com",
                peer_change_num="6100205",
                revision="2",
                draft_replies=drafts,
                stage_enabled=False,
            )
            self.assertEqual(count_off, 0)
            mock_draft.assert_not_called()

            # Opt-in (stage_enabled=True): stages unpublished draft reply on CL_peer
            count_on = trajectory_import.StageDraftRepliesOnPeerCl(
                host="chromium-review.googlesource.com",
                peer_change_num="6100205",
                revision="2",
                draft_replies=drafts,
                stage_enabled=True,
            )
            self.assertEqual(count_on, 1)
            mock_draft.assert_called_once()
            self.assertEqual(
                mock_draft.call_args[1]["body"]["in_reply_to"],
                "comment-uuid-1",
            )

        with mock.patch(
            "gerrit_util.GetChangeComments",
            return_value={
                "src/foo.cc": [
                    {
                        "id": "c-human",
                        "line": 14,
                        "patch_set": 1,
                        "unresolved": True,
                        "is_ai": False,
                        "author": {"email": "reviewer@chromium.org"},
                        "message": "Please use WeakPtr here.",
                    },
                    {
                        "id": "c-ai",
                        "line": 20,
                        "patch_set": 1,
                        "unresolved": True,
                        "is_ai": True,
                        "message": "AI note",
                    },
                ]
            },
        ):
            synced = trajectory_import.SyncPeerCommentsToSelfReview(
                peer_host="chromium-review.googlesource.com",
                peer_change_num="6100205",
            )
            self.assertEqual(len(synced), 1)
            self.assertEqual(synced[0]["id"], "c-human")

        with mock.patch("gerrit_util.AbandonChange") as mock_abandon:
            trajectory_import.ArchiveSelfReviewCl(
                self_review_host="https://chromium-internal-review.googlesource.com",
                self_review_change_num="6100101",
                peer_change_num="6100205",
            )
            mock_abandon.assert_called_once_with(
                "chromium-internal-review.googlesource.com",
                "6100101",
                msg="Archived Agentic Gerrit evidence for #6100205",
            )

    def testGitClUploadParserHasTrajectoryFlags(self):
        parser = optparse.OptionParser()
        with mock.patch.object(
            git_cl, "Changelist"
        ), mock.patch("sys.stdout"):
            git_cl.CMDupload(parser, ["--git-completion-helper"])
        opt_strings = {opt.get_opt_string() for opt in parser.option_list}
        self.assertIn("--import-trajectory", opt_strings)
        self.assertIn("--trajectory-id", opt_strings)
        self.assertIn("--promote-self-review", opt_strings)
        self.assertIn("--simulate-cq", opt_strings)
        self.assertIn("--self-review-host", opt_strings)
        self.assertIn("--stage-draft-replies", opt_strings)
        self.assertIn("--trajectory-granularity", opt_strings)
        self.assertIn("--max-self-review-patchsets", opt_strings)

    def testParseTrajectoryTurnsGranularityModesAndRmDeletion(self):
        os.makedirs(os.path.join(self.repo_root, "src"), exist_ok=True)
        coord_p = os.path.join(self.repo_root, "src", "coordinator.mm")
        med_p = os.path.join(self.repo_root, "src", "mediator.mm")
        build_p = os.path.join(self.repo_root, "src", "BUILD.gn")
        old_p = os.path.join(self.repo_root, "src", "deprecated_subpage.mm")
        test_p = os.path.join(self.repo_root, "src", "mediator_unittest.mm")

        for p, text in [
            (coord_p, "int coord = 0;\n"),
            (med_p, "int med = 0;\n"),
            (build_p, "sources = [ 'deprecated_subpage.mm' ]\n"),
            (old_p, "// deprecated\n"),
            (test_p, "int test = 0;\n"),
        ]:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        subprocess.run(["git", "add", "."], cwd=self.repo_root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "Base"], cwd=self.repo_root, check=True
        )
        base_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root, text=True
        ).strip()

        # Apply final state on branch
        with open(coord_p, "w", encoding="utf-8") as f:
            f.write("int coord = 2;\n")
        with open(med_p, "w", encoding="utf-8") as f:
            f.write("int med = 1;\n")
        with open(build_p, "w", encoding="utf-8") as f:
            f.write("sources = []\n")
        os.remove(old_p)
        with open(test_p, "w", encoding="utf-8") as f:
            f.write("int test = 1;\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo_root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "Final"], cwd=self.repo_root, check=True
        )
        head_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root, text=True
        ).strip()

        t_path = self._write_transcript(
            "conv-granularity",
            [
                {
                    "step_index": 1,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "content": "Remove deprecated subpage across coordinator, build, and tests",
                },
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "Update coordinator part 1",
                    "tool_calls": [
                        {
                            "name": "edit_file",
                            "args": {
                                "Path": coord_p,
                                "Target": "int coord = 0;",
                                "Replacement": "int coord = 1;",
                            },
                        }
                    ],
                },
                {
                    "step_index": 3,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "Update coordinator part 2 and mediator",
                    "tool_calls": [
                        {
                            "name": "edit_file",
                            "args": {
                                "Path": coord_p,
                                "Target": "int coord = 1;",
                                "Replacement": "int coord = 2;",
                            },
                        },
                        {
                            "name": "edit_file",
                            "args": {
                                "Path": med_p,
                                "Target": "int med = 0;",
                                "Replacement": "int med = 1;",
                            },
                        },
                    ],
                },
                {
                    "step_index": 4,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "Update BUILD.gn and remove deprecated_subpage.mm",
                    "tool_calls": [
                        {
                            "name": "edit_file",
                            "args": {
                                "Path": build_p,
                                "Target": "sources = [ 'deprecated_subpage.mm' ]",
                                "Replacement": "sources = []",
                            },
                        },
                        {
                            "name": "run_command",
                            "args": {
                                "CommandLine": f"rm -f {old_p}",
                                "toolSummary": "Remove deprecated_subpage.mm",
                            },
                        },
                    ],
                },
                {
                    "step_index": 5,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "Update unit tests",
                    "tool_calls": [
                        {
                            "name": "edit_file",
                            "args": {
                                "Path": test_p,
                                "Target": "int test = 0;",
                                "Replacement": "int test = 1;",
                            },
                        }
                    ],
                },
            ],
        )

        # 1. 'turn' mode: strictly 1 USER_INPUT = 1 Patchset
        turns_strict = trajectory_import.ParseTrajectoryTurns(
            t_path, repo_root=self.repo_root, granularity="turn"
        )
        self.assertEqual(len(turns_strict), 1)

        # 2. 'auto' (default) / 'phase' mode: splits single high-level user turn into
        # 3 logical phases (Core implementation -> Build & cleanup -> Unit tests)
        turns_auto = trajectory_import.ParseTrajectoryTurns(
            t_path, repo_root=self.repo_root, granularity="auto"
        )
        self.assertEqual(len(turns_auto), 3)
        self.assertIn("Core implementation", turns_auto[0].short_title)
        self.assertEqual(
            turns_auto[0].modified_files,
            ["src/coordinator.mm", "src/mediator.mm"],
        )
        self.assertIn(
            "Build targets, constants & cleanup", turns_auto[1].short_title
        )
        self.assertEqual(
            turns_auto[1].modified_files,
            ["src/BUILD.gn", "src/deprecated_subpage.mm"],
        )
        self.assertIn("Unit & integration tests", turns_auto[2].short_title)
        self.assertEqual(
            turns_auto[2].modified_files, ["src/mediator_unittest.mm"]
        )

        # ReconstructTurnTrees verifies PS2 removes deprecated_subpage.mm from tree
        snaps = trajectory_import.ReconstructTurnTrees(
            repo_root=self.repo_root,
            base_commit=base_commit,
            head_commit=head_commit,
            turns=turns_auto,
        )
        self.assertEqual(len(snaps), 3)
        ps1_files = subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", snaps[0].tree_sha],
            cwd=self.repo_root,
            text=True,
        ).splitlines()
        self.assertIn("src/deprecated_subpage.mm", ps1_files)
        ps2_files = subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", snaps[1].tree_sha],
            cwd=self.repo_root,
            text=True,
        ).splitlines()
        self.assertNotIn("src/deprecated_subpage.mm", ps2_files)

    def testEndToEndSelfReviewClUploadAndHardGatedPromoteToPeerCl(self):
        os.makedirs(os.path.join(self.repo_root, "src"), exist_ok=True)
        foo_path = os.path.join(self.repo_root, "src", "foo.cc")
        with open(foo_path, "w", encoding="utf-8") as f:
            f.write("int a = 0;\n")
        subprocess.run(["git", "add", "."], cwd=self.repo_root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "Base"], cwd=self.repo_root, check=True
        )
        base_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root, text=True
        ).strip()

        with open(foo_path, "w", encoding="utf-8") as f:
            f.write("int a = 2;\n")
        subprocess.run(["git", "add", "."], cwd=self.repo_root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "Feature"],
            cwd=self.repo_root,
            check=True,
        )
        head_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root, text=True
        ).strip()
        head_tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD:"], cwd=self.repo_root, text=True
        ).strip()

        t_path = self._write_transcript(
            "conv-e2e",
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "content": "Set a to 1",
                },
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "Set a=1",
                    "tool_calls": [
                        {
                            "name": "edit_file",
                            "args": {
                                "Path": foo_path,
                                "Target": "int a = 0;",
                                "Replacement": "int a = 1;",
                            },
                        }
                    ],
                },
                {
                    "step_index": 2,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "content": "Update a to 2",
                },
                {
                    "step_index": 3,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "Set a=2",
                    "tool_calls": [
                        {
                            "name": "edit_file",
                            "args": {
                                "Path": foo_path,
                                "Target": "int a = 1;",
                                "Replacement": "int a = 2;",
                            },
                        }
                    ],
                },
            ],
        )

        config_store = {}
        cl = mock.MagicMock()
        cl.GetBranch.return_value = "feature-branch"
        cl.GetGerritHost.return_value = "chromium-review.googlesource.com"
        cl.GetRemoteBranch.return_value = ("origin", "refs/heads/main")
        cl.GetAffectedFiles.return_value = ["src/foo.cc"]
        cl._GitGetBranchConfigValue.side_effect = lambda k: config_store.get(k)
        cl._GitSetBranchConfigValue.side_effect = (
            lambda k, v: config_store.__setitem__(k, v)
        )
        cl._RunGitPushWithTraces.return_value = (
            "remote:   https://chromium-internal-review.googlesource.com/c/chromium/src/+/888001 [NEW]\n"
        )

        canonical_id = "Iaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        change_desc = git_cl.ChangeDescription(
            f"Feature commit\n\nChange-Id: {canonical_id}\n"
        )
        new_upload = git_cl._NewUpload(
            reviewers=["rev@chromium.org"],
            ccs=["cc@chromium.org"],
            commit_to_push=head_commit,
            new_last_uploaded_commit=head_commit,
            parent=base_commit,
            change_desc=change_desc,
            prev_patchset=0,
        )
        options = optparse.Values(
            {
                "import_trajectory": True,
                "trajectory_id": t_path,
                "promote_self_review": False,
                "simulate_cq": False,
                "self_review_host": "https://chromium-internal-review.googlesource.com",
                "stage_draft_replies": False,
                "force": False,
                "target_branch": None,
                "push_options": [],
            }
        )

        def run_git_in_repo(args, **kwargs):
            return subprocess.check_output(
                ["git"] + list(args), cwd=self.repo_root, text=True
            )

        with mock.patch.object(
            git_cl.settings, "GetRoot", return_value=self.repo_root
        ), mock.patch.object(
            git_cl, "RunGit", side_effect=run_git_in_repo
        ), mock.patch.object(
            git_cl, "GetTargetRef", return_value="main"
        ), mock.patch.object(
            trajectory_import, "PublishTurnSteeringComment"
        ) as mock_steer, mock.patch.object(
            trajectory_import, "UploadRationale"
        ) as mock_rationale:
            rc = git_cl._UploadSelfReviewTrajectory(cl, new_upload, options)
            self.assertEqual(rc, 0)
            self.assertEqual(cl._RunGitPushWithTraces.call_count, 2)
            first_refspec = cl._RunGitPushWithTraces.call_args_list[0][0][0]
            self.assertIn(":refs/for/main%wip,notify=NONE,m=", first_refspec)
            self.assertEqual(
                config_store.get(
                    trajectory_import.SELF_REVIEW_ISSUE_CONFIG_KEY
                ),
                "888001",
            )
            self.assertEqual(
                config_store.get(
                    trajectory_import.SELF_REVIEW_SERVER_CONFIG_KEY
                ),
                "https://chromium-internal-review.googlesource.com",
            )
            self.assertNotIn("gerritissue", config_store)
            self.assertEqual(mock_steer.call_count, 1)
            self.assertEqual(mock_rationale.call_count, 2)

            # Promotion with --force when check-ins are not yet acked:
            options.promote_self_review = True
            options.force = True
            git_cl._PublishPeerOrSimulateCqMetadata(
                cl, new_upload, options, peer_change_num="888002"
            )
            promote_payload = mock_rationale.call_args[1]["payload"]
            meta = promote_payload["self_review_metadata"]
            self.assertEqual(meta["rationale_scope"], "milestone_promoted")
            self.assertEqual(
                meta["self_review_host"],
                "https://chromium-internal-review.googlesource.com",
            )
            self.assertEqual(meta["self_review_change_num"], 888001)
            self.assertEqual(meta["peer_review_change_num"], 888002)
            self.assertEqual(meta["promoted_tree_sha"], head_tree)
            self.assertTrue(meta["forced_promotion"])
            self.assertEqual(
                meta["author_review_receipt"]["score"],
                trajectory_import.FORCED_PROMOTION_SCORE_CAP,
            )

    def testDescribeBaseCommitAndCrossHostParentMetadata(self):
        # 1. Commit with Reviewed-on footer
        c1_file = os.path.join(self.repo_root, "base_feature.cc")
        with open(c1_file, "w", encoding="utf-8") as f:
            f.write("// base feature\n")
        subprocess.run(["git", "add", "base_feature.cc"], cwd=self.repo_root, check=True)
        msg_with_reviewed_on = (
            "[ios][passwords] Base feature\n\n"
            "Summary of base feature.\n\n"
            "Reviewed-on: https://chromium-review.googlesource.com/c/chromium/src/+/8562018\n"
            "Change-Id: If75781069b0165458b53fd21ed57be546a6a6964\n"
        )
        subprocess.run(
            ["git", "commit", "-m", msg_with_reviewed_on],
            cwd=self.repo_root,
            check=True,
        )
        c1_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root, text=True
        ).strip()

        desc1 = trajectory_import.DescribeBaseCommit(
            self.repo_root,
            c1_sha,
            default_peer_host="https://chromium-review.googlesource.com",
        )
        self.assertEqual(desc1["base_commit_sha"], c1_sha)
        self.assertEqual(desc1["base_subject"], "[ios][passwords] Base feature")
        self.assertEqual(desc1["base_peer_change_num"], 8562018)
        self.assertEqual(
            desc1["base_peer_change_url"],
            "https://chromium-review.git.corp.google.com/c/chromium/src/+/8562018",
        )

        # 2. Commit without Reviewed-on footer
        with open(c1_file, "a", encoding="utf-8") as f:
            f.write("// unreviewed edit\n")
        subprocess.run(["git", "commit", "-am", "Unreviewed local base commit"], cwd=self.repo_root, check=True)
        c2_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_root, text=True
        ).strip()

        desc2 = trajectory_import.DescribeBaseCommit(
            self.repo_root,
            c2_sha,
            default_peer_host="https://chromium-review.googlesource.com",
        )
        self.assertEqual(desc2["base_commit_sha"], c2_sha)
        self.assertEqual(desc2["base_subject"], "Unreviewed local base commit")
        self.assertIsNone(desc2["base_peer_change_num"])
        self.assertEqual(
            desc2["base_peer_change_url"],
            f"https://chromium-review.git.corp.google.com/q/{c2_sha}",
        )

        # 3. Verify cross-host parent fields in BuildSelfReviewMetadata
        meta = trajectory_import.BuildSelfReviewMetadata(
            turn_index=1,
            rationale_scope="turn_delta",
            trajectory_id="conv-cross-host",
            self_review_host="https://chrome-internal-review.git.corp.google.com",
            self_review_change_num=9991017,
            peer_review_change_num=8578423,
            peer_review_host="https://chromium-review.googlesource.com",
            base_commit_sha=c1_sha,
            base_peer_change_num=8562018,
            base_peer_change_url="https://chromium-review.git.corp.google.com/c/chromium/src/+/8562018",
        )
        self.assertEqual(
            meta["peer_review_host"],
            "https://chromium-review.googlesource.com",
        )
        self.assertEqual(meta["base_commit_sha"], c1_sha)
        self.assertEqual(meta["base_peer_change_num"], 8562018)
        self.assertEqual(
            meta["base_peer_change_url"],
            "https://chromium-review.git.corp.google.com/c/chromium/src/+/8562018",
        )


if __name__ == "__main__":
    unittest.main()
