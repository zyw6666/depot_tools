#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Unit tests for deps_format.py."""

import os
import sys
import textwrap
import unittest
import warnings

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import deps_format  # noqa: E402


def _Dedent(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


class FormatDepsTest(unittest.TestCase):
    def testFormats(self):
        cases = [
            (
                "sorts by path ignoring the prefix",
                """
                include_rules = [
                  "+services",
                  "-content",
                  "+content/public",
                  "+base",
                ]
                """,
                """
                include_rules = [
                  "+base",
                  "-content",
                  "+content/public",
                  "+services",
                ]
                """,
            ),
            (
                "comments move with the rule below them",
                """
                include_rules = [  # Keep this comment.
                  # About z.
                  # More about z.
                  "+z",
                  "+a",  # About a.
                  # Trailing comment.
                ]
                """,
                """
                include_rules = [  # Keep this comment.
                  "+a",  # About a.
                  # About z.
                  # More about z.
                  "+z",
                  # Trailing comment.
                ]
                """,
            ),
            (
                "groups separated by blank lines are sorted on their own",
                """
                include_rules = [
                  "+d",
                  "+c",

                  # About the second group.

                  "+b",
                  "+a",
                ]
                """,
                """
                include_rules = [
                  "+c",
                  "+d",

                  # About the second group.

                  "+a",
                  "+b",
                ]
                """,
            ),
            (
                "comments followed by a blank line stay in place",
                """
                include_rules = [
                  # About the whole list.

                  # About z.
                  "+z",
                  "+a",
                  # Trailing comment.
                ]
                """,
                """
                include_rules = [
                  # About the whole list.

                  "+a",
                  # About z.
                  "+z",
                  # Trailing comment.
                ]
                """,
            ),
            (
                "duplicates in different groups are left alone",
                """
                include_rules = [
                  "+b",
                  "+a",

                  "+c",
                  "+b",
                ]
                """,
                """
                include_rules = [
                  "+a",
                  "+b",

                  "+b",
                  "+c",
                ]
                """,
            ),
            (
                "comments already on the surviving copy are not repeated",
                """
                include_rules = [
                  # About a.
                  "+a",  # For a.
                  "+b",
                  # About a.
                  "+a",  # For a.
                ]
                """,
                """
                include_rules = [
                  # About a.
                  "+a",  # For a.
                  "+b",
                ]
                """,
            ),
            (
                "repeated lines inside a moved comment are kept",
                """
                include_rules = [
                  # Para one.
                  #
                  # Para two.
                  #
                  # Para three.
                  "+a",
                  "+b",
                  "+a",  # For a.
                ]
                """,
                """
                include_rules = [
                  # Para one.
                  #
                  # Para two.
                  #
                  # Para three.
                  "+a",  # For a.
                  "+b",
                ]
                """,
            ),
            (
                "a moved inline comment is indented like its new line",
                """
                specific_include_rules = {
                  "a.cc": [
                      "+b",  # First copy.
                    "+a",
                    "+b",
                  ],
                }
                """,
                """
                specific_include_rules = {
                  "a.cc": [
                    "+a",
                    # First copy.
                    "+b",
                  ],
                }
                """,
            ),
            (
                "a missing trailing comma is added when the rule moves",
                """
                include_rules = [
                  "+b",
                  "+a"
                ]
                """,
                """
                include_rules = [
                  "+a",
                  "+b",
                ]
                """,
            ),
            (
                "duplicates collapse into the last copy",
                """
                include_rules = [
                  # First copy.
                  "+a",  # Inline.
                  "+b",
                  "+a",
                ]
                """,
                """
                include_rules = [
                  # First copy.
                  # Inline.
                  "+a",
                  "+b",
                ]
                """,
            ),
            (
                "duplicates are removed from an otherwise sorted list",
                """
                include_rules = [
                  "+a",
                  "+a",
                  "+b",
                ]
                """,
                """
                include_rules = [
                  "+a",
                  "+b",
                ]
                """,
            ),
            (
                "duplicates keep their place among rules for the same path",
                """
                include_rules = [
                  "+a",
                  "-a",
                  "+a",
                ]
                """,
                """
                include_rules = [
                  "-a",
                  "+a",
                ]
                """,
            ),
            (
                "a shadowed subdirectory rule of the same kind is sorted",
                """
                include_rules = [
                  "+a/b",
                  "+a",
                ]
                """,
                """
                include_rules = [
                  "+a",
                  "+a/b",
                ]
                """,
            ),
            (
                "specific_include_rules lists are sorted in place",
                """
                specific_include_rules = {
                  r"z\\.cc": [
                    "+y",
                    "+x",
                  ],
                  "a.cc": [
                    "+b",
                    "+a",
                  ],
                }
                """,
                """
                specific_include_rules = {
                  r"z\\.cc": [
                    "+x",
                    "+y",
                  ],
                  "a.cc": [
                    "+a",
                    "+b",
                  ],
                }
                """,
            ),
            (
                "other variables are left alone",
                """
                deps = {
                  "z": "z.git",
                  "a": "a.git",
                }
                hooks = [
                  {"name": "z"},
                  {"name": "a"},
                ]
                skip_child_includes = [
                  "z",
                  "a",
                ]
                """,
                None,
            ),
            (
                "a group with a single rule that isn't a string is left alone",
                """
                include_rules = [
                  COMMON_RULE,

                  "+a",
                  "+b",
                ]
                """,
                None,
            ),
            (
                "short and sorted single-line lists are left alone",
                """
                include_rules = ["+a", "+b"]
                specific_include_rules = {
                  "a.cc": [
                    "+z",
                  ],
                  "b.cc": ["+y"],
                }
                """,
                None,
            ),
            (
                "groups are not sorted against each other",
                """
                include_rules = [
                  "+b",

                  "+a",
                ]
                """,
                None,
            ),
            (
                "sorting would only change behavior across groups",
                """
                include_rules = [
                  "+a/b",

                  "-a",
                ]
                """,
                None,
            ),
            (
                "lists with keep-sorted directives are left alone",
                """
                include_rules = [
                  "-ios/chrome",
                  # KEEP_SORTED start
                  "+ios/chrome/b",
                  "+components/a",
                  # KEEP_SORTED end
                ]
                specific_include_rules = {
                  "a.cc": [  # go/KEEP_SORTED start
                    "+z",
                    "+y",
                  ],  # go/KEEP_SORTED end
                }
                """
                # Spelled out at runtime so keep-sorted doesn't act on this file.
                .replace("KEEP_SORTED", "keep" + "-sorted"),
                None,
            ),
            (
                "lists with IfThisThenThat directives are left alone",
                """
                include_rules = [
                  # LINT_DIRECTIVE.IfChange
                  "+z",
                  # LINT_DIRECTIVE.ThenChange(//foo/DEPS)
                  "+a",
                ]
                """
                # Spelled out at runtime so IfThisThenThat ignores this file.
                .replace("LINT_DIRECTIVE", "LI" + "NT"),
                None,
            ),
            (
                "sorted lists are left alone",
                """
                include_rules = [
                  "+a",

                  "+b",
                  "+c"
                ]
                """,
                None,
            ),
            (
                "sorted lists are left alone even if they can't be rewritten",
                """
                include_rules = [
                  "+a", "+b",
                  "+c" "/d",
                ]
                """,
                None,
            ),
        ]
        for name, content, expected in cases:
            with self.subTest(name):
                content = _Dedent(content)
                expected = content if expected is None else _Dedent(expected)
                self.assertEqual(
                    deps_format.FormatDeps(content), (expected, [])
                )

    def testLeavesListUnsortedWithWarning(self):
        cases = [
            (
                "sorting would change which rule applies",
                """
                include_rules = [
                  "+a/b",
                  "-a",
                ]
                """,
                'line 2: "+a/b" is overridden by "-a" on line 3, so sorting '
                "would change which rule applies.",
            ),
            (
                "an unsortable group without a trailing comma",
                """
                include_rules = [
                  "+a/b",
                  "-a"
                ]
                """,
                'line 2: "+a/b" is overridden by "-a" on line 3, so sorting '
                "would change which rule applies.",
            ),
            (
                "a group with duplicates that would change behavior",
                """
                include_rules = [
                  "+a/b",
                  "+a/b",
                  "-a",
                ]
                """,
                'line 3: "+a/b" is overridden by "-a" on line 4, so sorting '
                "would change which rule applies.",
            ),
            (
                "a broader rule in a later group does not hide the change",
                """
                include_rules = [
                  "+a/b/c",
                  "-a/b",

                  "+a",
                ]
                """,
                'line 2: "+a/b/c" is overridden by "-a/b" on line 3, so '
                "sorting would change which rule applies.",
            ),
            (
                "sorting would change which rule applies without ! rules",
                """
                include_rules = [
                  "-a/b",
                  "+a",
                  "!a/b",
                ]
                """,
                'line 2: "-a/b" is overridden by "+a" on line 3, so sorting '
                "would change which rule applies.",
            ),
            (
                "a rule is not a string",
                """
                include_rules = [
                  "+b",
                  RULE,
                ]
                """,
                "line 3: rule is not a plain string",
            ),
            (
                "two rules share a line",
                """
                include_rules = [
                  "+b", "+a",
                  "+c",
                ]
                """,
                "line 2: more than one rule on the line",
            ),
            (
                "strings are implicitly concatenated on one line",
                """
                include_rules = [
                  "+b" "/c",
                  "+a",
                ]
                """,
                "line 2: rule does not sit alone on its line",
            ),
            (
                "strings are implicitly concatenated across lines",
                """
                include_rules = [
                  "+c"
                  "+b",
                  "+a",
                ]
                """,
                "line 2: rule spans multiple lines",
            ),
            (
                "a rule has no prefix",
                """
                include_rules = [
                  "+b",
                  "a",
                ]
                """,
                'line 3: rule "a" does not start with "+", "-" or "!"',
            ),
            (
                "a rule ends with a slash",
                """
                include_rules = [
                  "+a/b",
                  "-a/",
                ]
                """,
                'line 3: rule "-a/" is not a valid path',
            ),
            (
                "a rule has an empty path",
                """
                include_rules = [
                  "+b",
                  "-",
                ]
                """,
                'line 3: rule "-" is not a valid path',
            ),
            (
                "a rule without a prefix sorts after the one above it",
                """
                include_rules = [
                  "+components/prefs",
                  "chrome/browser",
                ]
                """,
                'line 3: rule "chrome/browser" does not start with',
            ),
            (
                "a rule with a trailing slash sorts before the one below it",
                """
                include_rules = [
                  "-a/",
                  "+a/b",
                ]
                """,
                'line 2: rule "-a/" is not a valid path',
            ),
            (
                "a comma precedes the closing bracket",
                """
                include_rules = [
                  "+b",
                  "+a"
                ,]
                """,
                'line 4: unexpected text before "]"',
            ),
            (
                "a comma is separated from its rule by a blank line",
                """
                include_rules = [
                  "+b",
                  "+a"

                  ,
                ]
                """,
                "line 5: unexpected text between rules",
            ),
            (
                "a rule's comma is on the next line",
                """
                include_rules = [
                  "+b"
                  , "+a",
                ]
                """,
                "line 2: rule's comma is not on its line",
            ),
            (
                "a rule spans a blank line",
                """
                include_rules = [
                  "+d"

                  "/e",
                  "+c",
                  "+b",
                ]
                """,
                "line 2: rule spans a blank line",
            ),
            (
                "parentheses around a rule span blank lines",
                """
                include_rules = [
                  (

                    "+b"

                  ),
                  "+d",
                  "+c",
                ]
                """,
                "line 2: unexpected text between rules",
            ),
            (
                "a comma sits on its own line",
                """
                include_rules = [
                  "+b"
                  ,
                  "+a",
                ]
                """,
                "line 3: unexpected text between rules",
            ),
        ]
        for name, content, warning in cases:
            with self.subTest(name):
                content = _Dedent(content)
                formatted, warnings = deps_format.FormatDeps(content)
                self.assertEqual(formatted, content)
                self.assertEqual(len(warnings), 1)
                self.assertTrue(
                    warnings[0].startswith(warning),
                    f"{warnings[0]!r} does not start with {warning!r}",
                )

    def testInlineCommentMatchingTheSurvivorsIsNotRepeated(self):
        # The surviving copy's comment has trailing spaces.
        content = (
            "include_rules = [\n"
            '  "+a",  # For a.\n'
            '  "+b",\n'
            '  "+a",  # For a.   \n'
            "]\n"
        )
        self.assertEqual(
            deps_format.FormatDeps(content),
            ('include_rules = [\n  "+a",  # For a.   \n  "+b",\n]\n', []),
        )

    def testOtherGroupsAreStillSortedWhenOneWouldChangeBehavior(self):
        content = _Dedent(
            """
            include_rules = [
              "+d",
              "+c",

              "+a/b",
              "-a"
            ]
            """
        )
        formatted, warnings = deps_format.FormatDeps(content)
        self.assertEqual(
            formatted,
            _Dedent(
                """
                include_rules = [
                  "+c",
                  "+d",

                  "+a/b",
                  "-a"
                ]
                """
            ),
        )
        self.assertEqual(len(warnings), 1)
        self.assertTrue(
            warnings[0].startswith('line 5: "+a/b" is overridden by "-a"'),
            warnings[0],
        )
        self.assertTrue(
            warnings[0].endswith("leaving its group unsorted"), warnings[0]
        )

    def testListsThatCantBeRewrittenOnlyWarnForTouchedGroups(self):
        content = _Dedent(
            """
            include_rules = ["+d",
              "+c",

              "+a",
              "+b",
            ]
            """
        )
        cases = [
            ("touching the unsorted group", [(2, 2)], 1),
            ("touching only the sorted group", [(4, 5)], 0),
        ]
        for name, line_ranges, warning_count in cases:
            with self.subTest(name):
                formatted, warnings = deps_format.FormatDeps(
                    content, line_ranges
                )
                self.assertEqual(formatted, content)
                self.assertEqual(len(warnings), warning_count, warnings)

    def testGroupsThatCantBeRewrittenOnlyAffectThemselves(self):
        content = _Dedent(
            """
            include_rules = [
              "+d", "+c",
              "+e",

              "+long/path/"
                  "header.h",
              "+z",

              "+g",
              "+f",
            ]
            """
        )
        cases = [
            ("touching only the last group", [(10, 10)], 0),
            ("touching a group that needs sorting", [(2, 2), (10, 10)], 1),
            ("touching a sorted group", [(5, 7), (10, 10)], 0),
        ]
        for name, line_ranges, warning_count in cases:
            with self.subTest(name):
                formatted, warnings = deps_format.FormatDeps(
                    content, line_ranges
                )
                self.assertEqual(
                    formatted,
                    content.replace('"+g",\n  "+f"', '"+f",\n  "+g"'),
                )
                self.assertEqual(len(warnings), warning_count, warnings)
                if warnings:
                    self.assertEqual(
                        warnings[0],
                        "line 2: more than one rule on the line; leaving its "
                        "group unsorted",
                    )

    def testOtherListsAreStillSortedWhenOneIsUnsupported(self):
        content = _Dedent(
            """
            include_rules = [
              "+a/b",
              "-a",
            ]
            specific_include_rules = {
              "a.cc": [
                "+b",
                "+a",
              ],
            }
            """
        )
        formatted, warnings = deps_format.FormatDeps(content)
        self.assertIn('    "+a",\n    "+b",\n', formatted)
        self.assertIn('  "+a/b",\n  "-a",\n', formatted)
        self.assertEqual(len(warnings), 1)

    def testLineRanges(self):
        content = _Dedent(
            """
            include_rules = [
              "+b",
              "+a",
            ]
            specific_include_rules = {
              "a.cc": [
                "+d",
                "+c",
              ],
            }
            """
        )
        cases = [
            ("no ranges sorts every list", None, {1, 2}),
            ("a range inside the first list", [(2, 2)], {1}),
            ("a range on the second list's bracket", [(6, 6)], set()),
            ("a range on a rule of the second list", [(8, 8)], {2}),
            ("a range spanning both lists", [(3, 7)], {1, 2}),
            ("a range outside the lists", [(5, 5)], set()),
            ("no ranges at all", [], set()),
        ]
        for name, line_ranges, sorted_lists in cases:
            with self.subTest(name):
                formatted, warnings = deps_format.FormatDeps(
                    content, line_ranges
                )
                self.assertEqual(warnings, [])
                self.assertEqual(
                    '  "+a",\n  "+b",\n' in formatted, 1 in sorted_lists
                )
                self.assertEqual(
                    '    "+c",\n    "+d",\n' in formatted, 2 in sorted_lists
                )

    def testLineRangesSelectGroups(self):
        content = _Dedent(
            """
            include_rules = [
              "+b",
              "+a",

              # About d.
              "+d",
              "+c",
            ]
            """
        )
        cases = [
            ("a range in the first group", [(2, 2)], {1}),
            ("a range on a comment above the second group", [(5, 5)], {2}),
            ("a range on the blank line between them", [(4, 4)], set()),
        ]
        for name, line_ranges, sorted_groups in cases:
            with self.subTest(name):
                formatted, warnings = deps_format.FormatDeps(
                    content, line_ranges
                )
                self.assertEqual(warnings, [])
                self.assertEqual(
                    '  "+a",\n  "+b",\n' in formatted, 1 in sorted_groups
                )
                self.assertEqual(
                    '  "+c",\n  # About d.\n  "+d",\n' in formatted,
                    2 in sorted_groups,
                )

    def testPreservesLineEndings(self):
        content = 'include_rules = [\r\n  "+b",\r\n  "+a"\r\n]\r\n'
        self.assertEqual(
            deps_format.FormatDeps(content),
            ('include_rules = [\r\n  "+a",\r\n  "+b",\r\n]\r\n', []),
        )

    def testHandlesNonAsciiText(self):
        content = _Dedent(
            """
            include_rules = [
              "+b",  # Ünïcode.
              "+a",  # ✓
            ]
            """
        )
        self.assertEqual(
            deps_format.FormatDeps(content),
            (
                _Dedent(
                    """
                    include_rules = [
                      "+a",  # ✓
                      "+b",  # Ünïcode.
                    ]
                    """
                ),
                [],
            ),
        )

    def testSyntaxError(self):
        cases = [
            ("unclosed list", "include_rules = [\n", "line 1: could not"),
            ("null byte", "include_rules = []\0\n", "could not"),
        ]
        for name, content, warning in cases:
            with self.subTest(name):
                formatted, warnings = deps_format.FormatDeps(content)
                self.assertEqual(formatted, content)
                self.assertEqual(len(warnings), 1)
                self.assertTrue(warnings[0].startswith(warning), warnings[0])

    def testDoesNotPrintSyntaxWarnings(self):
        content = 'include_rules = []\nx = "foo\\.cc"\n'
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.assertEqual(deps_format.FormatDeps(content), (content, []))


if __name__ == "__main__":
    unittest.main()
