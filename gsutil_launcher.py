#!/usr/bin/env python3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Runs gsutil with a workaround for slow GCE credential setup.

Usage: gsutil_launcher.py <path to gsutil> [gsutil args...]

gsutil.py uses this to run gsutil on the pinned Windows CPython (see
gsutil.WINDOWS_PYTHON_VERSION).

gsutil's vendored apitools (credentials_lib._GceMetadataRequest) builds a new
urllib opener for every GCE metadata request. Since CPython 3.12, building an
opener eagerly creates a default SSLContext, which on Windows loads the system
certificate stores. With GCE credentials every `gsutil -m` worker thread does
several metadata requests, so parallel gsutil processes spend most of their
time loading certificate stores in the kernel (b/564326771). Reuse a single
opener for those requests instead; it only handles plain-HTTP metadata
requests and is safe to share between threads.

If a gsutil update changes credentials_lib so this no longer applies, a warning
is printed instead of silently doing nothing.

TODO(b/564326771): Remove once the gsutil version pinned in gsutil.py reuses
its GCE metadata opener (apitools credentials_lib).
"""

import importlib.abc
import importlib.machinery
import os
import runpy
import sys
import threading

_CREDENTIALS_LIB = "apitools.base.py.credentials_lib"


class _SharedMetadataOpenerRequest:
    """Proxies urllib.request, sharing the opener apitools builds per request.

    Only build_opener(ProxyHandler({})), which is what _GceMetadataRequest
    calls, is shared; everything else is delegated to the real module.
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

    def __init__(self, urllib):
        self._urllib = urllib
        self.request = _SharedMetadataOpenerRequest(urllib.request)

    def __getattr__(self, name):
        return getattr(self._urllib, name)


def patch_credentials_lib(module):
    """Makes credentials_lib reuse one opener for GCE metadata requests.

    Returns whether the module was patched. Warns and leaves the module alone
    if it no longer builds an opener per metadata request (e.g. after a gsutil
    update), since the workaround then has no effect.
    """
    request_fn = getattr(module, "_GceMetadataRequest", None)
    if (
        request_fn is None
        or not hasattr(module, "urllib")
        or "build_opener" not in request_fn.__code__.co_names
    ):
        sys.stderr.write(
            "Warning: gsutil_launcher.py: %s._GceMetadataRequest no longer "
            "builds a urllib opener per request; the b/564326771 workaround "
            "has no effect and may no longer be needed.\n" % _CREDENTIALS_LIB
        )
        return False
    module.urllib = _UrllibProxy(module.urllib)
    return True


class _PatchingFinder(importlib.abc.MetaPathFinder):
    """Patches credentials_lib right after it's imported."""

    def find_spec(self, fullname, path, target=None):
        if fullname != _CREDENTIALS_LIB:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        exec_module = spec.loader.exec_module

        def patched_exec_module(module):
            exec_module(module)
            patch_credentials_lib(module)

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
