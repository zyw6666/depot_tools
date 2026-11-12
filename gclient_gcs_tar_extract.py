#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Stdlib-only GCS path safety, tar validation, and single-pass extraction.

Kept free of heavy depot_tools imports (and lazy-loading optional compression C
extensions) so large GCS tar extractions offloaded to a child Python process
start in ~50ms without re-importing gclient.
"""

import contextlib
import json
import ntpath
import os
import posixpath
import shutil
import stat
import sys
import tarfile
import tempfile
import traceback
from collections.abc import Iterable, Iterator
from typing import Callable, Optional, Tuple

TAR_SPOOL_MAX_BYTES = 64 * 1024 * 1024
DECOMPRESS_CHUNK_BYTES = 4 * 1024 * 1024
SCM_METADATA_NAMES = (".git", ".jj")


class GcsTarValidationError(Exception):
    """Raised when a GCS path or tar archive violates checkout safety rules."""


def is_abs_or_drive(path: str) -> bool:
    """Returns whether `path` is absolute or has a drive on any OS.

    This includes paths that start with a slash or a backslash, UNC paths and
    drive-relative paths such as `C:foo`.
    """
    return (
        os.path.isabs(path)
        or ntpath.isabs(path)
        or bool(ntpath.splitdrive(path)[0])
        or path.startswith(("/", "\\"))
    )


def is_link_or_junction(path: str) -> bool:
    """Returns whether `path` is a symlink or a Windows junction/mount point."""
    if os.path.islink(path):
        return True
    if hasattr(os.path, "isjunction") and os.path.isjunction(path):
        return True
    try:
        reparse_tag = getattr(os.lstat(path), "st_reparse_tag", 0)
    except OSError:
        return False
    return reparse_tag != 0 and reparse_tag in (
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0),
    )


def starts_with_scm_metadata(rel_path: str) -> bool:
    """Returns whether `rel_path` is or is inside a `.git` or `.jj` entry.

    Names are compared case-insensitively and without the trailing dots and
    spaces that Windows drops.
    """
    parts = [
        p for p in rel_path.replace("\\", "/").split("/") if p not in ("", ".")
    ]
    return bool(parts) and parts[0].rstrip(". ").lower() in SCM_METADATA_NAMES


def is_within_dir(path: str, parent_dir: str) -> bool:
    try:
        norm_parent = os.path.normcase(os.path.normpath(parent_dir))
        norm_path = os.path.normcase(os.path.normpath(path))
        return (
            norm_path != norm_parent
            and os.path.commonpath([norm_parent, norm_path]) == norm_parent
        )
    except ValueError:
        return False


def write_to_file(content: str, file_path: str) -> None:
    if is_link_or_junction(file_path):
        os.remove(file_path)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.write("\n")


def validate_output_dir_paths(
    dep_dir: str, root_dir: str, output_dir: str
) -> str:
    clean_dir = dep_dir.replace("\\", "/")
    raw_parts = clean_dir.split("/")
    parts = [p for p in raw_parts if p and p != "."]
    if (
        not parts
        or is_abs_or_drive(clean_dir)
        or ".." in raw_parts
        or ":" in clean_dir
        or any(
            p != p.rstrip(". ") or p.lower() in SCM_METADATA_NAMES
            for p in parts
        )
    ):
        raise GcsTarValidationError(f"Invalid GCS dependency path: {dep_dir}")
    real_root = os.path.realpath(root_dir)
    cur = real_root
    for part in parts:
        cur = os.path.join(cur, part)
        if is_link_or_junction(cur):
            raise GcsTarValidationError(
                f"Refusing to use symlinked GCS dependency path: {cur}"
            )
    real_cur = os.path.realpath(cur)
    if not is_within_dir(real_cur, real_root):
        raise GcsTarValidationError(
            f"GCS dependency path resolves outside expected directory: "
            f"{output_dir}"
        )
    real_parts = os.path.relpath(real_cur, real_root).split(os.sep)
    if [p.lower() for p in real_parts] != [p.lower() for p in parts]:
        raise GcsTarValidationError(f"Invalid GCS dependency path: {dep_dir}")
    return real_cur


def validate_target_paths_in_dir(
    real_output_dir: str,
    rel_paths: Iterable[str],
    allow_internal_symlinks: bool = False,
) -> str:
    checked = set()
    for rel_path in rel_paths:
        clean = rel_path.replace("\\", "/")
        parts = [p for p in clean.split("/") if p and p != "."]
        cur = real_output_dir
        for part in parts:
            cur = os.path.join(cur, part)
            if cur in checked:
                continue
            if not os.path.lexists(cur):
                break
            is_link = is_link_or_junction(cur)
            if is_link and not allow_internal_symlinks:
                raise GcsTarValidationError(
                    f"Refusing to write through symlink in GCS "
                    f"output_dir: {cur}"
                )
            real_cur = os.path.realpath(cur)
            if not is_within_dir(real_cur, real_output_dir):
                raise GcsTarValidationError(
                    f"Refusing to write outside GCS output_dir via "
                    f"symlink: {cur}"
                )
            if not is_link:
                checked.add(cur)
    return real_output_dir


def validate_tar_members(tar: tarfile.TarFile, prefixes: Iterable[str]) -> bool:
    members = tar.getmembers()
    if not members:
        return True

    valid_prefixes = [p for p in prefixes if p and p not in (".", "..")]
    if not valid_prefixes:
        if all(m.name.replace("\\", "/") in (".", "./") for m in members):
            return True
        return False

    def _validate(tarinfo: tarfile.TarInfo) -> bool:
        if is_abs_or_drive(tarinfo.name):
            return False

        if tarinfo.issym() or tarinfo.islnk():
            clean_linkname = (tarinfo.linkname or "").replace("\\", "/")
            if not clean_linkname or is_abs_or_drive(clean_linkname):
                return False
            if tarinfo.issym():
                clean_dirname = posixpath.dirname(
                    tarinfo.name.replace("\\", "/")
                )
                link_target = posixpath.normpath(
                    posixpath.join(clean_dirname, clean_linkname)
                )
            else:
                link_target = posixpath.normpath(clean_linkname)

            if (
                link_target == ".."
                or link_target.startswith("../")
                or is_abs_or_drive(link_target)
                or not any(
                    link_target == prefix
                    or link_target.startswith(prefix + "/")
                    for prefix in valid_prefixes
                )
            ):
                return False

        cleaned_name = tarinfo.name.replace("\\", "/")
        while cleaned_name.startswith("./") and len(cleaned_name) > 2:
            cleaned_name = cleaned_name[2:]
        if cleaned_name in (".", "./"):
            return True
        path_parts = set(cleaned_name.split("/"))
        if (
            ".." in path_parts
            or is_abs_or_drive(cleaned_name)
            or not any(
                cleaned_name == prefix or cleaned_name.startswith(prefix + "/")
                for prefix in valid_prefixes
            )
        ):
            return False
        return True

    return all(map(_validate, members))


def check_archive_top_level(
    names: Iterable[str], url: str, output_dir: str
) -> None:
    if any(starts_with_scm_metadata(name) for name in names):
        raise GcsTarValidationError(
            f"GCS object {url} has a .git or .jj entry at its top "
            f"level, which would make {output_dir} look like a "
            "checkout"
        )


def detect_tar_decompressor(
    artifact_path: str,
) -> Tuple[Optional[Callable], int, Tuple[type, ...]]:
    """Returns `(opener, min_expansion, recoverable_errors)` from magic bytes.

    Compression modules (`lzma`, `bz2`, `gzip`) are imported lazily so gclient
    still starts on custom Python builds compiled without optional C libraries.
    """
    try:
        with open(artifact_path, "rb") as f:
            magic = f.read(6)
    except OSError:
        return None, 1, ()
    try:
        if magic.startswith(b"\xfd7zXZ\x00"):
            import lzma

            return lzma.open, 4, (EOFError, lzma.LZMAError)
        if magic.startswith(b"BZh"):
            import bz2

            return bz2.open, 4, (EOFError,)
        if magic.startswith(b"\x1f\x8b"):
            import gzip

            return gzip.open, 2, (EOFError, gzip.BadGzipFile)
    except ImportError:
        return None, 1, ()
    return None, 1, ()


@contextlib.contextmanager
def open_uncompressed_tar(
    artifact_path: str,
    output_dir: str,
    max_spool_bytes: int = TAR_SPOOL_MAX_BYTES,
) -> Iterator[tarfile.TarFile]:
    """Opens a tar archive so header validation + extraction decompress once.

    Uncompressed `.tar` archives (and unrecognized formats) open directly via
    `tarfile.open(..., "r:*")`. Compressed `.tar.{xz,gz,bz2}` archives stream
    once into an in-memory `SpooledTemporaryFile` up to `max_spool_bytes` (or
    directly into an anonymous/delete-on-close `TemporaryFile` in `output_dir`
    when compressed size already guarantees rollover). If full-stream
    decompression raises a stream error (e.g. `BadGzipFile` from trailing
    padding after a valid tar stream), falls back to `tarfile.open(..., "r:*")`.
    """
    opener, min_expansion, recoverable_errs = detect_tar_decompressor(
        artifact_path
    )
    if opener is None:
        with tarfile.open(artifact_path, "r:*") as tar:
            yield tar
        return

    compressed_size = os.path.getsize(artifact_path)
    if (
        max_spool_bytes <= 0
        or compressed_size * min_expansion > max_spool_bytes
    ):
        spool_cm = tempfile.TemporaryFile(
            mode="w+b",
            prefix=".gcs_untar_",
            suffix=".tar",
            dir=output_dir,
        )
    else:
        spool_cm = tempfile.SpooledTemporaryFile(
            max_size=max_spool_bytes,
            mode="w+b",
            prefix=".gcs_untar_",
            suffix=".tar",
            dir=output_dir,
        )

    with contextlib.ExitStack() as stack:
        spool = stack.enter_context(spool_cm)
        try:
            with opener(artifact_path, "rb") as src:
                shutil.copyfileobj(src, spool, length=DECOMPRESS_CHUNK_BYTES)
        except recoverable_errs:
            spool_ok = False
        else:
            spool_ok = True

        if spool_ok:
            spool.seek(0)
            tar = stack.enter_context(tarfile.open(fileobj=spool, mode="r:"))
            yield tar
            return

    with tarfile.open(artifact_path, "r:*") as tar:
        yield tar


def extract_gcs_tar(
    artifact_output_file: str,
    output_dir: str,
    root_dir: str,
    deps_key: str,
    url: str,
    content_names_file: str,
    max_spool_bytes: int = TAR_SPOOL_MAX_BYTES,
) -> None:
    real_output_dir = validate_output_dir_paths(deps_key, root_dir, output_dir)
    with open_uncompressed_tar(
        artifact_output_file, real_output_dir, max_spool_bytes=max_spool_bytes
    ) as tar:
        raw_names = tar.getnames()
        formatted_names = []
        for name in raw_names:
            clean_name = name.replace("\\", "/")
            while clean_name.startswith("./") and len(clean_name) > 2:
                clean_name = clean_name[2:]
            formatted_names.append(clean_name)
        possible_top_level_dirs = set()
        for name in formatted_names:
            top = name.split("/")[0]
            if top not in ("", ".", ".."):
                possible_top_level_dirs.add(top)
        if not validate_tar_members(tar, possible_top_level_dirs):
            raise GcsTarValidationError("tarfile contains invalid entries")
        check_archive_top_level(formatted_names, url, output_dir)

        validate_target_paths_in_dir(
            real_output_dir, [os.path.basename(content_names_file)]
        )
        validate_target_paths_in_dir(
            real_output_dir, formatted_names, allow_internal_symlinks=True
        )
        write_to_file(json.dumps(raw_names), content_names_file)

        def tar_filter(member: tarfile.TarInfo, path: str):
            if hasattr(tarfile, "data_filter"):
                filtered = tarfile.data_filter(member, path)
            else:
                target = os.path.realpath(
                    os.path.join(real_output_dir, member.name)
                )
                if os.path.normcase(target) != os.path.normcase(
                    real_output_dir
                ) and not is_within_dir(target, real_output_dir):
                    raise GcsTarValidationError(
                        f"Refusing to write outside GCS output_dir: {target}"
                    )
                filtered = member
            if filtered:
                filtered.mtime = None
            return filtered

        tar.extractall(path=output_dir, filter=tar_filter)


def main() -> None:
    params = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    try:
        extract_gcs_tar(
            artifact_output_file=params["artifact_output_file"],
            output_dir=params["output_dir"],
            root_dir=params["root_dir"],
            deps_key=params["deps_key"],
            url=params["url"],
            content_names_file=params["content_names_file"],
        )
    except GcsTarValidationError as e:
        sys.stderr.write(f"{e}\n")
        sys.exit(2)
    except Exception:  # pylint: disable=broad-except
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
