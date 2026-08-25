@echo off
chcp 65001 >nul
REM ============================================================
REM  外网穿透 - natapp 免费固定域名隧道（重启地址不变）
REM  ----
REM  前置（一次性，详见 docs/natapp-deploy.md）：
REM    1. natapp.cn 注册 + 实名认证
REM    2. 购买「免费 Web 隧道」，本地端口填 5000，本地地址 127.0.0.1
REM    3. 复制该隧道 authtoken，写入 data\natapp_token.txt（单行，无空格）
REM    4. 下载 natapp.exe 放到 natapp\ 子目录（与本 bat 同级）
REM    5. 把隧道分配的固定域名（如 xxx.natapp1.cc）填入
REM       src\Platform\appsettings.json 的 External:BaseUrl
REM  ----
REM  本脚本读 data\natapp_token.txt 获取 authtoken 并启动 natapp。
REM  保持窗口开启期间外网可达；关窗即断。
REM  建议开机自启：用计划任务在登录时运行本 bat（隐藏窗口）。
REM ============================================================
setlocal
set "ROOT=%~dp0"
set "TOKEN="
if exist "%ROOT%data\natapp_token.txt" set /p TOKEN=<"%ROOT%data\natapp_token.txt"

if "%TOKEN%"=="" (
  echo [!] 未配置 natapp authtoken。
  echo     请按 docs\natapp-deploy.md 获取 authtoken 并写入 data\natapp_token.txt
  echo     （单行，无引号无空格）
  pause
  exit /b 1
)

if not exist "%ROOT%natapp\natapp.exe" (
  echo [!] 未找到 natapp\natapp.exe
  echo     请到 https://natapp.cn 客户端下载 Windows 64 位版，
  echo     解压后将 natapp.exe 放到 %ROOT%natapp\ 目录
  pause
  exit /b 1
)

echo [*] 启动 natapp 隧道（authtoken 已配置，本地端口 5000 在 natapp 后台指定）...
echo [*] 启动后控制台会输出 Tunnel Status + 公网域名，保持本窗口开启以维持外网可达。
"%ROOT%natapp\natapp.exe" -authtoken=%TOKEN% -log=stdout -loglevel=INFO
pause
