#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import io
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import roll_downstream_deps  # noqa: E402

SOURCE_DEPS = """
vars = {
  'android_ndk_version': Str('2@30.0.14608247'),
  'chromium_git': 'https://chromium.googlesource.com',
  'clang_format_revision': '9f796802e5d633c96dbe97e0b6683d5e62594f9f',
  'gn_version': 'git_revision:5964f499767097d81dbe034e8b541c3988168073',
  'googletest_revision': '4fe3307fb2d9f86d19777c7eb0e4809e9694dde7',
  'harfbuzz_revision': '31695252eb6ed25096893aec7f848889dad874bc',
  'siso_version': 'git_revision:49dcca5d2be985d8ac6d512e59ee59e315264fb8',
}

deps = {
  'src/buildtools/linux64': {
    'packages': [
      {
        'package': 'gn/gn/linux-${{arch}}',
        'version': Var('gn_version'),
      }
    ],
    'dep_type': 'cipd',
    'condition': 'host_os == "linux"',
  },
  'src/third_party/clang-format/script':
    Var('chromium_git') +
        '/external/github.com/llvm/llvm-project/clang/tools/clang-format.git@' +
        Var('clang_format_revision'),
  'src/third_party/data_sharing_sdk': {
    'packages': [
      {
        'package': 'chrome_internal/third_party/google3/data_sharing_sdk',
        'version': 'NQq9pR3VrvsqPtQSfrezFe8hL-z_SfZsONkwC-MqX6wC',
      },
    ],
    'condition': 'checkout_src_internal',
    'dep_type': 'cipd',
  },
  'src/third_party/googletest/src':
    Var('chromium_git') + '/external/github.com/google/googletest.git@' +
        Var('googletest_revision'),
  'src/third_party/harfbuzz-ng/src':
    Var('chromium_git') + '/external/github.com/harfbuzz/harfbuzz.git@' +
        Var('harfbuzz_revision'),
  'src/third_party/icu':
    Var('chromium_git') +
        '/chromium/deps/icu.git@a86a32e67b8d1384b33f8fa48c83a6079b86f8cd',
  'src/third_party/libjpeg_turbo': {
    'url': Var('chromium_git') +
        '/chromium/deps/libjpeg_turbo.git@6383cf609c1f63c18af0f59b2738caa0c6c7e379',
  },
}
"""

DESTINATION_DEPS = """
vars = {
  'abseil_revision': '56722f9eb6f40fb1a140caffd36e8f1f61b57aaf',
  'android_ndk_version': Str('2@30.0.10000000'),
  'checkout_android': False,
  'chromium_git': 'https://chromium.googlesource.com',
  'clang_format_revision': '9f796802e5d633c96dbe97e0b6683d5e62594f9f',
  'data_sharing_version': 'old_cipd_version',
  'gn_version': 'git_revision:841e072bef419ae1116477dc75e8c5a662c0212c',
  'gtest_revision': '1111111111111111111111111111111111111111',
  'harfbuzz_revision': 'ff36703b95013ec1d437e6038e1aa7c881275f4e',
  'icu_revision': '5aa526207171ecadf31597f0980a7b7ad8872f33',
  'jpeg_turbo_revision': '640f254ad0fa03f6b1f29f89b7dd9366f2f6e533',
  'siso_version': 'git_revision:49dcca5d2be985d8ac6d512e59ee59e315264fb8',
  'unused_revision': 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef',
  'unmatched_cipd_version': 'git_revision:0000000000000000000000000000000000000000',
}

deps = {
  'buildtools/linux64': {
    'packages': [
      {
        'package': 'gn/gn/linux-${{arch}}',
        'version': Var('gn_version'),
      }
    ],
    'dep_type': 'cipd',
    'condition': 'host_os == "linux"',
  },
  'third_party/abseil-cpp':
    Var('chromium_git') + '/chromium/src/third_party/abseil-cpp.git@' +
        Var('abseil_revision'),
  'third_party/android_toolchain/ndk': {
    'packages': [
      {
        'package': 'chromium/third_party/android_toolchain/android_toolchain/${{platform}}',
        'version': 'version:' + Var('android_ndk_version'),
      },
    ],
    'condition': 'checkout_android',
    'dep_type': 'cipd',
  },
  'third_party/clang-format/script':
    Var('chromium_git') +
        '/external/github.com/llvm/llvm-project/clang/tools/clang-format.git@' +
        Var('clang_format_revision'),
  'third_party/data_sharing_sdk': {
    'packages': [
      {
        'package': 'chrome_internal/third_party/google3/data_sharing_sdk',
        'version': Var('data_sharing_version'),
      },
    ],
    'dep_type': 'cipd',
  },
  'third_party/googletest/src':
    Var('chromium_git') + '/external/github.com/google/googletest.git@' +
        Var('gtest_revision'),
  'third_party/harfbuzz/src':
    Var('chromium_git') + '/external/github.com/harfbuzz/harfbuzz.git@' +
        Var('harfbuzz_revision'),
  'third_party/icu':
    Var('chromium_git') + '/chromium/deps/icu.git@' + Var('icu_revision'),
  'third_party/libjpeg_turbo':
    Var('chromium_git') + '/chromium/deps/libjpeg_turbo.git@' +
        Var('jpeg_turbo_revision'),
  'third_party/siso/cipd': {
    'packages': [
      {
        'package': 'build/siso/${{platform}}',
        'version': Var('siso_version'),
      }
    ],
    'dep_type': 'cipd',
  },
  'third_party/unmatched_cipd': {
    'packages': [
      {
        'package': 'some/unmatched/package',
        'version': Var('unmatched_cipd_version'),
      }
    ],
    'dep_type': 'cipd',
  },
}
"""


class RollDownstreamDepsTest(unittest.TestCase):
    def testInvalidDepsAndEntries(self):
        with self.assertRaisesRegex(
            roll_downstream_deps.Error, "No vars or deps found"
        ):
            roll_downstream_deps.get_dep_updates(
                "", DESTINATION_DEPS, ["harfbuzz_revision"]
            )

        with self.assertRaisesRegex(
            roll_downstream_deps.Error,
            'Entry "nonexistent" not found in destination DEPS.',
        ):
            roll_downstream_deps.get_dep_updates(
                SOURCE_DEPS, DESTINATION_DEPS, ["nonexistent"]
            )

        with self.assertRaisesRegex(
            roll_downstream_deps.Error,
            'Could not find path for var "checkout_android" in destination DEPS.',
        ):
            roll_downstream_deps.get_dep_updates(
                SOURCE_DEPS, DESTINATION_DEPS, ["checkout_android"]
            )

        with self.assertRaisesRegex(
            roll_downstream_deps.Error,
            'Could not find path for var "unused_revision" in destination DEPS.',
        ):
            roll_downstream_deps.get_dep_updates(
                SOURCE_DEPS, DESTINATION_DEPS, ["unused_revision"]
            )

        with self.assertRaisesRegex(
            roll_downstream_deps.Error,
            'Entry "abseil_revision" not found in source DEPS.',
        ):
            roll_downstream_deps.get_dep_updates(
                SOURCE_DEPS, DESTINATION_DEPS, ["abseil_revision"]
            )

        with self.assertRaisesRegex(
            roll_downstream_deps.Error,
            'CIPD entry "unmatched_cipd_version" not found in source DEPS.',
        ):
            roll_downstream_deps.get_dep_updates(
                SOURCE_DEPS, DESTINATION_DEPS, ["unmatched_cipd_version"]
            )

    def testGitDepsResolution(self):
        updates = roll_downstream_deps.get_dep_updates(
            SOURCE_DEPS,
            DESTINATION_DEPS,
            [
                "harfbuzz_revision",
                "gtest_revision",
                "icu_revision",
                "jpeg_turbo_revision",
                "clang_format_revision",
            ],
        )
        self.assertEqual(
            updates,
            [
                roll_downstream_deps.DepUpdate(
                    var_name="harfbuzz_revision",
                    deps_path="third_party/harfbuzz/src",
                    dest_revision="ff36703b95013ec1d437e6038e1aa7c881275f4e",
                    source_revision="31695252eb6ed25096893aec7f848889dad874bc",
                    is_cipd=False,
                ),
                roll_downstream_deps.DepUpdate(
                    var_name="gtest_revision",
                    deps_path="third_party/googletest/src",
                    dest_revision="1111111111111111111111111111111111111111",
                    source_revision="4fe3307fb2d9f86d19777c7eb0e4809e9694dde7",
                    is_cipd=False,
                ),
                roll_downstream_deps.DepUpdate(
                    var_name="icu_revision",
                    deps_path="third_party/icu",
                    dest_revision="5aa526207171ecadf31597f0980a7b7ad8872f33",
                    source_revision="a86a32e67b8d1384b33f8fa48c83a6079b86f8cd",
                    is_cipd=False,
                ),
                roll_downstream_deps.DepUpdate(
                    var_name="jpeg_turbo_revision",
                    deps_path="third_party/libjpeg_turbo",
                    dest_revision="640f254ad0fa03f6b1f29f89b7dd9366f2f6e533",
                    source_revision="6383cf609c1f63c18af0f59b2738caa0c6c7e379",
                    is_cipd=False,
                ),
                roll_downstream_deps.DepUpdate(
                    var_name="clang_format_revision",
                    deps_path="third_party/clang-format/script",
                    dest_revision="9f796802e5d633c96dbe97e0b6683d5e62594f9f",
                    source_revision="9f796802e5d633c96dbe97e0b6683d5e62594f9f",
                    is_cipd=False,
                ),
            ],
        )

    def testCipdDepsResolution(self):
        updates = roll_downstream_deps.get_dep_updates(
            SOURCE_DEPS,
            DESTINATION_DEPS,
            [
                "gn_version",
                "android_ndk_version",
                "data_sharing_version",
                "siso_version",
            ],
        )
        self.assertEqual(
            updates,
            [
                roll_downstream_deps.DepUpdate(
                    var_name="gn_version",
                    deps_path="buildtools/linux64",
                    dest_revision="git_revision:841e072bef419ae1116477dc75e8c5a662c0212c",
                    source_revision="git_revision:5964f499767097d81dbe034e8b541c3988168073",
                    is_cipd=True,
                ),
                roll_downstream_deps.DepUpdate(
                    var_name="android_ndk_version",
                    deps_path="third_party/android_toolchain/ndk",
                    dest_revision="2@30.0.10000000",
                    source_revision="2@30.0.14608247",
                    is_cipd=True,
                ),
                roll_downstream_deps.DepUpdate(
                    var_name="data_sharing_version",
                    deps_path="third_party/data_sharing_sdk",
                    dest_revision="old_cipd_version",
                    source_revision="NQq9pR3VrvsqPtQSfrezFe8hL-z_SfZsONkwC-MqX6wC",
                    is_cipd=True,
                ),
                roll_downstream_deps.DepUpdate(
                    var_name="siso_version",
                    deps_path="third_party/siso/cipd",
                    dest_revision="git_revision:49dcca5d2be985d8ac6d512e59ee59e315264fb8",
                    source_revision="git_revision:49dcca5d2be985d8ac6d512e59ee59e315264fb8",
                    is_cipd=True,
                ),
            ],
        )

    def testDuplicateRevisionInDestVarsUsesCorrectVarAst(self):
        source = """
vars = {
  'chromium_git': 'https://chromium.googlesource.com',
  'foo_revision': '2222222222222222222222222222222222222222',
  'bar_revision': '3333333333333333333333333333333333333333',
}
deps = {}
"""
        dest = """
vars = {
  'chromium_git': 'https://chromium.googlesource.com',
  'foo_revision': '1111111111111111111111111111111111111111',
  'bar_revision': '1111111111111111111111111111111111111111',
}
deps = {
  'third_party/foo': Var('chromium_git') + '/foo.git@' + Var('foo_revision'),
  'third_party/bar': Var('chromium_git') + '/bar.git@' + Var('bar_revision'),
}
"""
        updates = roll_downstream_deps.get_dep_updates(
            source, dest, ["bar_revision"]
        )
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].deps_path, "third_party/bar")
        self.assertEqual(
            updates[0].source_revision,
            "3333333333333333333333333333333333333333",
        )

    def testUpdateCipdVarsPreservesFormatting(self):
        dest = """# Header comment
vars = {
  'android_ndk_version': Str('2@30.0.10000000'),
  'gn_version': 'git_revision:old',
}
deps = {}
"""
        expected = """# Header comment
vars = {
  'android_ndk_version': Str('2@30.0.14608247'),
  'gn_version': 'git_revision:new',
}
deps = {}
"""
        updated = roll_downstream_deps.update_cipd_vars(
            dest,
            {
                "android_ndk_version": "2@30.0.14608247",
                "gn_version": "git_revision:new",
            },
        )
        self.assertEqual(updated, expected)

    def testGenerateCipdCommitMessage(self):
        single_msg = roll_downstream_deps.generate_cipd_commit_message(
            {"gn_version": "git_revision:12345"},
            bug="123456",
        )
        self.assertEqual(
            single_msg,
            "Roll gn_version to git_revision:12345\n\n"
            "Created with:\n"
            "  roll_downstream_deps.py --var gn_version\n\n"
            "Bug: 123456\n",
        )

        multi_msg = roll_downstream_deps.generate_cipd_commit_message(
            {
                "gn_version": "git_revision:12345",
                "siso_version": "git_revision:67890",
            },
        )
        self.assertEqual(
            multi_msg,
            "Rolling 2 dependencies\n\n"
            "Roll gn_version to git_revision:12345\n\n"
            "Roll siso_version to git_revision:67890\n\n"
            "Created with:\n"
            "  roll_downstream_deps.py --var gn_version --var siso_version\n",
        )

    @mock.patch("roll_downstream_deps.subprocess2.check_call")
    def testMainRollsGitAndCipdDeps(self, mock_check_call):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_file = os.path.join(tmpdir, "SOURCE_DEPS")
            dest_file = os.path.join(tmpdir, "DEPS")
            with open(source_file, "w") as f:
                f.write(SOURCE_DEPS)
            with open(dest_file, "w") as f:
                f.write(DESTINATION_DEPS)

            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                ret = roll_downstream_deps.main(
                    [
                        "--source-deps",
                        source_file,
                        "--destination-deps",
                        dest_file,
                        "--var",
                        "harfbuzz_revision",
                        "--var",
                        "icu_revision",
                        "--var",
                        "gn_version",
                        "--var",
                        "clang_format_revision",
                        "--ignore-dirty-tree",
                        "--no-log",
                    ]
                )

            self.assertEqual(ret, 0)
            expected_commit_msg = (
                "Roll gn_version to "
                "git_revision:5964f499767097d81dbe034e8b541c3988168073\n\n"
                "Created with:\n"
                "  roll_downstream_deps.py --var gn_version\n"
            )
            self.assertEqual(
                mock_check_call.call_args_list,
                [
                    mock.call(
                        [
                            sys.executable,
                            roll_downstream_deps.ROLL_DEP_PATH,
                            "third_party/harfbuzz/src",
                            "--roll-to",
                            "31695252eb6ed25096893aec7f848889dad874bc",
                            "--ignore-dirty-tree",
                            "--no-log",
                        ],
                        cwd=tmpdir,
                    ),
                    mock.call(
                        [
                            sys.executable,
                            roll_downstream_deps.ROLL_DEP_PATH,
                            "third_party/icu",
                            "--roll-to",
                            "a86a32e67b8d1384b33f8fa48c83a6079b86f8cd",
                            "--ignore-dirty-tree",
                            "--no-log",
                        ],
                        cwd=tmpdir,
                    ),
                    mock.call(
                        ["git", "add", "DEPS"],
                        cwd=tmpdir,
                    ),
                    mock.call(
                        ["git", "commit", "--quiet", "-m", expected_commit_msg],
                        cwd=tmpdir,
                    ),
                ],
            )
            self.assertIn(
                "clang_format_revision: Revisions are the same.",
                stdout.getvalue(),
            )
            self.assertIn(
                "Roll gn_version to "
                "git_revision:5964f499767097d81dbe034e8b541c3988168073",
                stdout.getvalue(),
            )
            with open(dest_file, "r") as f:
                updated_dest = f.read()
            self.assertIn(
                "'gn_version': 'git_revision:5964f499767097d81dbe034e8b541c3988168073'",
                updated_dest,
            )


if __name__ == "__main__":
    level = logging.DEBUG if "-v" in sys.argv else logging.FATAL
    logging.basicConfig(
        level=level,
        format="%(asctime).19s %(levelname)s %(filename)s:"
        "%(lineno)s %(message)s",
    )
    unittest.main()
