# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Sorts the include rules in checkdeps DEPS files.

Rules in `include_rules` and in each list of `specific_include_rules` are
sorted by path, ignoring the leading `+`, `-` or `!`. checkdeps applies the
last matching rule in a list, so sorting by path keeps every parent directory
rule above the rules for its subdirectories and preserves which rule applies.
Groups where sorting would change which rule applies are left alone and
reported instead.

Each group of rules separated by blank lines is sorted on its own. Comment
lines directly above a rule move with it, while comments followed by a blank
line stay in place. Exact duplicate rules in a group are collapsed into their
last occurrence, which is the one checkdeps applies. Lists with keep-sorted or
IfThisThenThat directives are left alone, since those rely on line order.
"""

from __future__ import annotations

import ast
import collections
import dataclasses
import io
import re
import tokenize
import warnings
from typing import Optional, Sequence, Union

# Top-level DEPS variables whose lists hold checkdeps include rules.
_INCLUDE_RULES = "include_rules"
_SPECIFIC_INCLUDE_RULES = "specific_include_rules"

_RULE_PREFIXES = ("+", "-", "!")
_COMMENT_LINE_RE = re.compile(r"\s*#")
# Directives for tools that rely on the order of the lines they cover:
# keep-sorted (https://github.com/google/keep-sorted) and IfThisThenThat.
# "LINT" is split up so IfThisThenThat doesn't treat this file as a directive.
_ORDER_DIRECTIVE_RE = re.compile(
    r"#.*\b(keep-sorted\s+(start|end)|" + "LI" + r"NT\.(IfChange|ThenChange))\b"
)
# What may follow a rule string on its line: an optional comma and comment.
_RULE_SUFFIX_RE = re.compile(r"(?P<comma>\s*,)?\s*(?P<comment>#.*)?")


@dataclasses.dataclass
class _Rule:
    """A single include rule string and the lines that belong to it."""

    # The rule as checkdeps sees it, e.g. "+content/public".
    value: str
    # 1-based line number of the rule in the original file.
    line_number: int
    # The line holding the rule, with a trailing comma ensured.
    text: str
    # Comment lines directly above the rule, which move with it.
    leading_comments: list[str]
    # The comment after the rule on its own line, if any.
    inline_comment: Optional[str]

    @property
    def allow(self) -> str:
        return self.value[0]

    @property
    def path(self) -> str:
        return self.value[1:]


@dataclasses.dataclass
class _Group:
    """Consecutive rules not separated by a blank line, sorted together."""

    # The group's original lines, including the comments above its first rule.
    lines: list[str]
    # 1-based line number of lines[0].
    first_line: int
    # The list elements that start in the group.
    elements: list[ast.expr]
    rules: list[_Rule]
    # Why the group can't be rewritten, if it can't.
    problem: Optional[str] = None
    # The rules to write in place of the group's lines, if it is rewritten.
    new_rules: Optional[list[_Rule]] = None

    def Overlaps(
        self, line_ranges: Optional[Sequence[tuple[int, int]]]
    ) -> bool:
        """Whether the group overlaps any of line_ranges. None stands for the
        whole file."""
        last_line = self.first_line + len(self.lines) - 1
        return _Overlaps(self.first_line, last_line, line_ranges)


def _Overlaps(
    first: int, last: int, line_ranges: Optional[Sequence[tuple[int, int]]]
) -> bool:
    """Whether lines first to last overlap any of line_ranges. None stands for
    the whole file."""
    return line_ranges is None or any(
        start <= last and first <= end for start, end in line_ranges
    )


class _UnsupportedList(Exception):
    """Raised when a rule list's layout cannot be safely rewritten."""


def _Covers(rule_path: str, path: str) -> bool:
    """Mirrors checkdeps' Rule.ChildOrMatch: whether a rule for rule_path
    applies to path."""
    return rule_path == path or path.startswith(rule_path + "/")


def _LastMatch(rules: Sequence[_Rule], path: str) -> Optional[_Rule]:
    """Mirrors checkdeps' Rules.RuleApplyingTo for a single list of rules:
    the last rule in the list that covers path applies."""
    for rule in reversed(rules):
        if _Covers(rule.path, path):
            return rule
    return None


def _FindBehaviorChange(
    old_rules: Sequence[_Rule], new_rules: Sequence[_Rule]
) -> Optional[tuple[_Rule, _Rule]]:
    """Finds an include path whose applicable rule differs between an ordering
    of some rules and the same rules sorted by path.

    Only the rule paths themselves need checking: any other include path gets
    the same rule as its closest ancestor that has a rule. Because new_rules
    is sorted, agreeing on every rule path also implies agreeing when
    checkdeps restricts the rules to those below a directory marked
    new_usages_require_review. checkdeps can also ignore temporary "!" rules,
    so the orderings must agree without them too.

    Returns:
        None if the orderings behave the same. Otherwise, the rules that apply
        to the first differing path under the old and the new ordering.
    """
    rule_paths = sorted({rule.path for rule in old_rules})
    has_temp_rules = any(r.allow == "!" for r in old_rules)
    for ignore_temp_rules in (False, True) if has_temp_rules else (False,):
        old = [r for r in old_rules if not ignore_temp_rules or r.allow != "!"]
        new = [r for r in new_rules if not ignore_temp_rules or r.allow != "!"]
        for path in rule_paths:
            old_rule = _LastMatch(old, path)
            new_rule = _LastMatch(new, path)
            # Both orderings hold the same rules, so either both match or
            # neither does.
            if old_rule and new_rule and old_rule.allow != new_rule.allow:
                return old_rule, new_rule
    return None


def _IsSingleStringToken(source: str) -> bool:
    """Whether source is exactly one string literal, which rules out implicit
    concatenation such as "+foo" "/bar"."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return False
    significant = [
        t
        for t in tokens
        if t.type not in (tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER)
    ]
    return len(significant) == 1 and significant[0].type == tokenize.STRING


def _FindRuleProblem(value: str) -> Optional[str]:
    """Returns why a rule string can't be sorted, or None if it can."""
    if not value.startswith(_RULE_PREFIXES):
        return f'rule "{value}" does not start with "+", "-" or "!"'
    # checkdeps rejects a trailing "/", and neither it nor an empty path
    # matches subdirectories the way _Covers assumes.
    if len(value) == 1 or value.endswith("/"):
        return f'rule "{value}" is not a valid path'
    return None


def _ParseRule(
    element: ast.expr,
    is_last: bool,
    lines: Sequence[str],
    leading_comments: list[str],
) -> _Rule:
    """Builds a _Rule from a list element, checking that it sits alone on its
    line along with its comma, which only the last element can omit."""
    if not isinstance(element, ast.Constant) or not isinstance(
        element.value, str
    ):
        raise _UnsupportedList(
            f"line {element.lineno}: rule is not a plain string"
        )
    if element.lineno != element.end_lineno:
        raise _UnsupportedList(
            f"line {element.lineno}: rule spans multiple lines"
        )
    value = element.value
    problem = _FindRuleProblem(value)
    if problem:
        raise _UnsupportedList(f"line {element.lineno}: {problem}")

    line = lines[element.lineno - 1]
    # ast column offsets count UTF-8 bytes.
    encoded = line.encode("utf-8")
    before = encoded[: element.col_offset].decode("utf-8")
    source = encoded[element.col_offset : element.end_col_offset].decode(
        "utf-8"
    )
    rest = encoded[element.end_col_offset :].decode("utf-8")
    body = rest.rstrip("\r\n")
    line_ending = rest[len(body) :]
    suffix = _RULE_SUFFIX_RE.fullmatch(body)
    if before.strip() or not suffix or not _IsSingleStringToken(source):
        raise _UnsupportedList(
            f"line {element.lineno}: rule does not sit alone on its line"
        )
    if not suffix.group("comma") and not is_last:
        raise _UnsupportedList(
            f"line {element.lineno}: rule's comma is not on its line"
        )

    text = line
    if not suffix.group("comma"):
        text = before + source + "," + body + line_ending
    return _Rule(
        value=value,
        line_number=element.lineno,
        text=text,
        leading_comments=leading_comments,
        inline_comment=suffix.group("comment"),
    )


def _ParseRuleList(
    node: ast.List, lines: Sequence[str]
) -> list[Union[str, _Group]]:
    """Splits the lines between a rule list's brackets into groups of rules
    and the lines that stay in place.

    Blank lines separate groups. Comments directly above a rule move with it,
    while comments followed by a blank line, or after the last rule, stay in
    place. Groups whose rules aren't one plain string per line get a problem
    and are kept as they are.

    Raises:
        _UnsupportedList: if the list's lines can't be split into groups,
            such as when a rule shares a line with a bracket, a blank line
            falls inside a rule, or a line holds only part of the list's
            syntax, like a lone comma.
    """
    first, last = node.lineno, node.end_lineno
    starting: dict[int, list[ast.expr]] = collections.defaultdict(list)
    spanned: set[int] = set()
    for element in node.elts:
        starting[element.lineno].append(element)
        span = range(element.lineno, element.end_lineno + 1)
        if any(not lines[n - 1].strip() for n in span):
            raise _UnsupportedList(
                f"line {element.lineno}: rule spans a blank line"
            )
        spanned.update(span)
    if first in spanned or last in spanned:
        raise _UnsupportedList(f"line {first}: list is not one rule per line")
    # Only whitespace may precede the closing bracket, e.g. not ",]".
    if lines[last - 1].encode("utf-8")[: node.end_col_offset - 1].strip():
        raise _UnsupportedList(f'line {last}: unexpected text before "]"')

    segments: list[Union[str, _Group]] = []
    group: Optional[_Group] = None
    comments: list[str] = []
    for line_number in range(first + 1, last):
        line = lines[line_number - 1]
        if line_number not in spanned:
            if _COMMENT_LINE_RE.match(line):
                comments.append(line)
                continue
            if line.strip():
                raise _UnsupportedList(
                    f"line {line_number}: unexpected text between rules"
                )
            segments.extend(comments)
            segments.append(line)
            comments = []
            group = None
            continue

        if group is None:
            group = _Group(
                lines=[],
                first_line=line_number - len(comments),
                elements=[],
                rules=[],
            )
            segments.append(group)
        group.lines.extend(comments)
        group.lines.append(line)
        elements = starting.get(line_number, [])
        group.elements.extend(elements)
        # Lines continuing a rule that spans multiple lines start no element,
        # and that rule already gave the group a problem.
        if elements and group.problem is None:
            try:
                if len(elements) > 1:
                    raise _UnsupportedList(
                        f"line {line_number}: more than one rule on the line"
                    )
                group.rules.append(
                    _ParseRule(
                        elements[0],
                        elements[0] is node.elts[-1],
                        lines,
                        comments,
                    )
                )
            except _UnsupportedList as e:
                group.problem = str(e)
        comments = []
    segments.extend(comments)
    return segments


def _NeedsSorting(elements: Sequence[ast.expr]) -> bool:
    """Whether a group's elements are not valid rules in sorted order without
    duplicates. This only looks at the rule values, not the layout."""
    # A single rule is always in order, whatever it is.
    if len(elements) < 2:
        return False
    values = [
        e.value if isinstance(e, ast.Constant) else None for e in elements
    ]
    if not all(isinstance(v, str) and not _FindRuleProblem(v) for v in values):
        return True
    keys = [_SortKey(v) for v in values]
    return len(set(values)) != len(values) or keys != sorted(keys)


def _NeedsFormatting(
    node: ast.List,
    lines: Sequence[str],
    line_ranges: Optional[Sequence[tuple[int, int]]],
) -> bool:
    """Whether a list has a group overlapping line_ranges that needs sorting,
    looking only at the rule values and the lines between them."""
    # Pairs of a group's first line, including the comments above its first
    # rule, and the group's elements.
    groups: list[tuple[int, list[ast.expr]]] = []
    previous: Optional[ast.expr] = None
    for element in node.elts:
        between = (
            lines[previous.end_lineno : element.lineno - 1] if previous else []
        )
        if previous is None or not all(line.strip() for line in between):
            first_line = element.lineno
            while first_line - 1 > node.lineno and _COMMENT_LINE_RE.match(
                lines[first_line - 2]
            ):
                first_line -= 1
            groups.append((first_line, []))
        groups[-1][1].append(element)
        previous = element
    return any(
        _Overlaps(first_line, group[-1].end_lineno, line_ranges)
        and _NeedsSorting(group)
        for first_line, group in groups
    )


def _SortKey(value: str) -> str:
    """Sorts rules by path, ignoring the leading "+", "-" or "!"."""
    return value[1:]


def _Dedupe(rules: Sequence[_Rule]) -> list[_Rule]:
    """Drops all but the last copy of each rule.

    checkdeps applies the last matching rule, so earlier exact copies never
    take effect. Their comments are kept by moving them onto the surviving
    copy, unless it already has them: copies often carry the same comment.
    """
    last_index = {rule.value: i for i, rule in enumerate(rules)}
    moved_comments: dict[str, list[str]] = collections.defaultdict(list)
    survivors: list[_Rule] = []
    for i, rule in enumerate(rules):
        moved = moved_comments[rule.value]
        if i != last_index[rule.value]:
            survivor = rules[last_index[rule.value]]
            kept = _Stripped(moved + survivor.leading_comments)
            if survivor.inline_comment:
                kept.append(survivor.inline_comment.strip())
            # Lay moved comments out like the line they will sit above.
            text = survivor.text
            indent = text[: len(text) - len(text.lstrip())]
            line_ending = text[len(text.rstrip("\r\n")) :]
            # Compare whole comment blocks, so a repeated line inside a
            # multi-line comment, such as a bare "#", is kept.
            block = _Stripped(rule.leading_comments)
            if block and not _IsSublist(block, kept):
                moved.extend(indent + line + line_ending for line in block)
                kept.extend(block)
            inline = rule.inline_comment
            if inline and inline.strip() not in kept:
                moved.append(indent + inline.strip() + line_ending)
            continue
        if moved:
            rule = dataclasses.replace(
                rule, leading_comments=moved + rule.leading_comments
            )
        survivors.append(rule)
    return survivors


def _Stripped(comments: Sequence[str]) -> list[str]:
    return [comment.strip() for comment in comments]


def _IsSublist(needle: list[str], haystack: list[str]) -> bool:
    """Whether needle appears as a contiguous run in haystack."""
    return any(
        haystack[i : i + len(needle)] == needle
        for i in range(len(haystack) - len(needle) + 1)
    )


def _JoinSegments(segments: Sequence[Union[str, _Group]]) -> list[str]:
    """Joins the parsed lines of a rule list back together."""
    new_lines: list[str] = []
    for segment in segments:
        if isinstance(segment, str):
            new_lines.append(segment)
        elif segment.new_rules is None:
            new_lines.extend(segment.lines)
        else:
            for rule in segment.new_rules:
                new_lines.extend(rule.leading_comments)
                new_lines.append(rule.text)
    return new_lines


def _FormatRuleList(
    node: ast.List,
    lines: Sequence[str],
    line_ranges: Optional[Sequence[tuple[int, int]]],
) -> tuple[Optional[list[str]], list[str]]:
    """Sorts and deduplicates the groups of a rule list that overlap
    line_ranges.

    Groups that can't be rewritten, or where sorting would change which rule
    applies, are left as they are.

    Returns:
        The new lines between the list's brackets, or None if the list needs
        no changes, and warnings about touched groups left as they are.

    Raises:
        _UnsupportedList: if the list as a whole can't be rewritten and needs
            changes.
    """
    if any(
        _ORDER_DIRECTIVE_RE.search(line)
        for line in lines[node.lineno - 1 : node.end_lineno]
    ):
        return None, []
    try:
        segments = _ParseRuleList(node, lines)
    except _UnsupportedList:
        # Only complain about lists that need changes.
        if _NeedsFormatting(node, lines, line_ranges):
            raise
        return None, []

    group_warnings: list[str] = []
    for group in (s for s in segments if isinstance(s, _Group)):
        if not group.Overlaps(line_ranges):
            continue
        if group.problem:
            if _NeedsSorting(group.elements):
                group_warnings.append(
                    f"{group.problem}; leaving its group unsorted"
                )
            continue
        # sorted() is stable, so rules for the same path keep their order, and
        # the copy of a rule that _Dedupe keeps is still the last one.
        new_rules = _Dedupe(
            sorted(group.rules, key=lambda r: _SortKey(r.value))
        )
        if [r.line_number for r in new_rules] == [
            r.line_number for r in group.rules
        ]:
            continue
        # Each group is checked on its own: a broader rule in a later group can
        # hide a change in an earlier one, but checkdeps ignores that rule for
        # includes under a directory marked new_usages_require_review.
        change = _FindBehaviorChange(group.rules, new_rules)
        if change:
            # Sorting only moves a rule above the rules for its
            # subdirectories, so the old rule is broader and below the new one.
            broader, overridden = change
            group_warnings.append(
                f'line {overridden.line_number}: "{overridden.value}" is '
                f'overridden by "{broader.value}" on line '
                f"{broader.line_number}, so sorting would change which rule "
                "applies. Delete the overridden rule, or move it below the "
                "broader one if it is meant to apply; leaving its group "
                "unsorted"
            )
            continue
        group.new_rules = new_rules

    new_lines = _JoinSegments(segments)
    if new_lines == lines[node.lineno : node.end_lineno - 1]:
        return None, group_warnings
    return new_lines, group_warnings


def _FindRuleLists(module: ast.Module) -> list[ast.List]:
    """Returns the include rule lists assigned at the top level of a DEPS
    file."""
    rule_lists: list[ast.List] = []
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        names = [t.id for t in statement.targets if isinstance(t, ast.Name)]
        if _INCLUDE_RULES in names and isinstance(statement.value, ast.List):
            rule_lists.append(statement.value)
        elif _SPECIFIC_INCLUDE_RULES in names and isinstance(
            statement.value, ast.Dict
        ):
            rule_lists.extend(
                v for v in statement.value.values if isinstance(v, ast.List)
            )
    return rule_lists


def FormatDeps(
    content: str,
    line_ranges: Optional[Sequence[tuple[int, int]]] = None,
) -> tuple[str, list[str]]:
    """Sorts the include rules in the content of a DEPS file.

    Args:
        content: The DEPS file content.
        line_ranges: 1-based inclusive (first, last) line ranges. If given,
            only groups of rules overlapping one of them are sorted and have
            their duplicates removed.

    Returns:
        The formatted content and warnings about lists left unsorted.
    """
    try:
        # Unescaped regexes such as "foo\.cc" in specific_include_rules would
        # otherwise print a SyntaxWarning (a DeprecationWarning before Python
        # 3.12).
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            warnings.simplefilter("ignore", DeprecationWarning)
            module = ast.parse(content)
    except SyntaxError as e:
        location = f"line {e.lineno}: " if e.lineno else ""
        return content, [f"{location}could not parse DEPS file: {e.msg}"]

    lines = io.StringIO(content, newline="").readlines()
    problems: list[str] = []
    # Rewrite from the bottom up so earlier line numbers stay valid, and
    # prepend each list's warnings to keep them in file order.
    for node in sorted(
        _FindRuleLists(module), key=lambda n: (-n.lineno, -n.col_offset)
    ):
        if len(node.elts) < 2:
            continue
        if not _Overlaps(node.lineno, node.end_lineno, line_ranges):
            continue
        try:
            new_lines, list_problems = _FormatRuleList(node, lines, line_ranges)
        except _UnsupportedList as e:
            problems.insert(0, f"{e}; leaving the list unsorted")
            continue
        problems[:0] = list_problems
        if new_lines is not None:
            lines[node.lineno : node.end_lineno - 1] = new_lines

    return "".join(lines), problems
