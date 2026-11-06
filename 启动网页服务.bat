@echo off
chcp 936 >nul
setlocal
title ScreenOff-Web - 网页关屏服务（请保持本窗口开启）
cd /d "%~dp0"
rem 清除外部环境可能继承的编码设置，保证控制台输出显示正常
set "PYTHONUTF8="
set "PYTHONIOENCODING="

where python >nul 2>&1
if errorlevel 1 goto trypy
python -c "import sys" >nul 2>&1
if errorlevel 1 goto trypy
set "PY=python"
goto run

:trypy
where py >nul 2>&1
if errorlevel 1 goto nopy
py -3 -c "import sys" >nul 2>&1
if errorlevel 1 goto nopy
set "PY=py -3"
goto run

:nopy
echo.
echo [错误] 没有找到可用的 Python 3。
echo 请安装 Python 3.8 或更高版本，安装时勾选 Add python.exe to PATH。
echo 下载地址: https://www.python.org/downloads/
echo.
pause
exit /b 1

:run
echo 正在启动 ScreenOff-Web 服务...
echo 启动后会打印局域网地址，手机连同一 Wi-Fi 打开即可。
echo 关闭本窗口即可停止服务。
echo.
%PY% "%~dp0server.py" --open
echo.
echo 服务已退出，错误码 %errorlevel%。
pause
exit /b 0
