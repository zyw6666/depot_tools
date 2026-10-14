#!/usr/bin/env vpython3
# Copyright 2024 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import gn_helper  # noqa: E402
from testing_support import trial_dir  # noqa: E402


def write(filename, content):
    """Writes the content of a file and create the directories as needed."""
    filename = os.path.abspath(filename)
    dirname = os.path.dirname(filename)
    if not os.path.isdir(dirname):
        os.makedirs(dirname)
    with open(filename, "w") as f:
        f.write(content)


class GnHelperTest(trial_dir.TestCase):
    def setUp(self):
        super().setUp()
        self.previous_dir = os.getcwd()
        os.chdir(self.root_dir)

    def tearDown(self):
        os.chdir(self.previous_dir)
        super().tearDown()

    def test_lines(self):
        write(".gn", "")
        out_dir = os.path.join("out", "dir")
        # Make sure nested import directives work. This is based on the
        # reclient test.
        write(os.path.join(out_dir, "args.gn"), 'import("//out/common.gni")')
        write(os.path.join("out", "common.gni"), 'import("common_2.gni")')
        write(os.path.join("out", "common_2.gni"), "use_remoteexec=true")

        lines = list(gn_helper.lines(out_dir))

        # The test will only pass if both imports work and
        # 'use_remoteexec=true' is seen.
        self.assertListEqual(
            lines,
            [
                "use_remoteexec=true",
            ],
        )

    def test_args(self):
        write(".gn", "")
        out_dir = os.path.join("out", "dir")
        # Make sure nested import directives work. This is based on the
        # reclient test.
        write(os.path.join(out_dir, "args.gn"), 'import("//out/common.gni")')
        write(os.path.join("out", "common.gni"), 'import("common_2.gni")')
        write(os.path.join("out", "common_2.gni"), "use_remoteexec=true")

        lines = list(gn_helper.args(out_dir))

        # The test will only pass if both imports work and
        # 'use_remoteexec=true' is seen.
        self.assertListEqual(
            lines,
            [
                ("use_remoteexec", "true"),
            ],
        )

    def test_args_spaces(self):
        write(".gn", "")
        out_dir = os.path.join("out", "dir")
        # Make sure nested import directives work. This is based on the
        # reclient test.
        write(os.path.join(out_dir, "args.gn"), 'import("//out/common.gni")')
        write(os.path.join("out", "common.gni"), 'import("common_2.gni")')
        write(os.path.join("out", "common_2.gni"), "  use_remoteexec = true  ")

        lines = list(gn_helper.args(out_dir))

        # The test will only pass if both imports work and
        # 'use_remoteexec=true' is seen.
        self.assertListEqual(
            lines,
            [
                ("use_remoteexec", "true"),
            ],
        )

    def test_import_path_traversal_rejected(self):
        checkout = os.path.join(self.root_dir, "checkout")
        out_dir = os.path.join(checkout, "out", "dir")
        write(os.path.join(checkout, ".gn"), "")
        write(os.path.join(self.root_dir, "outside.gni"), "is_debug = true")
        write(
            os.path.join(out_dir, "args.gn"),
            'import("../../../outside.gni")',
        )

        with self.assertRaisesRegex(IOError, "escapes allowed directories"):
            list(gn_helper.lines(out_dir))

    def test_import_root_traversal_rejected(self):
        checkout = os.path.join(self.root_dir, "checkout")
        out_dir = os.path.join(checkout, "out", "dir")
        write(os.path.join(checkout, ".gn"), "")
        write(os.path.join(self.root_dir, "outside.gni"), "is_debug = true")
        write(
            os.path.join(out_dir, "args.gn"),
            'import("//../outside.gni")',
        )

        with self.assertRaisesRegex(IOError, "escapes allowed directories"):
            list(gn_helper.lines(out_dir))

    @unittest.skipIf(
        sys.platform.startswith("win"), "os.symlink requires privileges on win"
    )
    def test_import_symlink_escape_rejected(self):
        checkout = os.path.join(self.root_dir, "checkout")
        out_dir = os.path.join(checkout, "out", "dir")
        write(os.path.join(checkout, ".gn"), "")
        outside_file = os.path.join(self.root_dir, "outside.gni")
        write(outside_file, "is_debug = true")
        os.symlink(outside_file, os.path.join(checkout, "escaped.gni"))
        write(os.path.join(out_dir, "args.gn"), 'import("//escaped.gni")')

        with self.assertRaisesRegex(IOError, "escapes allowed directories"):
            list(gn_helper.lines(out_dir))

    @unittest.skipIf(
        sys.platform.startswith("win"), "os.symlink requires privileges on win"
    )
    def test_import_symlink_bounce_rejected(self):
        checkout = os.path.join(self.root_dir, "checkout")
        out_dir = os.path.join(checkout, "out", "dir")
        outside_dir = os.path.join(self.root_dir, "outside")
        write(os.path.join(checkout, ".gn"), "")
        write(os.path.join(outside_dir, "secret.gni"), "is_debug = true")
        step2 = os.path.join(checkout, "step2.gni")
        write(step2, 'import("../outside/secret.gni")')
        os.symlink(step2, os.path.join(outside_dir, "bounce.gni"))
        write(
            os.path.join(out_dir, "args.gn"),
            'import("../../../outside/bounce.gni")',
        )

        with self.assertRaisesRegex(IOError, "escapes allowed directories"):
            list(gn_helper.lines(out_dir))

    @unittest.skipIf(
        sys.platform.startswith("win"), "os.symlink requires privileges on win"
    )
    def test_top_level_args_gn_symlink_escape_rejected(self):
        checkout = os.path.join(self.root_dir, "checkout")
        out_dir = os.path.join(checkout, "out", "dir")
        os.makedirs(out_dir, exist_ok=True)
        write(os.path.join(checkout, ".gn"), "")
        outside_file = os.path.join(self.root_dir, "outside.gni")
        write(outside_file, "is_debug = true")
        os.symlink(outside_file, os.path.join(out_dir, "args.gn"))

        with self.assertRaisesRegex(IOError, "escapes allowed directories"):
            list(gn_helper.lines(out_dir))

    def test_import_system_absolute_path_containment(self):
        checkout = os.path.join(self.root_dir, "checkout")
        out_dir = os.path.join(checkout, "out", "dir")
        write(os.path.join(checkout, ".gn"), "")
        inside_file = os.path.join(checkout, "inside.gni")
        outside_file = os.path.join(self.root_dir, "outside.gni")
        write(inside_file, "is_debug = true")
        write(outside_file, "is_debug = false")

        def to_gn_abs(p):
            p = os.path.abspath(p).replace("\\", "/")
            return p if p.startswith("/") else f"/{p}"

        write(
            os.path.join(out_dir, "args.gn"),
            f'import("{to_gn_abs(inside_file)}")',
        )
        self.assertListEqual(
            list(gn_helper.lines(out_dir)),
            ["is_debug = true"],
        )

        write(
            os.path.join(out_dir, "args.gn"),
            f'import("{to_gn_abs(outside_file)}")',
        )
        with self.assertRaisesRegex(IOError, "escapes allowed directories"):
            list(gn_helper.lines(out_dir))

    def test_import_cycle_rejected(self):
        write(".gn", "")
        out_dir = os.path.join("out", "dir")
        write(os.path.join(out_dir, "args.gn"), 'import("//out/a.gni")')
        write(os.path.join("out", "a.gni"), 'import("//out/b.gni")')
        write(os.path.join("out", "b.gni"), 'import("//out/a.gni")')

        with self.assertRaisesRegex(IOError, "Recursive import cycle"):
            list(gn_helper.lines(out_dir))

    def test_import_diamond_allowed(self):
        write(".gn", "")
        out_dir = os.path.join("out", "dir")
        write(
            os.path.join(out_dir, "args.gn"),
            'import("//out/a.gni")\nimport("//out/b.gni")\n',
        )
        write(os.path.join("out", "a.gni"), 'import("//out/common.gni")\n')
        write(os.path.join("out", "b.gni"), 'import("//out/common.gni")\n')
        write(os.path.join("out", "common.gni"), "is_debug = true\n")

        self.assertListEqual(
            [line.strip() for line in gn_helper.lines(out_dir) if line.strip()],
            ["is_debug = true", "is_debug = true"],
        )


if __name__ == "__main__":
    unittest.main()
