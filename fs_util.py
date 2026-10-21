# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Filesystem copy-on-write (reflink/block-cloning) utilities and providers."""

import abc
import logging
import os
import shutil
import subprocess
import sys


class FilesystemProvider(abc.ABC):
    """Abstract base class for filesystem copy providers."""

    @abc.abstractmethod
    def copy(self, src, dst):
        """Copies or reflinks src to dst.

        Args:
            src: Source file or directory path.
            dst: Destination file or directory path.

        Returns:
            True if the copy/clone succeeded, False otherwise.
        """

    @property
    @abc.abstractmethod
    def supports_cow(self):
        """Returns True if the provider supports copy-on-write optimizations."""

    @property
    @abc.abstractmethod
    def fstype(self):
        """Returns the filesystem or provider type name."""

    @property
    def supports_snapshots(self):
        """Returns True if the provider supports subvolume snapshots."""
        return False


class DefaultFilesystemProvider(FilesystemProvider):
    """Fallback filesystem provider using standard file copies."""

    def __init__(self, fstype=None):
        self._fstype = fstype or "default"

    def copy(self, src, dst):
        try:
            if os.path.isdir(src):
                os.makedirs(dst, exist_ok=True)
                for item in os.listdir(src):
                    s = os.path.join(src, item)
                    d = os.path.join(dst, item)
                    if os.path.isdir(s):
                        shutil.copytree(s, d, dirs_exist_ok=True)
                    else:
                        shutil.copy2(s, d)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            return True
        except (OSError, shutil.Error) as e:
            logging.debug("Default copy failed from %s to %s: %s", src, dst, e)
            return False

    @property
    def supports_cow(self):
        return False

    @property
    def fstype(self):
        return self._fstype


class LinuxFilesystemProvider(FilesystemProvider):
    """Linux filesystem provider using GNU cp --reflink=auto."""

    def __init__(self, fallback=None, fstype=None):
        self._fallback = fallback or DefaultFilesystemProvider()
        self._fstype = fstype

    def copy(self, src, dst):
        try:
            if os.path.isdir(src):
                os.makedirs(dst, exist_ok=True)
                res = subprocess.run(
                    ["cp", "-a", "--reflink=auto", os.path.join(src, "."), dst],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if res.returncode == 0:
                    return True
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                res = subprocess.run(
                    ["cp", "-a", "--reflink=auto", src, dst],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if res.returncode == 0:
                    return True
        except (subprocess.SubprocessError, OSError) as e:
            logging.debug("Linux reflink failed, falling back to copy: %s", e)

        return self._fallback.copy(src, dst)

    @property
    def supports_cow(self):
        if self._fstype in ("ext4", "ext3", "ext2", "vfat", "fat", "ntfs"):
            return False
        return True

    @property
    def fstype(self):
        return self._fstype or "linux"


class BtrfsFilesystemProvider(LinuxFilesystemProvider):
    """Btrfs filesystem provider supporting subvolume snapshots and reflink cloning."""

    def __init__(self, fallback=None):
        super().__init__(fallback=fallback, fstype="btrfs")

    def copy(self, src, dst):
        if is_btrfs_subvolume(src):
            if snapshot_btrfs_subvolume(src, dst):
                return True
            logging.debug(
                "Btrfs subvolume snapshot failed from %s to %s, "
                "falling back to Linux reflink copy",
                src,
                dst,
            )
        return super().copy(src, dst)

    @property
    def supports_snapshots(self):
        return True

    @property
    def fstype(self):
        return "btrfs"


class APFSFilesystemProvider(FilesystemProvider):
    """macOS filesystem provider using APFS copy-on-write (cp -c)."""

    def __init__(self, fallback=None):
        self._fallback = fallback or DefaultFilesystemProvider()

    def copy(self, src, dst):
        try:
            if os.path.isdir(src):
                os.makedirs(dst, exist_ok=True)
                res = subprocess.run(
                    ["cp", "-R", "-p", "-c", os.path.join(src, "."), dst],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if res.returncode == 0:
                    return True
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                res = subprocess.run(
                    ["cp", "-p", "-c", src, dst],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if res.returncode == 0:
                    return True
        except (subprocess.SubprocessError, OSError) as e:
            logging.debug(
                "macOS APFS clone failed, falling back to copy: %s", e
            )

        return self._fallback.copy(src, dst)

    @property
    def supports_cow(self):
        return True

    @property
    def fstype(self):
        return "apfs"


def get_linux_filesystem_type(path):
    """Detects the filesystem type for a path on Linux."""
    target = os.path.abspath(path)
    while not os.path.exists(target):
        parent = os.path.dirname(target)
        if parent == target:
            break
        target = parent
    try:
        res = subprocess.run(
            ["findmnt", "-no", "FSTYPE", "--target", target],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().lower()
    except (subprocess.SubprocessError, OSError) as e:
        logging.debug("Failed to query filesystem type via findmnt: %s", e)

    # Fallback to reading /proc/mounts if findmnt is unavailable
    if os.path.isfile("/proc/mounts"):
        try:
            norm_target = target.replace("\\", "/")
            if (
                len(norm_target) >= 2
                and norm_target[1] == ":"
                and norm_target[0].isalpha()
            ):
                norm_target = norm_target[2:]
            best_match = ""
            best_fstype = None
            with open("/proc/mounts", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3:
                        mountpoint, fstype = parts[1], parts[2]
                        if norm_target == mountpoint or norm_target.startswith(
                            mountpoint.rstrip("/") + "/"
                        ):
                            if len(mountpoint) > len(best_match):
                                best_match = mountpoint
                                best_fstype = fstype
            if best_fstype:
                return best_fstype.lower()
        except OSError as e:
            logging.debug("Failed to read /proc/mounts: %s", e)

    return None


def get_darwin_filesystem_type(path):
    """Detects the filesystem type for a path on macOS."""
    target = os.path.abspath(path)
    while not os.path.exists(target):
        parent = os.path.dirname(target)
        if parent == target:
            break
        target = parent
    try:
        res = subprocess.run(
            ["stat", "-f", "%t", target],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().lower()
    except (subprocess.SubprocessError, OSError) as e:
        logging.debug("Failed to query Darwin filesystem type via stat: %s", e)

    return None


def get_filesystem_type(path=None, platform=None):
    """Detects the filesystem type for the specified path and platform.

    Args:
        path: Path to inspect (defaults to current working directory).
        platform: Platform name override (defaults to sys.platform).

    Returns:
        Filesystem name string in lowercase (e.g. 'btrfs', 'ext4', 'apfs') or
        None if undetected.
    """
    plat = platform or sys.platform
    target = path or os.getcwd()

    if plat.startswith("linux"):
        return get_linux_filesystem_type(target)
    if plat == "darwin":
        return get_darwin_filesystem_type(target)
    return None


def is_btrfs_subvolume(path):
    """Returns True if path is the root of a Btrfs subvolume or snapshot."""
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            return False
        st = os.stat(path)
        if st.st_ino != 256:
            return False
        return get_filesystem_type(path) == "btrfs"
    except OSError as e:
        logging.debug("Error checking if %s is btrfs subvolume: %s", path, e)
        return False


def create_btrfs_subvolume(path):
    """Creates a Btrfs subvolume at path.

    Args:
        path: Path where the subvolume will be created.

    Returns:
        True if the subvolume was created successfully, False otherwise.
    """
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        res = subprocess.run(
            ["btrfs", "subvolume", "create", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return True
        logging.debug(
            "btrfs subvolume create failed for %s: %s", path, res.stderr
        )
        return False
    except (subprocess.SubprocessError, OSError) as e:
        logging.debug("Exception creating btrfs subvolume at %s: %s", path, e)
        return False


def delete_btrfs_subvolume(path, recursive=True):
    """Deletes a Btrfs subvolume at path.

    Args:
        path: Path to the subvolume to delete.
        recursive: If True, delete nested subvolumes recursively.

    Returns:
        True if the subvolume was deleted successfully, False otherwise.
    """
    if not os.path.exists(path) and not os.path.islink(path):
        return True
    try:
        cmd = ["btrfs", "subvolume", "delete"]
        if recursive:
            cmd.append("-R")
        cmd.append(path)
        res = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return True
        logging.debug(
            "btrfs subvolume delete failed for %s: %s", path, res.stderr
        )
        # If recursive deletion failed with -R (e.g. older btrfs-progs or
        # non-root iteration limits), traverse bottom-up to delete nested
        # subvolumes first, then retry deleting the root subvolume.
        if recursive and os.path.exists(path):
            for root, dirs, _ in os.walk(path, topdown=False):
                for d in dirs:
                    sub = os.path.join(root, d)
                    if is_btrfs_subvolume(sub):
                        delete_btrfs_subvolume(sub, recursive=False)
            res2 = subprocess.run(
                ["btrfs", "subvolume", "delete", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if res2.returncode == 0:
                return True
            logging.debug(
                "btrfs subvolume delete retry failed for %s: %s",
                path,
                res2.stderr,
            )
        return False
    except (subprocess.SubprocessError, OSError) as e:
        logging.debug("Exception deleting btrfs subvolume at %s: %s", path, e)
        return False


def remove_path(path):
    """Removes a file, directory, or subvolume at path."""
    if not os.path.exists(path) and not os.path.islink(path):
        return True
    if is_btrfs_subvolume(path):
        return delete_btrfs_subvolume(path, recursive=True)
    if os.path.isdir(path) and not os.path.islink(path):
        try:
            shutil.rmtree(path)
            return True
        except OSError as e:
            logging.debug(
                "shutil.rmtree failed for %s: %s, cleaning nested subvolumes",
                path,
                e,
            )
            for root, dirs, _ in os.walk(path, topdown=False):
                for d in dirs:
                    sub = os.path.join(root, d)
                    if is_btrfs_subvolume(sub):
                        delete_btrfs_subvolume(sub, recursive=True)
            try:
                shutil.rmtree(path)
                return True
            except OSError as e2:
                logging.debug(
                    "Second shutil.rmtree failed for %s: %s", path, e2
                )
                return False
    else:
        try:
            os.remove(path)
            return True
        except OSError as e:
            logging.debug("Failed to remove %s: %s", path, e)
            return False


def snapshot_btrfs_subvolume(src, dst):
    """Creates a Btrfs snapshot of src at dst.

    If dst exists, it is removed prior to creating the snapshot.

    Args:
        src: Path to source subvolume.
        dst: Path to destination snapshot.

    Returns:
        True if the snapshot succeeded, False otherwise.
    """
    try:
        if os.path.exists(dst) or os.path.islink(dst):
            if not remove_path(dst):
                return False
        parent = os.path.dirname(os.path.abspath(dst))
        if parent:
            os.makedirs(parent, exist_ok=True)
        res = subprocess.run(
            ["btrfs", "subvolume", "snapshot", src, dst],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return True
        logging.debug(
            "btrfs subvolume snapshot failed from %s to %s: %s",
            src,
            dst,
            res.stderr,
        )
        return False
    except (subprocess.SubprocessError, OSError) as e:
        logging.debug(
            "Exception snapshotting btrfs subvolume from %s to %s: %s",
            src,
            dst,
            e,
        )
        return False


def ensure_directory_or_subvolume(path):
    """Ensures path exists as a directory or Btrfs subvolume.

    If path does not exist and its parent resides on Btrfs, attempts to create
    it as a Btrfs subvolume so subsequent operations can use subvolume snapshots.
    Falls back to os.makedirs if subvolume creation is not supported or fails.

    Args:
        path: Path to create.

    Returns:
        True if path exists as a directory or subvolume, False otherwise.
    """
    if os.path.isdir(path):
        return True
    try:
        fstype = get_filesystem_type(os.path.dirname(os.path.abspath(path)))
        if fstype == "btrfs":
            if create_btrfs_subvolume(path):
                return True
            logging.debug(
                "Failed to create btrfs subvolume at %s, falling back to makedirs",
                path,
            )
        os.makedirs(path, exist_ok=True)
        return os.path.isdir(path)
    except OSError as e:
        logging.debug(
            "Failed to ensure directory or subvolume at %s: %s", path, e
        )
        return False


def get_filesystem_provider(path=None, platform=None):
    """Factory returning the filesystem provider for the path and platform.

    Args:
        path: Optional path to inspect filesystem capabilities.
        platform: Optional platform string override.

    Returns:
        FilesystemProvider instance.
    """
    if (
        path
        and platform is None
        and path
        in (
            "linux",
            "linux2",
            "darwin",
            "win32",
            "cygwin",
            "other",
        )
    ):
        platform = path
        path = None

    plat = platform or sys.platform
    if plat.startswith("linux"):
        fstype = get_linux_filesystem_type(path) if path else None
        if fstype == "btrfs":
            return BtrfsFilesystemProvider()
        return LinuxFilesystemProvider(fstype=fstype)
    if plat == "darwin":
        return APFSFilesystemProvider()
    return DefaultFilesystemProvider()


def reflink_or_copy(src, dst, provider=None):
    """Reflinks or copies src to dst using CoW where supported, with fallback."""
    provider = provider or get_filesystem_provider(path=src)
    return provider.copy(src, dst)


# vim: ts=2:sw=2:tw=80:et:
