@echo off
chcp 936 >nul
setlocal
title ScreenOff-Web - 放行防火墙端口 8800

fltmc >nul 2>&1
if errorlevel 1 goto elevate

echo 正在添加防火墙规则 ScreenOff-Web  TCP 8800 ...
netsh advfirewall firewall delete rule name="ScreenOff-Web" >nul 2>&1
netsh advfirewall firewall add rule name="ScreenOff-Web" dir=in action=allow protocol=TCP localport=8800
if errorlevel 1 goto failed
echo.
echo [完成] 已放行 TCP 8800 入站，局域网设备可以访问本机网页。
echo.
pause
exit /b 0

:failed
echo.
echo [错误] 添加防火墙规则失败，请确认以管理员身份运行本文件。
pause
exit /b 1

:elevate
echo 正在申请管理员权限，请在系统弹窗中点击「是」...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
echo.
echo 请在提权后的新窗口里查看结果。
pause
exit /b 0
