#!/usr/bin/env vpython3
# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import os
import sys
from typing import Any
import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
import run_in_virtual_path  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Virtual path is not supported on Windows"
)

VIRTUAL_ROOT = "/tmp/depot_tools_virtual_build_path"
VIRTUAL_PATH = f"{VIRTUAL_ROOT}/src"


@pytest.fixture
def runner(mocker: Any) -> Any:
    mocker.patch("sys.platform", new="linux")
    mocker.patch(
        "gclient_paths.GetPrimarySolutionPath", return_value="/workspace/src"
    )
    mocker.patch("gclient_paths.FindGclientRoot", return_value="/workspace")
    mocker.patch("os.path.isfile", return_value=False)
    mocker.patch("os.getcwd", return_value="/workspace/src")
    mocker.patch("os.getuid", return_value=1001)
    mocker.patch("os.makedirs")
    return mocker.Mock(return_value=0)


def test_main_non_linux(
    mocker: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch("sys.platform", new="win32")

    ret = run_in_virtual_path.main(["run_in_virtual_path.py", "siso", "ninja"])
    assert ret == 1
    assert "virtual path is only supported on Linux" in capsys.readouterr().err


def test_main_no_args(mocker: Any, capsys: pytest.CaptureFixture[str]) -> None:
    mocker.patch("sys.platform", new="linux")

    ret = run_in_virtual_path.main(["run_in_virtual_path.py"])
    assert ret == 1
    assert (
        "Usage: run_in_virtual_path <command> [args...]"
        in capsys.readouterr().err
    )


def test_main_no_primary_solution_path(
    mocker: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch("sys.platform", new="linux")
    mocker.patch("gclient_paths.GetPrimarySolutionPath", return_value=None)

    ret = run_in_virtual_path.main(["run_in_virtual_path.py", "siso", "ninja"])
    assert ret == 1
    assert (
        "Could not find gclient primary solution path"
        in capsys.readouterr().err
    )


def test_main_runs_in_virtual_path(
    runner: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    ret = run_in_virtual_path.main(
        ["run_in_virtual_path.py", "autoninja", "-C", "out/Release", "chrome"],
        env={},
        runner=runner,
    )
    assert ret == 0

    expected_bash_cmd = (
        f"mount -t tmpfs tmpfs {VIRTUAL_ROOT} && "
        f"mkdir {VIRTUAL_PATH} && "
        f"mount --rbind /workspace/src {VIRTUAL_PATH} && "
        f"cd {VIRTUAL_PATH} && "
        "unshare --map-user=1001 autoninja -C out/Release chrome"
    )
    expected_unshare_cmd = [
        "luci-auth",
        "context",
        "--",
        "unshare",
        "--mount",
        "--map-root-user",
        "bash",
        "-c",
        expected_bash_cmd,
    ]

    runner.assert_called_once_with(
        expected_unshare_cmd,
        env={"SISO_CREDENTIAL_HELPER": "google-application-default"},
    )
    assert (
        f"Virtualizing paths from /workspace/src to {VIRTUAL_PATH}"
        in capsys.readouterr().err
    )


def test_main_custom_virtual_path(mocker: Any, runner: Any) -> None:
    mocker.patch("gclient_paths.FindGclientRoot", return_value=None)

    ret = run_in_virtual_path.main(
        ["run_in_virtual_path.py", "siso", "ninja"],
        env={"DEPOT_TOOLS_VIRTUAL_BUILD_PATH": "/custom/virtual_path/"},
        runner=runner,
    )
    assert ret == 0

    cmd = runner.call_args[0][0]
    expected_bash_cmd = (
        "mount -t tmpfs tmpfs /custom/virtual_path && "
        "mkdir /custom/virtual_path/src && "
        "mount --rbind /workspace/src /custom/virtual_path/src && "
        "cd /custom/virtual_path/src && "
        "unshare --map-user=1001 siso ninja"
    )
    assert cmd[-1] == expected_bash_cmd


@pytest.mark.parametrize(
    "cwd, expected_cd",
    [
        pytest.param(
            "/workspace/src/out/Release",
            f"{VIRTUAL_PATH}/out/Release",
            id="preserves_relative_path",
        ),
        pytest.param(
            "/other/dir",
            VIRTUAL_PATH,
            id="outside_solution_path",
        ),
        pytest.param(
            "/workspace",
            VIRTUAL_PATH,
            id="parent_of_solution_path",
        ),
    ],
)
def test_main_working_directory(
    mocker: Any, runner: Any, cwd: str, expected_cd: str
) -> None:
    mocker.patch("os.getcwd", return_value=cwd)

    ret = run_in_virtual_path.main(
        ["run_in_virtual_path.py", "autoninja", "chrome"],
        env={},
        runner=runner,
    )
    assert ret == 0

    cmd = runner.call_args[0][0]
    expected_bash_cmd = (
        f"mount -t tmpfs tmpfs {VIRTUAL_ROOT} && "
        f"mkdir {VIRTUAL_PATH} && "
        f"mount --rbind /workspace/src {VIRTUAL_PATH} && "
        f"cd {expected_cd} && "
        "unshare --map-user=1001 autoninja chrome"
    )
    assert cmd[-1] == expected_bash_cmd


def test_main_symlink_solution_path(mocker: Any, runner: Any) -> None:
    mocker.patch(
        "gclient_paths.GetPrimarySolutionPath",
        return_value="/symlink/workspace/src",
    )
    mocker.patch(
        "os.path.realpath",
        side_effect=lambda p: (
            "/real/workspace/src/out/Release"
            if p == "."
            else "/real/workspace/src"
        ),
    )

    ret = run_in_virtual_path.main(
        ["run_in_virtual_path.py", "autoninja", "chrome"],
        env={},
        runner=runner,
    )
    assert ret == 0

    cmd = runner.call_args[0][0]
    expected_bash_cmd = (
        f"mount -t tmpfs tmpfs {VIRTUAL_ROOT} && "
        f"mkdir {VIRTUAL_PATH} && "
        f"mount --rbind /symlink/workspace/src {VIRTUAL_PATH} && "
        f"cd {VIRTUAL_PATH}/out/Release && "
        "unshare --map-user=1001 autoninja chrome"
    )
    assert cmd[-1] == expected_bash_cmd


def test_main_mounts_gclient_entries(mocker: Any, runner: Any) -> None:
    mocker.patch(
        "os.path.isfile",
        side_effect=lambda p: p == "/workspace/.gclient_entries",
    )

    ret = run_in_virtual_path.main(
        ["run_in_virtual_path.py", "autoninja", "-C", "out/Release", "chrome"],
        env={},
        runner=runner,
    )
    assert ret == 0

    cmd = runner.call_args[0][0]
    expected_bash_cmd = (
        f"mount -t tmpfs tmpfs {VIRTUAL_ROOT} && "
        f"mkdir {VIRTUAL_PATH} && "
        f"mount --rbind /workspace/src {VIRTUAL_PATH} && "
        f"touch {VIRTUAL_ROOT}/.gclient_entries && "
        f"mount --bind /workspace/.gclient_entries {VIRTUAL_ROOT}/.gclient_entries && "
        f"cd {VIRTUAL_PATH} && "
        "unshare --map-user=1001 autoninja -C out/Release chrome"
    )
    assert cmd[-1] == expected_bash_cmd


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
