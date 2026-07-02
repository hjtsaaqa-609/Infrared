@echo off
setlocal

if "%~1"=="" (
  echo Usage:
  echo   run_zs8k_windows.bat COM5
  echo.
  echo This reads ZS-8K address 1, 8 channels, once, with wire debug enabled.
  exit /b 1
)

py "%~dp0windows_zs8k_modbus.py" poll --port %1 --address 1 --channels 8 --once --debug-wire
