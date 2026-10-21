#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Unit tests for GcsDependency and central CAS caching in gclient.py."""

import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import download_from_google_storage
import gclient
import git_cache


class GcsDependencyTest(unittest.TestCase):
    def setUp(self):
        super(GcsDependencyTest, self).setUp()
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        git_cache.Mirror.SetCachePath(None)
        self.addCleanup(git_cache.Mirror.SetCachePath, None)

        parser = gclient.OptionParser()
        options, _ = parser.parse_args([])
        self.options = options
        self.client = gclient.GClient(root_dir=self.tmpdir, options=options)
        self.parent = gclient.Dependency(
            parent=self.client,
            name="src",
            url="https://example.com/repo.git",
            managed=False,
            custom_deps=None,
            custom_vars=None,
            custom_hooks=None,
            deps_file="DEPS",
            should_process=True,
            should_recurse=False,
            relative=False,
            condition=None,
        )

    def _make_gcs_dep(
        self,
        name="src/dep",
        bucket="test_bucket",
        object_name="pkg.tar.gz",
        sha256sum="1234567890abcdef" * 4,
        output_file=None,
        size_bytes=100,
        should_process=True,
    ):
        return gclient.GcsDependency(
            parent=self.parent,
            name=name,
            bucket=bucket,
            object_name=object_name,
            sha256sum=sha256sum,
            output_file=output_file,
            size_bytes=size_bytes,
            gcs_root=self.client.GetGcsRoot(),
            custom_vars=None,
            should_process=should_process,
            relative=False,
            condition=None,
        )

    def _create_tar_file(self, path, files_dict):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with tarfile.open(path, "w:gz") as tar:
            for arcname, content in files_dict.items():
                data = (
                    content.encode("utf-8")
                    if isinstance(content, str)
                    else content
                )
                ti = tarfile.TarInfo(name=arcname)
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))

    def _create_zip_file(self, path, files_dict):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, "w") as zf:
            for arcname, content in files_dict.items():
                zf.writestr(arcname, content)

    def test_properties(self):
        dep = self._make_gcs_dep(
            name="src/custom_dir",
            object_name="subdir/file.bin",
            output_file="renamed.bin",
        )
        self.assertEqual(
            dep.output_dir, os.path.join(self.tmpdir, "src", "custom_dir")
        )
        self.assertEqual(dep.gcs_file_name, "subdir_file.bin")
        self.assertEqual(dep.file_prefix, "subdir_file_bin")
        self.assertEqual(
            dep.artifact_output_file,
            os.path.join(dep.output_dir, "renamed.bin"),
        )
        self.assertEqual(
            dep.hash_file,
            os.path.join(dep.output_dir, ".subdir_file_bin_hash"),
        )
        self.assertEqual(
            dep.migration_toggle_file,
            os.path.join(
                dep.output_dir,
                download_from_google_storage.construct_migration_file_name(
                    "subdir/file.bin"
                ),
            ),
        )
        self.assertEqual(dep.GetScmName(), "gcs")
        self.assertTrue(dep.verify_validity())

    def test_artifact_output_file_default_name(self):
        dep = self._make_gcs_dep(
            name="src/dep", object_name="tools/binary.tar.gz", output_file=None
        )
        self.assertEqual(
            dep.artifact_output_file,
            os.path.join(dep.output_dir, ".tools_binary.tar.gz"),
        )

    def test_maybe_extract_artifact_plain_file(self):
        dep = self._make_gcs_dep(object_name="bundle.txt")
        os.makedirs(dep.output_dir, exist_ok=True)
        with open(dep.artifact_output_file, "w") as f:
            f.write("plain text")
        dep._MaybeExtractArtifact()
        # Does not create content names file for non-archive
        content_names_file = os.path.join(
            dep.output_dir, f".{dep.file_prefix}_content_names"
        )
        self.assertFalse(os.path.exists(content_names_file))

    def test_maybe_extract_artifact_tar(self):
        dep = self._make_gcs_dep(object_name="pkg.tar.gz")
        self._create_tar_file(
            dep.artifact_output_file,
            {
                "pkg/file1.txt": "content 1",
                "pkg/file2.txt": "content 2",
            },
        )
        dep._MaybeExtractArtifact()
        file1 = os.path.join(dep.output_dir, "pkg", "file1.txt")
        file2 = os.path.join(dep.output_dir, "pkg", "file2.txt")
        self.assertTrue(os.path.isfile(file1))
        self.assertTrue(os.path.isfile(file2))
        with open(file1, "r") as f:
            self.assertEqual(f.read(), "content 1")
        with open(file2, "r") as f:
            self.assertEqual(f.read(), "content 2")

        content_names_file = os.path.join(
            dep.output_dir, f".{dep.file_prefix}_content_names"
        )
        self.assertTrue(os.path.isfile(content_names_file))
        with open(content_names_file, "r") as f:
            names = json.loads(f.read())
        self.assertIn("pkg/file1.txt", names)
        self.assertIn("pkg/file2.txt", names)

    def test_maybe_extract_artifact_zip(self):
        dep = self._make_gcs_dep(object_name="pkg.zip")
        self._create_zip_file(
            dep.artifact_output_file,
            {
                "pkg/zip1.txt": "zip content 1",
                "pkg/zip2.txt": "zip content 2",
            },
        )
        dep._MaybeExtractArtifact()
        file1 = os.path.join(dep.output_dir, "pkg", "zip1.txt")
        file2 = os.path.join(dep.output_dir, "pkg", "zip2.txt")
        self.assertTrue(os.path.isfile(file1))
        self.assertTrue(os.path.isfile(file2))
        with open(file1, "r") as f:
            self.assertEqual(f.read(), "zip content 1")
        with open(file2, "r") as f:
            self.assertEqual(f.read(), "zip content 2")

        content_names_file = os.path.join(
            dep.output_dir, f".{dep.file_prefix}_content_names"
        )
        self.assertTrue(os.path.isfile(content_names_file))
        with open(content_names_file, "r") as f:
            names = json.loads(f.read())
        self.assertIn("pkg/zip1.txt", names)
        self.assertIn("pkg/zip2.txt", names)

    def test_maybe_extract_artifact_tar_traversal_rejected(self):
        dep = self._make_gcs_dep(object_name="bad.tar.gz")
        os.makedirs(dep.output_dir, exist_ok=True)
        with tarfile.open(dep.artifact_output_file, "w:gz") as tar:
            data = b"malicious"
            ti = tarfile.TarInfo("../escaped.txt")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))

        with self.assertRaises(Exception):
            dep._MaybeExtractArtifact()

    def test_is_download_needed(self):
        dep = self._make_gcs_dep()
        self.assertTrue(dep.IsDownloadNeeded())

        os.makedirs(dep.output_dir, exist_ok=True)
        with open(dep.artifact_output_file, "w") as f:
            f.write("content")
        self.assertTrue(dep.IsDownloadNeeded())

        with open(dep.hash_file, "w") as f:
            f.write(dep.sha256sum + "\n")
        self.assertTrue(dep.IsDownloadNeeded())

        with open(dep.migration_toggle_file, "w") as f:
            f.write("1\n")
        self.assertFalse(dep.IsDownloadNeeded())

        with open(dep.hash_file, "w") as f:
            f.write("mismatched_hash\n")
        self.assertTrue(dep.IsDownloadNeeded())

    def test_download_google_storage_no_download_needed(self):
        dep = self._make_gcs_dep()
        os.makedirs(dep.output_dir, exist_ok=True)
        with open(dep.artifact_output_file, "w") as f:
            f.write("artifact")
        with open(dep.hash_file, "w") as f:
            f.write(dep.sha256sum + "\n")
        with open(dep.migration_toggle_file, "w") as f:
            f.write("1\n")

        with mock.patch.object(dep, "_DownloadAndExtract") as mock_dl:
            dep.DownloadGoogleStorage(self.options)
            mock_dl.assert_not_called()

    def test_download_google_storage_fallback_to_test_download(self):
        dep = self._make_gcs_dep(
            object_name="archive.tar.gz",
            sha256sum="abcd123",
            size_bytes=10000,
        )
        with mock.patch.dict(os.environ, {"GCLIENT_TEST": "1"}):
            dep.DownloadGoogleStorage(self.options)

        self.assertTrue(os.path.isfile(dep.artifact_output_file))
        self.assertTrue(os.path.isfile(dep.hash_file))
        self.assertTrue(os.path.isfile(dep.migration_toggle_file))
        with open(dep.hash_file, "r") as f:
            self.assertEqual(f.read().strip(), "abcd123")

    def test_run_skips_runhooks_and_revinfo(self):
        dep = self._make_gcs_dep()
        with mock.patch.object(dep, "DownloadGoogleStorage") as mock_download:
            dep.run(None, "runhooks", [], None, self.options, None, None, None)
            mock_download.assert_not_called()

            dep.run(None, "revinfo", [], None, self.options, None, None, None)
            mock_download.assert_not_called()

    def test_run_skips_when_should_process_false(self):
        dep = self._make_gcs_dep(should_process=False)
        with mock.patch.object(dep, "DownloadGoogleStorage") as mock_download:
            dep.run(None, "sync", [], None, self.options, None, None, None)
            mock_download.assert_not_called()

    def test_run_executes_download(self):
        dep = self._make_gcs_dep(should_process=True)
        with mock.patch.object(dep, "DownloadGoogleStorage") as mock_download:
            with mock.patch.object(
                gclient.Dependency, "run", return_value=None
            ):
                dep.run(None, "sync", [], None, self.options, None, None, None)
                mock_download.assert_called_once_with(self.options)

    def test_cache_dir_from_set_config(self):
        cache_dir = os.path.join(self.tmpdir, "config_cache")
        self.client.SetConfig(f'cache_dir = {cache_dir!r}\nsolutions = []\n')
        dep = self._make_gcs_dep()
        self.assertEqual(dep.cache_dir, os.path.abspath(cache_dir))

    def test_cache_dir_from_gclient_cache_dir_env_var(self):
        cache_dir = os.path.join(self.tmpdir, "env_cache")
        with mock.patch.dict(os.environ, {"GCLIENT_CACHE_DIR": cache_dir}):
            self.client.SetConfig("solutions = []\n")
        dep = self._make_gcs_dep()
        self.assertEqual(dep.cache_dir, os.path.abspath(cache_dir))

    def test_cache_dir_git_cache_mirror(self):
        cache_dir = os.path.join(self.tmpdir, "mirror_cache")
        git_cache.Mirror.SetCachePath(cache_dir)
        dep = self._make_gcs_dep()
        self.assertEqual(dep.cache_dir, os.path.abspath(cache_dir))

    def test_cache_dir_unset_returns_none(self):
        dep = self._make_gcs_dep()
        with mock.patch.object(
            git_cache.Mirror,
            "GetCachePath",
            side_effect=RuntimeError("unset"),
        ):
            self.assertIsNone(dep.cache_dir)

    def test_download_google_storage_populates_archive_cache(self):
        cache_dir = os.path.join(self.tmpdir, "cache")
        git_cache.Mirror.SetCachePath(cache_dir)
        dep = self._make_gcs_dep(
            object_name="archive.tar.gz",
            sha256sum="abcd123",
            size_bytes=10000,
        )
        with mock.patch.dict(os.environ, {"GCLIENT_TEST": "1"}):
            dep.DownloadGoogleStorage(self.options)

        cache_archive = dep._GetCacheArchiveFile(cache_dir)
        self.assertTrue(os.path.isfile(cache_archive))
        self.assertTrue(os.path.exists(f"{cache_archive}.ready"))

        # Workspace target should have extracted files and hash
        self.assertTrue(
            os.path.exists(
                os.path.join(dep.output_dir, "extracted_dir", "extracted_file")
            )
        )
        self.assertTrue(os.path.exists(dep.hash_file))
        with open(dep.hash_file, "r") as f:
            self.assertEqual(f.read().strip(), "abcd123")

    def test_download_google_storage_reuses_from_archive_cache(self):
        cache_dir = os.path.join(self.tmpdir, "cache")
        git_cache.Mirror.SetCachePath(cache_dir)
        dep = self._make_gcs_dep(
            object_name="pkg.tar.gz",
            sha256sum="abcd123",
            size_bytes=10,
        )
        cache_archive = dep._GetCacheArchiveFile(cache_dir)
        os.makedirs(os.path.dirname(cache_archive), exist_ok=True)
        with open(cache_archive, "w") as f:
            f.write("0123456789")
        with open(f"{cache_archive}.ready", "w") as f:
            f.write("ready\n")

        with mock.patch.object(
            dep, "_PopulateFromArchiveCache", return_value=True
        ) as mock_pop:
            with mock.patch.object(dep, "_DownloadAndExtract") as mock_dl:
                dep.DownloadGoogleStorage(self.options)
                mock_pop.assert_called_once_with(cache_archive)
                mock_dl.assert_not_called()


if __name__ == "__main__":
    unittest.main()

# vim: ts=2:sw=2:tw=80:et:
