#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Unit tests for modular luci-test-results scripts."""

import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_test
import fetch_log
import find_cl_builds
import list_failures
import luci_client
import monitor
import test_history

SAMPLE_BUILD_ID = "8000000000000000001"
SAMPLE_BUILD_ID_SUCCESS = "8000000000000000002"
SAMPLE_BUILD_ID_2 = "8000000000000000003"
SAMPLE_BUILD_ID_3 = "8000000000000000004"
SAMPLE_BUILD_NUM = 12345
SAMPLE_BUILDER = "android-13-x64-rel"
SAMPLE_CL = "1234567"
SAMPLE_TASK_ID = "chromium-swarm.appspot.com-sampletaskid123"
SAMPLE_TEST_QUERY = "JSONReaderTest"
SAMPLE_TEST_ID = (
    "://base\\:base_unittests!gtest::JSONReaderTest#ASCIIControlCodes"
)
SAMPLE_RESULT_NAME = (
    f"invocations/task-{SAMPLE_TASK_ID}/tests/"
    ":%2F%2Fbase%5C:base_unittests%21gtest::"
    "JSONReaderTest%23ASCIIControlCodes/results/sample-result-id"
)


class TestLuciScripts(unittest.TestCase):
    def setUp(self):
        super().setUp()
        find_cl_builds._PATCHSET_CACHE.clear()

    @mock.patch("luci_client.subprocess.Popen")
    def test_run_prpc_success(self, mock_popen):
        mock_proc = mock.MagicMock()
        mock_proc.communicate.return_value = ('{"foo": "bar"}', "")
        mock_proc.returncode = 0
        mock_popen.return_value.__enter__.return_value = mock_proc

        res = luci_client.run_prpc("service.example.com", "Method", {"k": "v"})
        self.assertEqual(res, {"foo": "bar"})

    @mock.patch("sys.stderr", new_callable=io.StringIO)
    @mock.patch("luci_client.subprocess.Popen")
    def test_run_prpc_error(self, mock_popen, mock_stderr):
        mock_proc = mock.MagicMock()
        mock_proc.communicate.return_value = ("", "rpc error")
        mock_proc.returncode = 1
        mock_popen.return_value.__enter__.return_value = mock_proc

        res = luci_client.run_prpc("service.example.com", "Method", {"k": "v"})
        self.assertIsNone(res)

    @mock.patch("luci_client.subprocess.run")
    def test_is_authenticated(self, mock_run):
        mock_run.return_value.returncode = 0
        self.assertTrue(luci_client.is_authenticated())

        mock_run.return_value.returncode = 1
        self.assertFalse(luci_client.is_authenticated())

        mock_run.side_effect = FileNotFoundError()
        self.assertFalse(luci_client.is_authenticated())

    @mock.patch("luci_client.run_prpc")
    def test_resolve_build_id(self, mock_prpc):
        mock_prpc.return_value = {"id": SAMPLE_BUILD_ID_SUCCESS}
        build_id = luci_client.resolve_build_id(
            "chromium", "ci", SAMPLE_BUILDER, SAMPLE_BUILD_NUM
        )
        self.assertEqual(build_id, SAMPLE_BUILD_ID_SUCCESS)
        mock_prpc.assert_called_once_with(
            "cr-buildbucket.appspot.com",
            "buildbucket.v2.Builds.GetBuild",
            {
                "builder": {
                    "project": "chromium",
                    "bucket": "ci",
                    "builder": SAMPLE_BUILDER,
                },
                "buildNumber": SAMPLE_BUILD_NUM,
            },
        )

    @mock.patch("luci_client.run_prpc")
    def test_get_build(self, mock_prpc):
        mock_prpc.return_value = {
            "id": SAMPLE_BUILD_ID_SUCCESS,
            "status": "SUCCESS",
        }
        data = luci_client.get_build(f"b{SAMPLE_BUILD_ID_SUCCESS}")
        self.assertEqual(data["status"], "SUCCESS")
        mock_prpc.assert_called_once_with(
            "cr-buildbucket.appspot.com",
            "buildbucket.v2.Builds.GetBuild",
            {
                "id": SAMPLE_BUILD_ID_SUCCESS,
                "mask": {
                    "fields": "id,builder,number,status,summaryMarkdown,output"
                },
            },
        )

    @mock.patch("find_cl_builds.run_prpc")
    def test_find_cl_builds_with_patchset(self, mock_prpc):
        mock_prpc.return_value = {
            "builds": [
                {
                    "builder": {"builder": "linux-rel"},
                    "status": "FAILURE",
                    "id": SAMPLE_BUILD_ID,
                },
                {
                    "builder": {"builder": "mac-rel"},
                    "status": "STARTED",
                    "id": SAMPLE_BUILD_ID_2,
                },
                {
                    "builder": {"builder": "android-arm64-rel"},
                    "status": "SUCCESS",
                    "id": SAMPLE_BUILD_ID_3,
                },
            ]
        }
        failed = find_cl_builds.find_cl_builds(SAMPLE_CL, patchset=1)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["builder"], "linux-rel")

        all_builds = find_cl_builds.find_cl_builds(
            SAMPLE_CL, patchset=1, show_all=True
        )
        self.assertEqual(len(all_builds), 3)

    @mock.patch("find_cl_builds.subprocess.check_output")
    @mock.patch("find_cl_builds.run_prpc")
    def test_find_cl_builds_default_patchset(
        self, mock_prpc, mock_check_output
    ):
        gerrit_json = (
            ")]}'\n"
            '{"current_revision": "rev1", "revisions": {"rev1": {"_number": 3}}}'
        )
        mock_check_output.return_value = gerrit_json.encode("utf-8")
        mock_prpc.return_value = {
            "builds": [
                {
                    "builder": {"builder": "linux-rel"},
                    "status": "FAILURE",
                    "id": SAMPLE_BUILD_ID,
                }
            ]
        }
        builds = find_cl_builds.find_cl_builds(SAMPLE_CL)
        self.assertEqual(len(builds), 1)
        mock_prpc.assert_called_once_with(
            "cr-buildbucket.appspot.com",
            "buildbucket.v2.Builds.SearchBuilds",
            {
                "predicate": {
                    "gerritChanges": [
                        {
                            "host": "chromium-review.googlesource.com",
                            "change": int(SAMPLE_CL),
                            "patchset": 3,
                        }
                    ]
                }
            },
        )

    @mock.patch("list_failures.run_prpc")
    def test_list_failures_grouped_by_task(self, mock_prpc):
        mock_prpc.return_value = {
            "testVariants": [
                {
                    "testId": SAMPLE_TEST_ID,
                    "status": "UNEXPECTED",
                    "results": [
                        {
                            "result": {
                                "name": SAMPLE_RESULT_NAME,
                                "status": "FAIL",
                                "failureReason": {
                                    "primaryErrorMessage": "AssertionError"
                                },
                            }
                        }
                    ],
                },
                {
                    "testId": "ninja://test/Class#methodB",
                    "status": "FLAKY",
                    "results": [
                        {
                            "result": {
                                "name": f"invocations/task-{SAMPLE_TASK_ID}/tests/testB/results/1",
                                "status": "FAIL",
                            }
                        }
                    ],
                },
                {
                    "testId": "ninja://test/Class#methodC",
                    "status": "EXONERATED",
                    "results": [
                        {
                            "result": {
                                "name": f"invocations/task-{SAMPLE_TASK_ID}/tests/testC/results/1",
                                "status": "FAIL",
                            }
                        }
                    ],
                },
            ]
        }

        # Default excludes EXONERATED, includes FLAKY and UNEXPECTED
        res = list_failures.list_failures(f"b{SAMPLE_BUILD_ID}")
        self.assertIn(SAMPLE_TASK_ID, res)
        self.assertEqual(len(res[SAMPLE_TASK_ID]), 2)
        self.assertEqual(res[SAMPLE_TASK_ID][0]["id"], SAMPLE_TEST_ID)
        self.assertEqual(res[SAMPLE_TASK_ID][0]["err"], "AssertionError")

        # Ignore flaky
        res_no_flake = list_failures.list_failures(
            SAMPLE_BUILD_ID, ignore_flaky=True
        )
        self.assertEqual(len(res_no_flake[SAMPLE_TASK_ID]), 1)
        self.assertEqual(res_no_flake[SAMPLE_TASK_ID][0]["id"], SAMPLE_TEST_ID)

        # Include exonerated
        res_exonerated = list_failures.list_failures(
            SAMPLE_BUILD_ID, include_exonerated=True
        )
        self.assertEqual(len(res_exonerated[SAMPLE_TASK_ID]), 3)

        # Limit results
        res_limit = list_failures.list_failures(SAMPLE_BUILD_ID, limit=1)
        self.assertEqual(len(res_limit[SAMPLE_TASK_ID]), 1)

    @mock.patch("list_failures.run_prpc")
    def test_list_failures_pagination(self, mock_prpc):
        mock_prpc.side_effect = [
            {
                "testVariants": [
                    {
                        "testId": "ninja://test/Class#method1",
                        "status": "UNEXPECTED",
                        "results": [
                            {
                                "result": {
                                    "name": "invocations/task-task1/tests/test1/results/1",
                                    "status": "FAIL",
                                }
                            }
                        ],
                    }
                ],
                "nextPageToken": "token123",
            },
            {
                "testVariants": [
                    {
                        "testId": "ninja://test/Class#method2",
                        "status": "UNEXPECTED",
                        "results": [
                            {
                                "result": {
                                    "name": "invocations/task-task2/tests/test2/results/1",
                                    "status": "FAIL",
                                }
                            }
                        ],
                    }
                ]
            },
        ]
        res = list_failures.list_failures(SAMPLE_BUILD_ID)
        self.assertEqual(len(res), 2)
        self.assertIn("task1", res)
        self.assertIn("task2", res)

    @mock.patch("fetch_log.subprocess.check_output")
    @mock.patch("fetch_log.run_prpc")
    def test_fetch_log_snippet_regex(self, mock_prpc, mock_check_output):
        mock_prpc.return_value = {
            "artifacts": [
                {
                    "artifactId": "test_log",
                    "fetchUrl": "https://logs.example.com/log.txt",
                }
            ]
        }
        sample_log = (
            "[ RUN      ] SomeTest.Foo\n"
            "Line 2\n"
            "org.junit.ComparisonFailure: expected:<[a]> but was:<[b]>\n"
            "  at java.lang.AssertionError\n"
            "[  FAILED  ] SomeTest.Foo\n"
        )
        mock_check_output.return_value = sample_log.encode("utf-8")

        output = fetch_log.fetch_log_snippet(SAMPLE_RESULT_NAME)
        self.assertIn("[ RUN      ]", output)

    @mock.patch("fetch_log.subprocess.check_output")
    @mock.patch("fetch_log.run_prpc")
    def test_fetch_log_snippet_raw(self, mock_prpc, mock_check_output):
        mock_prpc.return_value = {
            "artifacts": [
                {
                    "artifactId": "stdout",
                    "fetchUrl": "https://logs.example.com/stdout.txt",
                }
            ]
        }
        sample_log = "Raw log output without matches"
        mock_check_output.return_value = sample_log.encode("utf-8")

        output = fetch_log.fetch_log_snippet(SAMPLE_RESULT_NAME, raw=True)
        self.assertEqual(output, sample_log)

    @mock.patch("fetch_log.is_authenticated")
    @mock.patch("fetch_log.run_prpc")
    def test_fetch_log_snippet_no_artifacts(
        self, mock_prpc, mock_is_authenticated
    ):
        mock_is_authenticated.return_value = False

        for empty_val in ({}, {"artifacts": []}, None):
            mock_prpc.return_value = empty_val
            output = fetch_log.fetch_log_snippet(
                "invocations/task-123/tests/foo/results/1"
            )
            self.assertIn("No artifacts found", output)
            self.assertIn("bb auth-login", output)

    @mock.patch("check_test.run_prpc")
    def test_check_test(self, mock_prpc):
        mock_prpc.return_value = {
            "testResults": [
                {
                    "name": SAMPLE_RESULT_NAME,
                    "testId": SAMPLE_TEST_ID,
                    "status": "FAIL",
                    "failureReason": {
                        "primaryErrorMessage": "Expected 1 to equal 2"
                    },
                }
            ]
        }
        res = check_test.check_test(f"b{SAMPLE_BUILD_ID}", SAMPLE_TEST_QUERY)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "FAIL")
        self.assertFalse(res[0]["expected"])
        self.assertEqual(res[0]["res"], SAMPLE_RESULT_NAME)
        self.assertEqual(res[0]["err"], "Expected 1 to equal 2")
        mock_prpc.assert_called_once_with(
            "results.api.luci.app",
            "luci.resultdb.v1.ResultDB.QueryTestResults",
            {
                "invocations": [f"invocations/build-{SAMPLE_BUILD_ID}"],
                "predicate": {
                    "testIdRegexp": f".*{SAMPLE_TEST_QUERY}.*",
                    "expectancy": "ALL",
                },
                "pageSize": 1000,
            },
        )

    @mock.patch("fetch_log.fetch_log_snippet")
    def test_fetch_log_helper_alias(self, mock_snippet):
        mock_snippet.return_value = "snippet-text"
        self.assertEqual(
            fetch_log.fetch_log(SAMPLE_RESULT_NAME, raw=True), "snippet-text"
        )
        mock_snippet.assert_called_once_with(SAMPLE_RESULT_NAME, raw=True)

    @mock.patch("test_history.run_prpc")
    def test_test_history(self, mock_prpc):
        mock_prpc.return_value = {
            "verdicts": [
                {
                    "testId": SAMPLE_TEST_ID,
                    "status": "EXPECTED",
                }
            ]
        }
        verdicts = test_history.test_history(
            "chromium",
            SAMPLE_TEST_ID,
            limit=5,
            builder=SAMPLE_BUILDER,
            test_suite="base_unittests",
        )
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["status"], "EXPECTED")
        mock_prpc.assert_called_once_with(
            "analysis.api.luci.app",
            "luci.analysis.v1.TestHistory.Query",
            {
                "project": "chromium",
                "testId": SAMPLE_TEST_ID,
                "predicate": {
                    "variantPredicate": {
                        "contains": {
                            "def": {
                                "builder": SAMPLE_BUILDER,
                                "test_suite": "base_unittests",
                            }
                        }
                    }
                },
                "pageSize": 5,
            },
        )

    @mock.patch("test_history.run_prpc")
    def test_test_history_pagination(self, mock_prpc):
        mock_prpc.side_effect = [
            {
                "verdicts": [
                    {"testId": SAMPLE_TEST_ID, "status": "UNEXPECTED"}
                ],
                "nextPageToken": "page2",
            },
            {
                "verdicts": [{"testId": SAMPLE_TEST_ID, "status": "EXPECTED"}],
            },
        ]
        verdicts = test_history.test_history(
            "chromium", SAMPLE_TEST_ID, limit=2
        )
        self.assertEqual(len(verdicts), 2)
        self.assertEqual(mock_prpc.call_count, 2)

    @mock.patch("test_history.run_prpc")
    def test_query_variant_builders_and_format_summary(self, mock_prpc):
        mock_prpc.return_value = {
            "variants": [
                {
                    "variantHash": "hash1",
                    "variant": {"def": {"bucket": "ci", "builder": "bot-fail"}},
                },
                {
                    "variantHash": "hash2",
                    "variant": {"def": {"bucket": "ci", "builder": "bot-pass"}},
                },
            ]
        }
        mapping = test_history.query_variant_builders(
            "chromium", SAMPLE_TEST_ID
        )
        self.assertEqual(mapping["hash1"], "ci/bot-fail")

        verdicts = [
            {
                "variantHash": "hash1",
                "status": "UNEXPECTED",
                "partitionTime": "2026-08-25T12:00:00Z",
            },
            {
                "variantHash": "hash1",
                "status": "EXPECTED",
                "partitionTime": "2026-08-25T10:00:00Z",
            },
            {
                "variantHash": "hash2",
                "status": "EXPECTED",
                "partitionTime": "2026-08-25T11:00:00Z",
            },
        ]
        summary = test_history.format_summary(
            verdicts, variant_builders=mapping
        )
        self.assertIn("Builder: ci/bot-fail | Pass: 1, Fail: 1", summary)
        self.assertIn("Recent (newest->oldest): FP", summary)
        self.assertIn(
            "100% Passing Builders (1): ci/bot-pass (1 pass)", summary
        )

    @mock.patch("test_history.run_prpc")
    def test_query_tests(self, mock_prpc):
        mock_prpc.return_value = {"testIds": [SAMPLE_TEST_ID]}
        candidates = test_history.query_tests("chromium", SAMPLE_TEST_QUERY)
        self.assertEqual(candidates, [SAMPLE_TEST_ID])
        mock_prpc.assert_called_once_with(
            "analysis.api.luci.app",
            "luci.analysis.v1.TestHistory.QueryTests",
            {"project": "chromium", "testIdSubstring": SAMPLE_TEST_QUERY},
        )

    @mock.patch("test_history.run_prpc")
    def test_fetch_multi_builder_history(self, mock_prpc):
        def side_effect(service, method, payload):
            if method == "luci.analysis.v1.TestHistory.QueryVariants":
                return {
                    "variants": [
                        {
                            "variantHash": "h1",
                            "variant": {
                                "def": {"bucket": "ci", "builder": "b1"}
                            },
                        },
                        {
                            "variantHash": "h2",
                            "variant": {
                                "def": {"bucket": "ci", "builder": "b2"}
                            },
                        },
                    ]
                }
            return {
                "verdicts": [
                    {
                        "testId": SAMPLE_TEST_ID,
                        "status": "EXPECTED",
                        "variantHash": "h1",
                    }
                ]
            }

        mock_prpc.side_effect = side_effect
        verdicts, mapping = test_history.fetch_multi_builder_history(
            "chromium", SAMPLE_TEST_ID, per_builder_limit=5
        )
        self.assertEqual(len(verdicts), 2)
        self.assertEqual(mapping["h1"], "ci/b1")
        self.assertEqual(mapping["h2"], "ci/b2")

    @mock.patch("test_history.run_prpc")
    def test_query_variant_builders_uses_variant_predicate(self, mock_prpc):
        mock_prpc.return_value = {"variants": []}
        pred = {"variantPredicate": {"contains": {"def": {"bucket": "ci"}}}}
        test_history.query_variant_builders(
            "chromium", SAMPLE_TEST_ID, predicate=pred
        )
        mock_prpc.assert_called_once_with(
            "analysis.api.luci.app",
            "luci.analysis.v1.TestHistory.QueryVariants",
            {
                "project": "chromium",
                "testId": SAMPLE_TEST_ID,
                "pageSize": 1000,
                "variantPredicate": {"contains": {"def": {"bucket": "ci"}}},
            },
        )

    @mock.patch("sys.stderr", new_callable=io.StringIO)
    @mock.patch("test_history.run_prpc")
    def test_fetch_multi_builder_history_truncation_notice(
        self, mock_prpc, mock_stderr
    ):
        def side_effect(service, method, payload):
            if method == "luci.analysis.v1.TestHistory.QueryVariants":
                return {
                    "variants": [
                        {
                            "variantHash": f"h{i}",
                            "variant": {
                                "def": {"bucket": "ci", "builder": f"b{i}"}
                            },
                        }
                        for i in range(3)
                    ]
                }
            return {"verdicts": []}

        mock_prpc.side_effect = side_effect
        test_history.fetch_multi_builder_history(
            "chromium", SAMPLE_TEST_ID, max_builders=2
        )
        self.assertIn(
            "Notice: Querying 2 of 3 discovered builders",
            mock_stderr.getvalue(),
        )

    @mock.patch("sys.stderr", new_callable=io.StringIO)
    @mock.patch("test_history.test_history")
    @mock.patch("test_history.query_variant_builders")
    def test_fetch_multi_builder_history_handles_worker_exception(
        self, mock_variants, mock_history, mock_stderr
    ):
        mock_variants.return_value = {"h1": "ci/b1", "h2": "ci/b2"}

        def history_side_effect(project, test_id, **kwargs):
            if kwargs.get("builder") == "b1":
                raise RuntimeError("Transient RPC failure")
            return [
                {
                    "testId": SAMPLE_TEST_ID,
                    "status": "EXPECTED",
                    "variantHash": "h2",
                }
            ]

        mock_history.side_effect = history_side_effect
        verdicts, _ = test_history.fetch_multi_builder_history(
            "chromium", SAMPLE_TEST_ID
        )
        self.assertEqual(len(verdicts), 1)
        self.assertIn(
            "Warning: Failed to fetch history", mock_stderr.getvalue()
        )

    def test_format_summary_includes_daily_skip_count(self):
        verdicts = [
            {
                "variantHash": "h1",
                "status": "SKIPPED",
                "partitionTime": "2026-08-25T12:00:00Z",
            }
        ]
        summary = test_history.format_summary(
            verdicts, default_builder="ci/bot-skip"
        )
        self.assertIn("Skip=1", summary)

    @mock.patch("sys.stdout", new_callable=io.StringIO)
    @mock.patch("sys.stderr", new_callable=io.StringIO)
    @mock.patch("test_history.query_tests")
    @mock.patch("test_history.test_history")
    def test_main_empty_verdicts_raw_and_self_filter(
        self, mock_history, mock_query_tests, mock_stderr, mock_stdout
    ):
        mock_history.return_value = []
        # 1. --raw should print "[]" and not exit with error
        with mock.patch(
            "sys.argv",
            [
                "test_history.py",
                "--test-id",
                SAMPLE_TEST_ID,
                "--builder",
                "b1",
                "--raw",
            ],
        ):
            test_history.main()
        self.assertEqual(mock_stdout.getvalue().strip(), "[]")

        # 2. Non-raw should filter out args.test_id from suggestions
        mock_query_tests.return_value = [SAMPLE_TEST_ID, "other_candidate_id"]
        with mock.patch(
            "sys.argv",
            [
                "test_history.py",
                "--test-id",
                SAMPLE_TEST_ID,
                "--builder",
                "b1",
            ],
        ):
            with self.assertRaises(SystemExit):
                test_history.main()
        stderr_out = mock_stderr.getvalue()
        self.assertIn("other_candidate_id", stderr_out)
        self.assertNotIn(f"  {SAMPLE_TEST_ID}", stderr_out)

    @mock.patch("sys.stderr", new_callable=io.StringIO)
    @mock.patch("find_cl_builds.subprocess.check_output")
    @mock.patch("find_cl_builds.run_prpc")
    def test_find_cl_builds_fallback_patchset(
        self, mock_prpc, mock_check_output, mock_stderr
    ):
        gerrit_json = (
            ")]}'\n"
            '{"current_revision": "rev4", "revisions": {"rev4": {"_number": 4}}}'
        )
        mock_check_output.return_value = gerrit_json.encode("utf-8")
        mock_prpc.side_effect = [
            # PS 4 (post-submit revision) has 0 builds
            {"builds": []},
            # PS 3 has trybot builds
            {
                "builds": [
                    {
                        "builder": {"builder": "linux-rel"},
                        "status": "FAILURE",
                        "id": SAMPLE_BUILD_ID,
                    }
                ]
            },
        ]
        builds = find_cl_builds.find_cl_builds(SAMPLE_CL)
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0]["builder"], "linux-rel")
        self.assertEqual(builds[0]["patchset"], 3)
        self.assertEqual(mock_prpc.call_count, 2)
        self.assertIn(
            "Notice: No builds on patchset 4; using patchset 3.",
            mock_stderr.getvalue(),
        )

    @mock.patch("find_cl_builds.subprocess.check_output")
    @mock.patch("find_cl_builds.run_prpc")
    def test_find_cl_builds_rpc_failure_no_fallback(
        self, mock_prpc, mock_check_output
    ):
        gerrit_json = (
            ")]}'\n"
            '{"current_revision": "rev4", "revisions": {"rev4": {"_number": 4}}}'
        )
        mock_check_output.return_value = gerrit_json.encode("utf-8")
        mock_prpc.return_value = None
        builds = find_cl_builds.find_cl_builds(SAMPLE_CL)
        self.assertEqual(builds, [])
        # Should stop immediately on None rather than looping across patchsets
        self.assertEqual(mock_prpc.call_count, 1)

    @mock.patch("sys.stdout", new_callable=io.StringIO)
    @mock.patch("sys.stderr", new_callable=io.StringIO)
    @mock.patch("test_history.query_tests")
    @mock.patch("test_history.test_history")
    def test_main_substring_resolution(
        self, mock_history, mock_query_tests, mock_stderr, mock_stdout
    ):
        # Prefix-overlapping and non-delimiter suffix candidates:
        # bounded suffix match (preceded by ':') should win.
        prefix_overlap_id = f"{SAMPLE_TEST_ID}NoEmptyElements"
        non_delim_suffix_id = "://base\\:base_unittests!gtest::OtherJSONReaderTest#ASCIIControlCodes"
        mock_query_tests.return_value = [
            prefix_overlap_id,
            non_delim_suffix_id,
            SAMPLE_TEST_ID,
        ]
        mock_history.return_value = [
            {"testId": SAMPLE_TEST_ID, "status": "EXPECTED"}
        ]
        with mock.patch(
            "sys.argv",
            [
                "test_history.py",
                "--test-id",
                "JSONReaderTest#ASCIIControlCodes",
                "--builder",
                SAMPLE_BUILDER,
                "--raw",
            ],
        ):
            test_history.main()
        mock_history.assert_called_once_with(
            "chromium",
            SAMPLE_TEST_ID,
            limit=test_history.DEFAULT_SINGLE_BUILDER_LIMIT,
            builder=SAMPLE_BUILDER,
            bucket=None,
            device_os=None,
            device_type=None,
            os_val=None,
            test_suite=None,
        )

        # Unresolved substring should exit early with code 1 without calling test_history
        mock_history.reset_mock()
        mock_query_tests.return_value = []
        with mock.patch(
            "sys.argv",
            [
                "test_history.py",
                "--test-substring",
                "NonExistentTest#foo",
                "--raw",
            ],
        ):
            with self.assertRaises(SystemExit) as cm:
                test_history.main()
            self.assertEqual(cm.exception.code, 1)
        mock_history.assert_not_called()

        # Passing both --test-id and --test-substring is rejected by mutually exclusive group
        with mock.patch(
            "sys.argv",
            [
                "test_history.py",
                "--test-id",
                SAMPLE_TEST_ID,
                "--test-substring",
                "JSONReaderTest",
            ],
        ):
            with self.assertRaises(SystemExit) as cm:
                test_history.main()
            self.assertEqual(cm.exception.code, 2)

    def test_parse_cl_arg(self):
        self.assertEqual(
            find_cl_builds.parse_cl_arg(None),
            (None, None, None),
        )
        self.assertEqual(
            find_cl_builds.parse_cl_arg("not-a-cl"),
            (None, None, None),
        )
        self.assertEqual(
            find_cl_builds.parse_cl_arg("8260911"),
            (None, "8260911", None),
        )
        self.assertEqual(
            find_cl_builds.parse_cl_arg(8260911),
            (None, "8260911", None),
        )
        self.assertEqual(
            find_cl_builds.parse_cl_arg(
                "https://chromium-review.googlesource.com/c/chromium/src/+/8260911/3"
            ),
            ("chromium-review.googlesource.com", "8260911", 3),
        )
        self.assertEqual(
            find_cl_builds.parse_cl_arg(
                "https://chromium-review.googlesource.com/#/c/chromium/src/+/8260911/4"
            ),
            ("chromium-review.googlesource.com", "8260911", 4),
        )
        self.assertEqual(
            find_cl_builds.parse_cl_arg(
                "https://chromium-review.git.corp.google.com/c/chromium/src/+/8260911"
            ),
            ("chromium-review.googlesource.com", "8260911", None),
        )
        self.assertEqual(
            find_cl_builds.parse_cl_arg("https://crrev.com/c/8260911/2"),
            (None, "8260911", 2),
        )
        self.assertEqual(
            find_cl_builds.parse_cl_arg("https://crrev.com/i/8260911/2"),
            ("chrome-internal-review.googlesource.com", "8260911", 2),
        )

    @mock.patch("find_cl_builds.subprocess.check_output")
    def test_get_current_branch_cl(self, mock_check_output):
        mock_check_output.return_value = b"Issue number: 8260911 (https://chromium-review.googlesource.com/8260911)\n"
        self.assertEqual(find_cl_builds.get_current_branch_cl(), "8260911")

        mock_check_output.return_value = b"Issue number: None (None)\n"
        self.assertIsNone(find_cl_builds.get_current_branch_cl())

        mock_check_output.side_effect = (
            find_cl_builds.subprocess.CalledProcessError(
                1, ["git", "cl", "issue"]
            )
        )
        self.assertIsNone(find_cl_builds.get_current_branch_cl())

    @mock.patch("sys.stderr", new_callable=io.StringIO)
    @mock.patch("find_cl_builds.time.sleep")
    @mock.patch("find_cl_builds.subprocess.check_output")
    def test_get_latest_patchset_retry_and_cache(
        self, mock_check_output, mock_sleep, mock_stderr
    ):
        gerrit_json = (
            ")]}'\n"
            '{"current_revision": "rev5", "revisions": {"rev5": {"_number": 5}}}'
        )
        mock_check_output.side_effect = [
            find_cl_builds.subprocess.CalledProcessError(1, ["curl"]),
            gerrit_json.encode("utf-8"),
        ]
        ps = find_cl_builds.get_latest_patchset(SAMPLE_CL, retries=3)
        self.assertEqual(ps, 5)
        self.assertEqual(mock_check_output.call_count, 2)
        mock_sleep.assert_called_once()

        # Subsequent full failure falls back to cached patchset
        mock_check_output.side_effect = (
            find_cl_builds.subprocess.CalledProcessError(1, ["curl"])
        )
        ps_cached = find_cl_builds.get_latest_patchset(
            SAMPLE_CL, retries=2, retry_delay=0
        )
        self.assertEqual(ps_cached, 5)

        # Uncached CL with empty or failing Gerrit response returns None and logs stderr
        find_cl_builds._PATCHSET_CACHE.clear()
        mock_check_output.side_effect = None
        mock_check_output.return_value = b")]}'\n"
        self.assertIsNone(
            find_cl_builds.get_latest_patchset(
                "9999999", retries=1, retry_delay=0
            )
        )
        self.assertIn(
            "Error getting latest patchset for CL 9999999",
            mock_stderr.getvalue(),
        )

    @mock.patch("find_cl_builds.subprocess.check_output")
    @mock.patch("find_cl_builds.run_prpc")
    def test_find_cl_builds_deduplicates_retries_and_merges_reused(
        self, mock_prpc, mock_check_output
    ):
        gerrit_json = (
            ")]}'\n"
            '{"current_revision": "rev2", "revisions": {"rev2": {"_number": 2}}}'
        )
        mock_check_output.return_value = gerrit_json.encode("utf-8")
        ps2_payload = {
            "builds": [
                {
                    "builder": {"builder": "chromium_presubmit"},
                    "status": "SUCCESS",
                    "id": "8000000000000000001",
                }
            ]
        }
        ps1_payload = {
            "builds": [
                # linux-rel has older failed attempt 0009, newer passed attempt 0002
                {
                    "builder": {"builder": "linux-rel"},
                    "status": "FAILURE",
                    "id": "8000000000000000009",
                },
                {
                    "builder": {"builder": "linux-rel"},
                    "status": "SUCCESS",
                    "id": "8000000000000000002",
                },
                # mac-rel failed on PS 1; LUCI CV does not reuse failed builds across patchsets
                {
                    "builder": {"builder": "mac-rel"},
                    "status": "FAILURE",
                    "id": "8000000000000000008",
                },
            ]
        }
        mock_prpc.side_effect = [ps2_payload, ps1_payload]
        builds = find_cl_builds.find_cl_builds(
            SAMPLE_CL, show_all=True, latest_only=True, merge_reused=True
        )
        by_builder = {b["builder"]: b for b in builds}
        self.assertEqual(len(by_builder), 2)
        self.assertEqual(by_builder["chromium_presubmit"]["patchset"], 2)
        self.assertEqual(by_builder["linux-rel"]["status"], "SUCCESS")
        self.assertEqual(by_builder["linux-rel"]["id"], "8000000000000000002")
        self.assertEqual(by_builder["linux-rel"]["patchset"], 1)
        self.assertNotIn("mac-rel", by_builder)

        # Default show_all=False after retry deduplication omits builders whose
        # newest retry succeeded
        mock_prpc.side_effect = [ps1_payload]
        failed_only = find_cl_builds.find_cl_builds(
            SAMPLE_CL, patchset=1, show_all=False, latest_only=True
        )
        self.assertEqual(len(failed_only), 1)
        self.assertEqual(failed_only[0]["builder"], "mac-rel")

        # latest_only=False (--all-attempts) preserves both attempts of linux-rel
        mock_prpc.side_effect = [ps1_payload]
        all_attempts = find_cl_builds.find_cl_builds(
            SAMPLE_CL, patchset=1, show_all=True, latest_only=False
        )
        self.assertEqual(len(all_attempts), 3)

    @mock.patch("sys.stderr", new_callable=io.StringIO)
    @mock.patch("find_cl_builds.get_latest_patchset")
    @mock.patch("find_cl_builds.run_prpc")
    def test_find_cl_builds_url_errors_and_boundaries(
        self, mock_prpc, mock_get_ps, mock_stderr
    ):
        # URL decomposition with explicit patchset and merge_reused=True
        mock_prpc.side_effect = [
            {
                "builds": [
                    {
                        "builder": {"builder": "chromium_presubmit"},
                        "status": "SUCCESS",
                        "id": "8000000000000000001",
                    }
                ]
            },
            {
                "builds": [
                    {
                        "builder": {"builder": f"bot-{i}"},
                        "status": "SUCCESS",
                        "id": f"800000000000000001{i}",
                    }
                    for i in range(6)
                ]
            },
        ]
        builds = find_cl_builds.find_cl_builds(
            "https://chromium-review.git.corp.google.com/c/chromium/src/+/8260911/3",
            show_all=True,
            merge_reused=True,
        )
        self.assertEqual(len(builds), 7)
        # Stops after PS 2 because PS 2 had 6 (> 5) unique builders
        self.assertEqual(mock_prpc.call_count, 2)
        mock_get_ps.assert_not_called()

        # When current_ps has > 5 unique builders, merge_reused does not query prior patchsets
        mock_prpc.reset_mock()
        mock_prpc.side_effect = [
            {
                "builds": [
                    {
                        "builder": {"builder": f"bot-{i}"},
                        "status": "SUCCESS",
                        "id": f"800000000000000002{i}",
                    }
                    for i in range(6)
                ]
            }
        ]
        builds_full = find_cl_builds.find_cl_builds(
            SAMPLE_CL, patchset=3, show_all=True, merge_reused=True
        )
        self.assertEqual(len(builds_full), 6)
        self.assertEqual(mock_prpc.call_count, 1)

        # return_none_on_error=True returns None on Gerrit failure, PRPC failure, or invalid input
        mock_get_ps.return_value = None
        self.assertIsNone(
            find_cl_builds.find_cl_builds(SAMPLE_CL, return_none_on_error=True)
        )
        self.assertEqual(
            find_cl_builds.find_cl_builds(
                SAMPLE_CL, return_none_on_error=False
            ),
            [],
        )
        self.assertIsNone(
            find_cl_builds.find_cl_builds(
                "invalid-cl", return_none_on_error=True
            )
        )
        self.assertIsNone(
            find_cl_builds.find_cl_builds(
                SAMPLE_CL, patchset="bad", return_none_on_error=True
            )
        )

        # RPC failure on a prior patchset during merge_reused returns None when return_none_on_error=True
        mock_prpc.reset_mock()
        mock_prpc.side_effect = [
            {
                "builds": [
                    {
                        "builder": {"builder": "chromium_presubmit"},
                        "status": "SUCCESS",
                        "id": "8000000000000000001",
                    }
                ]
            },
            None,
        ]
        self.assertIsNone(
            find_cl_builds.find_cl_builds(
                SAMPLE_CL,
                patchset=2,
                show_all=True,
                merge_reused=True,
                return_none_on_error=True,
            )
        )

    @mock.patch("sys.stderr", new_callable=io.StringIO)
    @mock.patch("sys.stdout", new_callable=io.StringIO)
    @mock.patch("find_cl_builds.find_cl_builds")
    @mock.patch("find_cl_builds.get_current_branch_cl")
    def test_find_cl_builds_main(
        self, mock_branch_cl, mock_find_builds, mock_stdout, mock_stderr
    ):
        mock_find_builds.return_value = [{"builder": "b1", "status": "FAILURE"}]
        with mock.patch(
            "sys.argv",
            [
                "find_cl_builds.py",
                "--cl",
                SAMPLE_CL,
                "--patchset",
                "2",
                "--all",
                "--all-attempts",
                "--merge-reused",
            ],
        ):
            find_cl_builds.main()
        mock_find_builds.assert_called_once_with(
            SAMPLE_CL,
            patchset=2,
            host=None,
            show_all=True,
            latest_only=False,
            merge_reused=True,
        )

        # Auto-detects branch CL when --cl is omitted, and exits 2 when none exists
        mock_branch_cl.return_value = None
        with mock.patch("sys.argv", ["find_cl_builds.py"]):
            with self.assertRaises(SystemExit) as cm:
                find_cl_builds.main()
            self.assertEqual(cm.exception.code, 2)

    @mock.patch("sys.stdout", new_callable=io.StringIO)
    @mock.patch("monitor.find_cl_builds")
    def test_monitor_cls_exit_codes_and_json(
        self, mock_find_builds, mock_stdout
    ):
        # All jobs passed -> exit code 0; verify find_cl_builds call contract and URL parsing
        mock_find_builds.return_value = [
            {
                "builder": "linux-rel",
                "status": "SUCCESS",
                "id": SAMPLE_BUILD_ID_SUCCESS,
                "patchset": 2,
            }
        ]
        self.assertEqual(
            monitor.monitor_cls(
                [
                    "https://chromium-review.googlesource.com/c/chromium/src/+/8260911/2"
                ]
            ),
            monitor.EXIT_ALL_PASSED,
        )
        mock_find_builds.assert_called_once_with(
            "8260911",
            patchset=2,
            host="chromium-review.googlesource.com",
            show_all=True,
            latest_only=True,
            merge_reused=True,
            return_none_on_error=True,
        )
        self.assertIn("Result: ALL JOBS PASSED", mock_stdout.getvalue())

        # Some jobs running (STARTED / SCHEDULED) -> exit code 2
        mock_stdout.truncate(0)
        mock_stdout.seek(0)
        mock_find_builds.return_value = [
            {
                "builder": "linux-rel",
                "status": "SUCCESS",
                "id": SAMPLE_BUILD_ID_SUCCESS,
                "patchset": 1,
            },
            {
                "builder": "mac-rel",
                "status": "SCHEDULED",
                "id": SAMPLE_BUILD_ID_2,
                "patchset": 1,
            },
        ]
        self.assertEqual(
            monitor.monitor_cls([SAMPLE_CL]), monitor.EXIT_STILL_RUNNING
        )
        self.assertIn(
            f"  - mac-rel: SCHEDULED ({SAMPLE_BUILD_ID_2})",
            mock_stdout.getvalue(),
        )
        self.assertNotIn("  - linux-rel:", mock_stdout.getvalue())

        # Some jobs failed (FAILURE / INFRA_FAILURE / CANCELED) -> exit code 1
        for failed_status in ("FAILURE", "INFRA_FAILURE", "CANCELED"):
            mock_stdout.truncate(0)
            mock_stdout.seek(0)
            mock_find_builds.return_value = [
                {
                    "builder": "linux-rel",
                    "status": failed_status,
                    "id": SAMPLE_BUILD_ID,
                    "patchset": 1,
                },
                {
                    "builder": "mac-rel",
                    "status": "STARTED",
                    "id": SAMPLE_BUILD_ID_2,
                    "patchset": 1,
                },
            ]
            self.assertEqual(
                monitor.monitor_cls([SAMPLE_CL]), monitor.EXIT_SOME_FAILED
            )
            self.assertIn(
                f"  - linux-rel: {failed_status} ({SAMPLE_BUILD_ID})",
                mock_stdout.getvalue(),
            )

        # Unknown build status -> UNKNOWN state and exit code 3, with builder
        # breakdown printed
        mock_stdout.truncate(0)
        mock_stdout.seek(0)
        mock_find_builds.return_value = [
            {
                "builder": "weird-bot",
                "status": "STATUS_UNSPECIFIED",
                "id": SAMPLE_BUILD_ID,
                "patchset": 1,
            }
        ]
        self.assertEqual(monitor.monitor_cls([SAMPLE_CL]), monitor.EXIT_ERROR)
        self.assertIn(
            f"  - weird-bot: STATUS_UNSPECIFIED ({SAMPLE_BUILD_ID})",
            mock_stdout.getvalue(),
        )

        # No jobs, RPC error, empty list, or unparsable CL -> exit code 3
        mock_stdout.truncate(0)
        mock_stdout.seek(0)
        mock_find_builds.return_value = []
        self.assertEqual(monitor.monitor_cls([SAMPLE_CL]), monitor.EXIT_ERROR)
        self.assertIn("NO JOBS", mock_stdout.getvalue())

        mock_stdout.truncate(0)
        mock_stdout.seek(0)
        mock_find_builds.return_value = None
        self.assertEqual(monitor.monitor_cls([SAMPLE_CL]), monitor.EXIT_ERROR)
        self.assertIn("ERROR", mock_stdout.getvalue())

        self.assertEqual(monitor.monitor_cls([]), monitor.EXIT_ERROR)

        mock_find_builds.reset_mock()
        self.assertEqual(
            monitor.monitor_cls(["not-a-valid-cl"]), monitor.EXIT_ERROR
        )
        mock_find_builds.assert_not_called()

        # Multi-CL aggregation precedence:
        # PASSED + PASSED -> 0; PASSED + RUNNING -> 2; RUNNING + FAILED -> 1;
        # RUNNING + NO_JOBS -> 2; PASSED + NO_JOBS -> 3
        passed_job = [
            {
                "builder": "b1",
                "status": "SUCCESS",
                "id": SAMPLE_BUILD_ID_SUCCESS,
                "patchset": 1,
            }
        ]
        running_job = [
            {
                "builder": "b1",
                "status": "STARTED",
                "id": SAMPLE_BUILD_ID_2,
                "patchset": 1,
            }
        ]
        failed_job = [
            {
                "builder": "b1",
                "status": "FAILURE",
                "id": SAMPLE_BUILD_ID,
                "patchset": 1,
            }
        ]
        mock_find_builds.side_effect = [passed_job, passed_job]
        self.assertEqual(
            monitor.monitor_cls(["111", "222"]), monitor.EXIT_ALL_PASSED
        )
        mock_find_builds.side_effect = [passed_job, running_job]
        self.assertEqual(
            monitor.monitor_cls(["111", "222"]), monitor.EXIT_STILL_RUNNING
        )
        mock_find_builds.side_effect = [running_job, failed_job]
        self.assertEqual(
            monitor.monitor_cls(["111", "222"]), monitor.EXIT_SOME_FAILED
        )
        mock_find_builds.side_effect = [running_job, []]
        self.assertEqual(
            monitor.monitor_cls(["111", "222"]), monitor.EXIT_STILL_RUNNING
        )
        mock_find_builds.side_effect = [passed_job, []]
        self.assertEqual(
            monitor.monitor_cls(["111", "222"]), monitor.EXIT_ERROR
        )

        # JSON output mode
        mock_stdout.truncate(0)
        mock_stdout.seek(0)
        mock_find_builds.side_effect = None
        mock_find_builds.return_value = passed_job
        code = monitor.monitor_cls([SAMPLE_CL], as_json=True)
        self.assertEqual(code, monitor.EXIT_ALL_PASSED)
        payload = json.loads(mock_stdout.getvalue())
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["result"], "ALL JOBS PASSED")
        self.assertEqual(payload["cls"][0]["status"], "PASSED")

    @mock.patch("sys.stderr", new_callable=io.StringIO)
    @mock.patch("sys.stdout", new_callable=io.StringIO)
    @mock.patch("monitor.monitor_cls")
    @mock.patch("monitor.get_current_branch_cl")
    def test_monitor_main_auto_detects_branch_cl(
        self, mock_branch_cl, mock_monitor_cls, mock_stdout, mock_stderr
    ):
        mock_branch_cl.return_value = SAMPLE_CL
        mock_monitor_cls.return_value = 0
        with mock.patch("sys.argv", ["monitor.py"]):
            with self.assertRaises(SystemExit) as cm:
                monitor.main()
            self.assertEqual(cm.exception.code, 0)
        mock_monitor_cls.assert_called_once_with(
            [SAMPLE_CL], patchset=None, host=None, as_json=False
        )
        self.assertIn(
            f"Monitoring active branch CL: {SAMPLE_CL}", mock_stdout.getvalue()
        )

        # When --json is passed with auto-detected CL, stdout should not have the banner
        mock_stdout.truncate(0)
        mock_stdout.seek(0)
        mock_monitor_cls.reset_mock()
        with mock.patch(
            "sys.argv", ["monitor.py", "--json", "--patchset", "3"]
        ):
            with self.assertRaises(SystemExit) as cm:
                monitor.main()
            self.assertEqual(cm.exception.code, 0)
        mock_monitor_cls.assert_called_once_with(
            [SAMPLE_CL], patchset=3, host=None, as_json=True
        )
        self.assertEqual(mock_stdout.getvalue(), "")

        # When no CL is passed and no branch CL exists -> exits with EXIT_ERROR (3)
        mock_branch_cl.return_value = None
        mock_monitor_cls.reset_mock()
        with mock.patch("sys.argv", ["monitor.py"]):
            with self.assertRaises(SystemExit) as cm:
                monitor.main()
            self.assertEqual(cm.exception.code, monitor.EXIT_ERROR)
        mock_monitor_cls.assert_not_called()
        self.assertIn("No CLs specified", mock_stderr.getvalue())

        # When --json is passed and no branch CL exists -> prints valid JSON and
        # exits 3
        mock_stdout.truncate(0)
        mock_stdout.seek(0)
        with mock.patch("sys.argv", ["monitor.py", "--json"]):
            with self.assertRaises(SystemExit) as cm:
                monitor.main()
            self.assertEqual(cm.exception.code, monitor.EXIT_ERROR)
        err_payload = json.loads(mock_stdout.getvalue())
        self.assertEqual(err_payload["exit_code"], monitor.EXIT_ERROR)
        self.assertEqual(err_payload["cls"], [])

        # When explicit positional CLs and --host are passed -> skips
        # get_current_branch_cl() and forwards arguments
        mock_branch_cl.reset_mock()
        mock_monitor_cls.reset_mock()
        mock_monitor_cls.return_value = 0
        with mock.patch(
            "sys.argv",
            ["monitor.py", "111", "222", "--host", "custom-host"],
        ):
            with self.assertRaises(SystemExit) as cm:
                monitor.main()
            self.assertEqual(cm.exception.code, 0)
        mock_branch_cl.assert_not_called()
        mock_monitor_cls.assert_called_once_with(
            ["111", "222"], patchset=None, host="custom-host", as_json=False
        )


if __name__ == "__main__":
    unittest.main()
