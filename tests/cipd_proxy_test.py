#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Tests Windows CIPD bootstrap proxy parameters without using the network."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POWERSHELL = shutil.which("powershell" if sys.platform == "win32" else "pwsh")
CLIENT_CONTENT = b"fake cipd client"

# Mock the download command, but run the actual bootstrap script, including its
# digest check and file move. The typed parameters verify credential binding.
HARNESS = r"""
param($BootstrapScript, $CipdBinary, $VersionFile, $BackendURL)
$ErrorActionPreference = 'Stop'
function Invoke-WebRequest {
  param(
    [string]$UserAgent,
    [uri]$Uri,
    [string]$OutFile,
    [uri]$Proxy,
    [pscredential]$ProxyCredential,
    [pscredential]$Credential
  )
  $global:Request = @{
    Uri = $Uri.AbsoluteUri
    HasProxy = $PSBoundParameters.ContainsKey('Proxy')
    HasProxyCredential = $PSBoundParameters.ContainsKey('ProxyCredential')
    HasCredential = $PSBoundParameters.ContainsKey('Credential')
  }
  if ($Proxy) {
    $global:Request.Proxy = $Proxy.AbsoluteUri
  }
  if ($ProxyCredential) {
    $global:Request.Username = $ProxyCredential.UserName
    $global:Request.Password = $ProxyCredential.GetNetworkCredential().Password
  }
  Set-Content -LiteralPath $OutFile -Value 'fake cipd client' `
      -Encoding Ascii -NoNewline
}
if ($Env:TEST_CONSTRAINED_LANGUAGE) {
  # PowerShell 5.1 implements Get-FileHash using a script that creates .NET
  # objects. Delegate to it in full language, keeping the digest check real
  # while testing only the bootstrap script in constrained language.
  function Get-TestFileHash {
    param($Path, $Algorithm)
    Microsoft.PowerShell.Utility\Get-FileHash @PSBoundParameters
  }
  Set-Alias Get-FileHash Get-TestFileHash
  $ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'
}
& $BootstrapScript -CipdBinary $CipdBinary -VersionFile $VersionFile `
    -BackendURL $BackendURL
ConvertTo-Json -InputObject $global:Request -Compress
"""


@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class CipdProxyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="depot_tools_proxy_")
        self.addCleanup(self.tempdir.cleanup)
        self.binary = os.path.join(self.tempdir.name, "cipd.exe")
        self.version_file = os.path.join(self.tempdir.name, "version")
        self.harness_file = os.path.join(self.tempdir.name, "harness.ps1")
        self._write(self.version_file, "test-version\n")
        digest = hashlib.sha256(CLIENT_CONTENT).hexdigest()
        self._write(
            self.version_file + ".digests", f"windows-amd64 sha256 {digest}\n"
        )

    @staticmethod
    def _write(path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as output:
            output.write(content)

    def _bootstrap(
        self,
        env: dict[str, str] | None = None,
        backend: str = "https://packages.example.com",
        constrained: bool = False,
    ) -> tuple[dict, str]:
        self._write(self.harness_file, HARNESS)
        process_env = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            not in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
                "TEST_CONSTRAINED_LANGUAGE",
            )
        }
        process_env.update(env or {})
        if constrained:
            process_env["TEST_CONSTRAINED_LANGUAGE"] = "1"
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                self.harness_file,
                "-BootstrapScript",
                os.path.join(ROOT_DIR, ".cipd_impl.ps1"),
                "-CipdBinary",
                self.binary,
                "-VersionFile",
                self.version_file,
                "-BackendURL",
                backend,
            ],
            env=process_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        with open(self.binary, "rb") as binary:
            self.assertEqual(binary.read(), CLIENT_CONTENT)
        request = json.loads(result.stdout.splitlines()[-1])
        self.assertFalse(request["HasCredential"])
        self.assertEqual(
            request["Uri"],
            f"{backend}/client?platform=windows-amd64&version=test-version",
        )
        return request, result.stdout

    def test_no_proxy_configuration(self) -> None:
        request, _ = self._bootstrap()
        self.assertFalse(request["HasProxy"])
        self.assertFalse(request["HasProxyCredential"])

    def test_proxy_environment_precedence(self) -> None:
        cases = [
            ({"HTTP_PROXY": "http://http.example:8118"}, "http.example"),
            ({"ALL_PROXY": "http://all.example:8118"}, "all.example"),
            (
                {
                    "HTTPS_PROXY": "http://https.example:8118",
                    "HTTP_PROXY": "http://http.example:8118",
                    "ALL_PROXY": "http://all.example:8118",
                },
                "https.example",
            ),
            (
                {
                    "HTTP_PROXY": "http://http.example:8118",
                    "ALL_PROXY": "http://all.example:8118",
                },
                "http.example",
            ),
        ]
        for env, host in cases:
            with self.subTest(env=env):
                request, _ = self._bootstrap(env)
                self.assertEqual(request["Proxy"], f"http://{host}:8118/")
                self.assertFalse(request["HasProxyCredential"])

    def test_proxy_credentials(self) -> None:
        cases = [
            ("user:password", "user", "password"),
            (
                "user%40example:p%40ss%3Aword%2F%25",
                "user@example",
                "p@ss:word/%",
            ),
            ("user%3Aname:pass:word", "user:name", "pass:word"),
            ("user+name:pass+word", "user+name", "pass+word"),
            ("user:", "user", ""),
            ("user", "user", ""),
        ]
        for user_info, username, password in cases:
            with self.subTest(user_info=user_info):
                request, output = self._bootstrap(
                    {"HTTP_PROXY": f"http://{user_info}@proxy.example:8118"}
                )
                self.assertEqual(request["Proxy"], "http://proxy.example:8118/")
                self.assertTrue(request["HasProxyCredential"])
                self.assertEqual(request["Username"], username)
                self.assertEqual(request["Password"], password)
                self.assertNotIn(user_info + "@", output)
                self.assertIn(
                    "Using Proxy http://proxy.example:8118...", output
                )

    def test_existing_no_proxy_bypasses_credentials(self) -> None:
        request, _ = self._bootstrap(
            {
                "HTTP_PROXY": "http://user:password@proxy.example:8118",
                "NO_PROXY": "packages.example.com",
            }
        )
        self.assertFalse(request["HasProxy"])
        self.assertFalse(request["HasProxyCredential"])

    def test_constrained_language(self) -> None:
        for no_proxy in ("", "packages.example.com"):
            with self.subTest(no_proxy=no_proxy):
                request, _ = self._bootstrap(
                    {
                        "HTTP_PROXY": "http://user%40example:p%3Ass@proxy.example:8118",
                        "NO_PROXY": no_proxy,
                    },
                    constrained=True,
                )
                self.assertEqual(request["HasProxy"], not bool(no_proxy))
                if not no_proxy:
                    self.assertEqual(request["Username"], "user@example")
                    self.assertEqual(request["Password"], "p:ss")


if __name__ == "__main__":
    unittest.main()
