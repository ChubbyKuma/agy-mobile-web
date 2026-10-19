@echo off
chcp 65001 >nul
echo =========================================================
echo   正在启动 Antigravity 手机端 Web 服务与 Cloudflare 公网隧道...
echo   访问 PIN 码: 453162
echo   请查看下方生成的 https://xxxx.trycloudflare.com 链接
echo   在手机浏览器打开: https://xxxx.trycloudflare.com/?pin=453162
echo =========================================================
start "Antigravity-Local-Server" /min "C:\Users\Chanson Huang\AppData\Local\Programs\Python\Python311\python.exe" "%~dp0server.py"
timeout /t 2 >nul
"%~dp0cloudflared.exe" tunnel --url http://127.0.0.1:8765 --no-autoupdate
pause
