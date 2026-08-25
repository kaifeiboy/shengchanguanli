@echo off
REM ============================================================
REM  Benji Tuzhi Pingtai - start server (double-click to run)
REM  Starts the ASP.NET Core platform on port 5000.
REM  Phone on same WiFi: open http://<LAN-IP>:5000
REM  Find LAN IP: after start, open http://127.0.0.1:5000/api/network/info
REM  Phone on 4G/5G: run start-tunnel.bat in another window
REM ============================================================
setlocal
REM Clear any inherited SERVER__PORT so the port stays 5000
set SERVER__PORT=
cd /d "%~dp0src\Platform"
dotnet run
endlocal
