#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""This script rolls DEPS variables/revisions from a source DEPS file onto a
destination DEPS file.

Git dependencies are rolled using roll-dep, while CIPD variables are updated
in-place in the destination DEPS file and committed.

Example usage:
  roll_downstream_deps.py \
      --source-deps some/repo/DEPS \
      --destination-deps some/downstream/repo/DEPS \
      --var harfbuzz_revision \
      --var gn_version

Note that this script is mainly expected to run on Linux-based bots, so it
deliberately avoids trying to be Windows-compatible.
"""

import argparse
import ast
import collections.abc
import os
import re
import sys
from typing import List, Mapping, NamedTuple, Optional, Sequence

import gclient_eval
import gclient_utils
import subprocess2

ROLL_DEP_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "roll_dep.py"
)


class Error(Exception):
    pass


class DepUpdate(NamedTuple):
    var_name: str
    deps_path: str
    dest_revision: str
    source_revision: str
    is_cipd: bool


def is_pristine(root: str) -> bool:
    """Returns True if a git checkout is pristine."""
    diff_cmd = ["git", "diff", "--ignore-submodules", "origin/main"]
    return (
        not subprocess2.check_output(diff_cmd, cwd=root).decode("utf-8").strip()
        and not subprocess2.check_output(diff_cmd + ["--cached"], cwd=root)
        .decode("utf-8")
        .strip()
    )


def find_path_for_var(
    deps_dict: gclient_eval._NodeDict, var_name: str, revision: str
) -> Optional[str]:
    """Finds the deps path that corresponds to `var_name` or `revision`."""
    if isinstance(deps_dict, gclient_eval._NodeDict):
        for path in deps_dict:
            node = deps_dict.GetNode(path)
            if node is None:
                continue
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "Var"
                    and len(child.args) == 1
                    and isinstance(child.args[0], ast.Constant)
                    and child.args[0].value == var_name
                ):
                    return path

    for path, dep_info in deps_dict.items():
        if revision in str(dep_info):
            return path
    return None


def get_revision_from_dep_value(dep_value) -> Optional[str]:
    """Extracts the revision or CIPD version from `dep_value`."""
    url = dep_value
    if isinstance(dep_value, collections.abc.Mapping):
        # This is for CIPD packages.
        if "packages" in dep_value:
            for package in dep_value["packages"]:
                if "version" in package:
                    return package["version"]
        url = dep_value.get("url")

    if not url or not isinstance(url, str):
        return None

    match = re.search(r"@(.+)", url)
    if match:
        return match.group(1)
    return None


def _parse_deps(content: str, filename: str) -> gclient_eval._NodeDict:
    """Parses `content` using `gclient_eval.Exec`."""
    try:
        scope = gclient_eval.Exec(content, filename)
    except (SyntaxError, ValueError, gclient_utils.Error) as e:
        raise Error(f"Failed to parse {filename}: {e}") from e
    if not scope.get("vars") and not scope.get("deps"):
        raise Error(f"No vars or deps found in {filename}.")
    return scope


def resolve_dep_update(
    source_scope: gclient_eval._NodeDict,
    dest_scope: gclient_eval._NodeDict,
    var_name: str,
) -> DepUpdate:
    """Resolves a single `var_name` between `source_scope` and `dest_scope`."""
    dest_vars = dest_scope.get("vars", {})
    if var_name not in dest_vars:
        raise Error(f'Entry "{var_name}" not found in destination DEPS.')

    dest_revision = gclient_eval.GetVar(dest_scope, var_name)
    if not isinstance(dest_revision, str) or not dest_revision:
        raise Error(
            f'Could not find path for var "{var_name}" in destination DEPS.'
        )

    dest_deps = dest_scope.get("deps", {})
    deps_path = find_path_for_var(dest_deps, var_name, dest_revision)
    if not deps_path:
        raise Error(
            f'Could not find path for var "{var_name}" in destination DEPS.'
        )

    dest_dep_value = dest_deps.get(deps_path)
    is_cipd = False
    if isinstance(dest_dep_value, collections.abc.Mapping):
        dep_type = dest_dep_value.get("dep_type", "git")
        if dep_type == "cipd":
            is_cipd = True
        elif dep_type != "git":
            raise Error(
                f'Dependency "{deps_path}" has unsupported dep_type '
                f'"{dep_type}".'
            )
    elif not isinstance(dest_dep_value, str):
        raise Error(f'Dependency "{deps_path}" is not a valid dependency.')

    source_vars = source_scope.get("vars", {})
    source_deps = source_scope.get("deps", {})
    source_revision = None
    if var_name in source_vars:
        val = gclient_eval.GetVar(source_scope, var_name)
        if isinstance(val, str) and val:
            source_revision = val

    if not source_revision:
        source_dep_value = source_deps.get("src/" + deps_path)
        if source_dep_value is None:
            source_dep_value = source_deps.get(deps_path)
        if source_dep_value is not None:
            source_revision = get_revision_from_dep_value(source_dep_value)

    if not source_revision:
        if is_cipd:
            raise Error(f'CIPD entry "{var_name}" not found in source DEPS.')
        raise Error(f'Entry "{var_name}" not found in source DEPS.')

    return DepUpdate(
        var_name=var_name,
        deps_path=deps_path,
        dest_revision=dest_revision,
        source_revision=source_revision,
        is_cipd=is_cipd,
    )


def get_dep_updates(
    source_content: str,
    destination_content: str,
    vars_to_roll: Sequence[str],
    source_filename: str = "<source>",
    dest_filename: str = "<destination>",
) -> List[DepUpdate]:
    """Resolves all requested `vars_to_roll` between source and destination."""
    source_scope = _parse_deps(source_content, source_filename)
    dest_scope = _parse_deps(destination_content, dest_filename)
    return [
        resolve_dep_update(source_scope, dest_scope, var_name)
        for var_name in vars_to_roll
    ]


def update_cipd_vars(
    destination_content: str,
    cipd_updates: Mapping[str, str],
    dest_filename: str = "<destination>",
) -> str:
    """Updates `cipd_updates` in `destination_content`."""
    dest_scope = _parse_deps(destination_content, dest_filename)
    for var_name, new_revision in cipd_updates.items():
        gclient_eval.SetVar(dest_scope, var_name, new_revision)
    return gclient_eval.RenderDEPSFile(dest_scope)


def generate_cipd_commit_message(
    cipd_updates: Mapping[str, str],
    bug: Optional[str] = None,
) -> str:
    """Generates a commit message for `cipd_updates`."""
    logs = [
        f"Roll {var_name} to {new_revision}\n"
        for var_name, new_revision in cipd_updates.items()
    ]
    cmdline = "roll_downstream_deps.py " + " ".join(
        f"--var {var_name}" for var_name in cipd_updates
    )
    commit_msg = ""
    if len(logs) > 1:
        commit_msg = f"Rolling {len(logs)} dependencies\n\n"
    commit_msg += "\n".join(logs) + "\n"
    commit_msg += f"Created with:\n  {cmdline}\n"
    if bug:
        commit_msg += f"\nBug: {bug}\n"
    return commit_msg


def commit_cipd_updates(
    destination_deps_file: str,
    cipd_updates: Mapping[str, str],
    ignore_dirty_tree: bool = False,
    bug: Optional[str] = None,
) -> None:
    """Updates CIPD vars in `destination_deps_file` and creates a commit."""
    dest_dir = os.path.dirname(os.path.abspath(destination_deps_file))
    deps_basename = os.path.basename(destination_deps_file)

    if not ignore_dirty_tree and not is_pristine(dest_dir):
        raise Error(
            f"Ensure {dest_dir} is clean first (no non-merged commits)."
        )

    with open(destination_deps_file, "r") as f:
        current_dest_content = f.read()
    new_content = update_cipd_vars(
        current_dest_content,
        cipd_updates,
        dest_filename=destination_deps_file,
    )
    with open(destination_deps_file, "w") as f:
        f.write(new_content)

    commit_msg = generate_cipd_commit_message(cipd_updates, bug=bug)
    print("Commit message:")
    print("\n".join("    " + line for line in commit_msg.splitlines()))

    subprocess2.check_call(["git", "add", deps_basename], cwd=dest_dir)
    subprocess2.check_call(
        ["git", "commit", "--quiet", "-m", commit_msg],
        cwd=dest_dir,
    )


def build_roll_dep_args(args: argparse.Namespace) -> List[str]:
    """Builds the list of pass-through arguments for roll-dep from `args`."""
    extra_args = []
    if getattr(args, "ignore_dirty_tree", False):
        extra_args.append("--ignore-dirty-tree")
    if getattr(args, "no_log", False):
        extra_args.append("--no-log")
    if getattr(args, "always_log", False):
        extra_args.append("--always-log")
    if getattr(args, "log_limit", None) is not None:
        extra_args.extend(["--log-limit", str(args.log_limit)])
    if getattr(args, "reviewer", None):
        for reviewer in args.reviewer:
            extra_args.extend(["-r", reviewer])
    if getattr(args, "bug", None):
        extra_args.extend(["-b", args.bug])
    if getattr(args, "no_update_readme", False):
        extra_args.append("--no-update-readme")
    return extra_args


def run_roll_dep(
    deps_path: str,
    roll_to: str,
    cwd: str,
    extra_args: Sequence[str] = (),
) -> None:
    """Executes roll_dep.py for `deps_path` to `roll_to` in `cwd`."""
    cmd = [
        sys.executable,
        ROLL_DEP_PATH,
        deps_path,
        "--roll-to",
        roll_to,
        *extra_args,
    ]
    subprocess2.check_call(cmd, cwd=cwd)


def roll_deps(
    source_content: str,
    destination_deps_file: str,
    vars_to_roll: Sequence[str],
    roll_dep_args: Sequence[str] = (),
    source_filename: str = "<source>",
    ignore_dirty_tree: bool = False,
    bug: Optional[str] = None,
) -> None:
    """Rolls `vars_to_roll` from `source_content` to `destination_deps_file`."""
    with open(destination_deps_file, "r") as f:
        destination_content = f.read()

    updates = get_dep_updates(
        source_content,
        destination_content,
        vars_to_roll,
        source_filename=source_filename,
        dest_filename=destination_deps_file,
    )

    git_updates = []
    cipd_updates = {}
    for update in updates:
        if update.source_revision == update.dest_revision:
            print(f"{update.var_name}: Revisions are the same.")
        elif update.is_cipd:
            cipd_updates[update.var_name] = update.source_revision
        else:
            git_updates.append(update)

    dest_dir = os.path.dirname(os.path.abspath(destination_deps_file))
    for update in git_updates:
        run_roll_dep(
            update.deps_path,
            update.source_revision,
            dest_dir,
            roll_dep_args,
        )

    if cipd_updates:
        commit_cipd_updates(
            destination_deps_file,
            cipd_updates,
            ignore_dirty_tree=ignore_dirty_tree,
            bug=bug,
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-deps",
        required=True,
        help="Source DEPS file where revisions will be copied from",
    )
    parser.add_argument(
        "--destination-deps",
        required=True,
        help="Destination DEPS file to update",
    )
    parser.add_argument(
        "--var",
        "--entry",
        dest="vars",
        action="append",
        required=True,
        help="DEPS variable/entry to update (can be specified multiple times)",
    )
    parser.add_argument(
        "--ignore-dirty-tree",
        action="store_true",
        help="Roll anyways, even if there is a diff.",
    )
    parser.add_argument(
        "-r",
        "--reviewer",
        action="append",
        help="To specify multiple reviewers, either use a comma separated "
        "list, e.g. -r joe,jane,john or provide the flag multiple times, e.g. "
        "-r joe -r jane. Defaults to @chromium.org",
    )
    parser.add_argument(
        "-b", "--bug", help="Associate a bug number to the roll"
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="Do not include the short log in the commit message",
    )
    parser.add_argument(
        "--always-log",
        action="store_true",
        help="Always include the short log in the commit message",
    )
    parser.add_argument(
        "--log-limit",
        type=int,
        help="Trim log after N commits",
    )
    parser.add_argument(
        "--no-update-readme",
        action="store_true",
        help="Do not try to update Revision in README.chromium",
    )
    args = parser.parse_args(argv)

    if not args.vars:
        parser.error("No DEPS variables specified to roll, aborting...")

    if args.no_log and args.always_log:
        parser.error("Can't use both --no-log and --always-log")

    if os.path.abspath(args.source_deps) == os.path.abspath(
        args.destination_deps
    ):
        parser.error("Source and destination DEPS files must be different.")

    with open(args.source_deps, "r") as f:
        source_content = f.read()

    try:
        roll_deps(
            source_content,
            args.destination_deps,
            args.vars,
            roll_dep_args=build_roll_dep_args(args),
            source_filename=args.source_deps,
            ignore_dirty_tree=args.ignore_dirty_tree,
            bug=args.bug,
        )
    except Error as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except subprocess2.CalledProcessError:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
