#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Runs gsutil with a workaround for slow GCE credential setup.

Usage: gsutil_launcher.py <path to gsutil> [gsutil args...]

gsutil.py uses this to run gsutil on the pinned Windows CPython (see
gsutil.WINDOWS_PYTHON_VERSION).

gsutil's vendored apitools (credentials_lib._GceMetadataRequest and
util.DetectGce) builds a new urllib opener for every GCE metadata request.
Since CPython 3.12, building an
opener eagerly creates a default SSLContext, which on Windows loads the system
certificate stores. With GCE credentials every `gsutil -m` worker thread does
several metadata requests, so parallel gsutil processes spend most of their
time loading certificate stores in the kernel (b/564326771). Reuse a single
opener for those requests instead; it only handles plain-HTTP metadata
requests and is safe to share between threads.

If a gsutil update changes apitools so this no longer applies, a warning is
printed instead of silently doing nothing.

TODO(b/564326771): Remove once the gsutil version pinned in gsutil.py reuses
its GCE metadata opener (apitools credentials_lib and util.DetectGce).
"""

import importlib.abc
import importlib.machinery
import os
import runpy
import sys
import threading
import urllib.request

_CREDENTIALS_LIB = "apitools.base.py.credentials_lib"
_UTIL = "apitools.base.py.util"


class _SharedMetadataOpenerRequest:
    """Proxies urllib.request, sharing the opener apitools builds per request.

    Only build_opener(ProxyHandler({})), which is what _GceMetadataRequest and
    DetectGce call, is shared; everything else is delegated to the real module.
    """

    def __init__(self, request):
        self._request = request
        self._lock = threading.Lock()
        self._opener = None

    def __getattr__(self, name):
        return getattr(self._request, name)

    def build_opener(self, *handlers):
        if (
            len(handlers) == 1
            and isinstance(handlers[0], self._request.ProxyHandler)
            and not handlers[0].proxies
        ):
            with self._lock:
                if self._opener is None:
                    self._opener = self._request.build_opener(*handlers)
                return self._opener
        return self._request.build_opener(*handlers)


class _UrllibProxy:
    """Proxies a urllib module, replacing its `request` submodule."""

    def __init__(self, urllib, request):
        self._urllib = urllib
        self.request = request

    def __getattr__(self, name):
        return getattr(self._urllib, name)


def _builds_opener(module, fn_name, attr):
    fn = getattr(module, fn_name, None)
    if (
        fn is None
        or not hasattr(module, attr)
        or "build_opener" not in fn.__code__.co_names
    ):
        sys.stderr.write(
            "Warning: gsutil_launcher.py: %s.%s no longer builds a urllib "
            "opener per request; the b/564326771 workaround has no effect "
            "and may no longer be needed.\n" % (module.__name__, fn_name)
        )
        return False
    return True


def patch_credentials_lib(module, request):
    """Makes credentials_lib._GceMetadataRequest use `request`'s opener.

    Returns whether the module was patched. Warns and leaves the module alone
    if it no longer builds an opener per metadata request (e.g. after a gsutil
    update), since the workaround then has no effect.
    """
    if not _builds_opener(module, "_GceMetadataRequest", "urllib"):
        return False
    module.urllib = _UrllibProxy(module.urllib, request)
    return True


def patch_util(module, request):
    """Makes util.DetectGce use `request`'s opener.

    See patch_credentials_lib.
    """
    if not _builds_opener(module, "DetectGce", "urllib_request"):
        return False
    module.urllib_request = request
    return True


class _PatchingFinder(importlib.abc.MetaPathFinder):
    """Patches apitools modules right after they're imported.

    All patched modules share one metadata opener.
    """

    def __init__(self):
        self._request = _SharedMetadataOpenerRequest(urllib.request)
        self._patchers = {
            _CREDENTIALS_LIB: patch_credentials_lib,
            _UTIL: patch_util,
        }

    def find_spec(self, fullname, path, target=None):
        patcher = self._patchers.get(fullname)
        if patcher is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        exec_module = spec.loader.exec_module
        request = self._request

        def patched_exec_module(module):
            exec_module(module)
            patcher(module, request)

        spec.loader.exec_module = patched_exec_module
        return spec


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    gsutil_bin = argv[1]
    sys.meta_path.insert(0, _PatchingFinder())
    sys.argv = [gsutil_bin] + argv[2:]
    sys.path.insert(0, os.path.dirname(gsutil_bin))
    runpy.run_path(gsutil_bin, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
