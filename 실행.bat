@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 실시간번역기

rem 1. Python 찾기 (없으면 자동 설치)
set "PY="
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe") do (
  if not defined PY if exist %%P set "PY=%%~P"
)
if not defined PY (
  where py >nul 2>nul && for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
)
if not defined PY (
  echo.
  echo ▶ Python 이 없어 자동 설치합니다 (1~3분, 인터넷 필요)
  curl -L -# -o "%TEMP%\python-setup.exe" https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
  if errorlevel 1 goto :fail_dl
  "%TEMP%\python-setup.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  if not exist "%PY%" goto :fail_py
  echo   Python 설치 완료
)

rem 2. 패키지 설치 (최초 1회)
if not exist .venv (
  echo.
  echo ▶ 필요한 패키지 설치 (최초 1회)
  "%PY%" -m venv .venv || goto :fail_venv
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\pip install -q numpy openai anthropic pyaudiowpatch || goto :fail_pip
)

rem 3. 실행 (첫 실행이면 API 키 입력창이 뜨)
echo.
echo ▶ 실시간번역기 시작. 자막은 이 폴더에 자동 저장됩니다.
.venv\Scripts\python realtime_translator.py
if errorlevel 1 pause
exit /b 0

:fail_dl
echo ✖ Python 다운로드 실패. 인터넷 연결을 확인하세요.
pause & exit /b 1
:fail_py
echo ✖ Python 설치 실패. https://www.python.org/downloads/ 에서 직접 설치 후 다시 실행하세요.
pause & exit /b 1
:fail_venv
echo ✖ 가상환경 생성 실패.
pause & exit /b 1
:fail_pip
echo ✖ 패키지 설치 실패. 인터넷 연결을 확인하세요.
pause & exit /b 1
