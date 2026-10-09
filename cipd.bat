@echo off
:: Copyright 2016 The Chromium Authors
:: Use of this source code is governed by a BSD-style license that can be
:: found in the LICENSE file.

setlocal

set CIPD_BACKEND=https://chrome-infra-packages.appspot.com
set VERSION_FILE=%~dp0cipd_client_version
set CIPD_BINARY=%~dp0.cipd_client.exe
set CIPD_PLATFORM=windows-amd64
set PLATFORM_OVERRIDE_FILE=%~dp0.cipd_client_platform
set CIPD_IMPL=%~dp0.cipd_impl.ps1

:: Uncomment to recognize arm64 by default.
:: if %PROCESSOR_ARCHITECTURE%==ARM64 (
::   set CIPD_PLATFORM=windows-arm64
:: )

:: A value in .cipd_client_platform overrides the "guessed" platform.
set CIPD_PLATFORM_EXTRA=
if exist "%PLATFORM_OVERRIDE_FILE%" (
  for /F "usebackq tokens=1*" %%l in ("%PLATFORM_OVERRIDE_FILE%") do (
    set CIPD_PLATFORM=%%l
    set CIPD_PLATFORM_EXTRA=%%m
  )
  if defined CIPD_PLATFORM_EXTRA (
    echo Invalid platform override in "%PLATFORM_OVERRIDE_FILE%". 1>&2
    exit /b 1
  )
  set CIPD_PLATFORM | findstr /r /v "^CIPD_PLATFORM=[0-9a-z\-][0-9a-z\-]*$" 1>nul 2>nul
  if not errorlevel 1 (
    echo Invalid platform override in "%PLATFORM_OVERRIDE_FILE%". 1>&2
    exit /b 1
  )
)

:: Nuke the existing client if its platform doesn't match what we want now. We
:: crudely search for a CIPD client package name in the .cipd_version JSON file.
:: It has only "instance_id" as the other field (looking like a base64 string),
:: so mismatches are very unlikely.
set INSTALLED_VERSION_FILE=%~dp0.versions\.cipd_client.exe.cipd_version
findstr /m "infra/tools/cipd/%CIPD_PLATFORM%" "%INSTALLED_VERSION_FILE%" 1>nul 2>nul
if %ERRORLEVEL% neq 0 (
  if exist "%INSTALLED_VERSION_FILE%" (
    echo Detected CIPD client platform change to %CIPD_PLATFORM%. 1>&2
    echo Deleting the existing client to trigger the bootstrap... 1>&2
    del "%CIPD_BINARY%"
    del "%INSTALLED_VERSION_FILE%"
  )
)

if not exist "%CIPD_BINARY%" (
  call :CLEAN_BOOTSTRAP
  goto :EXEC_CIPD
)

call :SELF_UPDATE >nul 2>&1
if %ERRORLEVEL% neq 0 (
  echo CIPD client self-update failed, trying to bootstrap it from scratch... 1>&2
  call :CLEAN_BOOTSTRAP
)

:EXEC_CIPD

if %ERRORLEVEL% neq 0 (
  echo Failed to bootstrap or update CIPD client 1>&2
)
if %ERRORLEVEL% equ 0 (
  "%CIPD_BINARY%" %*
)

endlocal & (
  set EXPORT_ERRORLEVEL=%ERRORLEVEL%
)
exit /b %EXPORT_ERRORLEVEL%


:: Functions below.
::
:: See http://steve-jansen.github.io/guides/windows-batch-scripting/part-7-functions.html
:: if you are unfamiliar with this madness.


:CLEAN_BOOTSTRAP
:: To allow this powershell script to run if it was a byproduct of downloading
:: and unzipping the depot_tools.zip distribution, we clear the Zone.Identifier
:: alternate data stream. This is equivalent to clicking the "Unblock" button
:: in the file's properties dialog.
echo.>"%CIPD_IMPL%:Zone.Identifier"
:: Invoke via -Command with '&' rather than -File so .cipd_impl.ps1 is not
:: dot-sourced (which fails under ConstrainedLanguage mode, crbug.com/1442051).
:: Read arguments from $env:* so special characters in paths or platform strings
:: are never interpolated into PowerShell code (b/557829167).
powershell -NoProfile -ExecutionPolicy RemoteSigned ^
    -Command "& $env:CIPD_IMPL" ^
    "-CipdBinary $env:CIPD_BINARY" ^
    "-Platform $env:CIPD_PLATFORM" ^
    "-BackendURL $env:CIPD_BACKEND" ^
    "-VersionFile $env:VERSION_FILE" ^
  <nul
if %ERRORLEVEL% equ 0 (
  :: Need to call SELF_UPDATE to setup .cipd_version file.
  call :SELF_UPDATE
)
exit /B %ERRORLEVEL%


:SELF_UPDATE
"%CIPD_BINARY%" selfupdate ^
    -version-file "%VERSION_FILE%" ^
    -service-url "%CIPD_BACKEND%"
exit /B %ERRORLEVEL%
