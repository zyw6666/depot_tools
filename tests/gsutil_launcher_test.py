#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Tests for gsutil_launcher.py."""

import importlib
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock
import urllib.request

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEPOT_TOOLS_DIR = os.path.dirname(THIS_DIR)
sys.path.append(DEPOT_TOOLS_DIR)

import gsutil_launcher  # noqa: E402

# A stand-in for apitools' credentials_lib, which calls
# urllib.request.build_opener(urllib.request.ProxyHandler({})) per request.
FAKE_CREDENTIALS_LIB = """
import urllib.request


def _GceMetadataRequest(relative_url):
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))
"""


class SharedMetadataOpenerTest(unittest.TestCase):
    def setUp(self):
        self.proxy = gsutil_launcher._UrllibProxy(urllib)

    def test_shares_metadata_opener(self):
        build = self.proxy.request.build_opener
        ProxyHandler = self.proxy.request.ProxyHandler
        opener = build(ProxyHandler({}))
        self.assertIsInstance(opener, urllib.request.OpenerDirector)
        self.assertIs(build(ProxyHandler({})), opener)

    def test_does_not_share_other_openers(self):
        build = self.proxy.request.build_opener
        ProxyHandler = self.proxy.request.ProxyHandler
        shared = build(ProxyHandler({}))
        self.assertIsNot(build(), shared)
        self.assertIsNot(build(), build())
        with_proxy = build(ProxyHandler({"http": "http://proxy:3128"}))
        self.assertIsNot(with_proxy, shared)

    def test_delegates_everything_else(self):
        self.assertIs(self.proxy.request.Request, urllib.request.Request)
        self.assertIs(self.proxy.parse, urllib.parse)

    def test_patches_credentials_lib_on_import(self):
        tempdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tempdir)
        pkg = os.path.join(tempdir, "apitools", "base", "py")
        os.makedirs(pkg)
        for d in ("apitools", "apitools/base", "apitools/base/py"):
            open(os.path.join(tempdir, d, "__init__.py"), "w").close()
        with open(os.path.join(pkg, "credentials_lib.py"), "w") as f:
            f.write(FAKE_CREDENTIALS_LIB)

        finder = gsutil_launcher._PatchingFinder()
        saved_modules = dict(sys.modules)
        with (
            mock.patch.object(sys, "path", [tempdir] + sys.path),
            mock.patch.object(sys, "meta_path", [finder] + sys.meta_path),
        ):
            try:
                module = importlib.import_module(
                    "apitools.base.py.credentials_lib"
                )
                self.assertIsInstance(
                    module.urllib, gsutil_launcher._UrllibProxy
                )
                self.assertIs(
                    module._GceMetadataRequest("a"),
                    module._GceMetadataRequest("b"),
                )
            finally:
                for name in list(sys.modules):
                    if name not in saved_modules:
                        del sys.modules[name]


class PatchCredentialsLibTest(unittest.TestCase):
    def _module(self, source):
        module = types.ModuleType("credentials_lib")
        exec(source, module.__dict__)
        return module

    def test_patches_matching_module(self):
        module = self._module(FAKE_CREDENTIALS_LIB)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertTrue(gsutil_launcher.patch_credentials_lib(module))
        self.assertEqual(stderr.getvalue(), "")
        self.assertIsInstance(module.urllib, gsutil_launcher._UrllibProxy)

    def test_warns_if_metadata_request_is_gone(self):
        module = self._module("import urllib.request\n")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertFalse(gsutil_launcher.patch_credentials_lib(module))
        self.assertIn("b/564326771", stderr.getvalue())
        self.assertIs(module.urllib, urllib)

    def test_warns_if_opener_is_no_longer_built_per_request(self):
        module = self._module(
            "import urllib.request\n"
            "_OPENER = urllib.request.build_opener()\n"
            "def _GceMetadataRequest(relative_url):\n"
            "    return _OPENER\n"
        )
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertFalse(gsutil_launcher.patch_credentials_lib(module))
        self.assertIn("no longer builds a urllib opener", stderr.getvalue())
        self.assertIs(module.urllib, urllib)


if __name__ == "__main__":
    unittest.main()
