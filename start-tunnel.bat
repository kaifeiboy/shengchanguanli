@echo off
REM ============================================================
REM  External tunnel for phone on 4G/5G  (Cloudflare Tunnel)
REM  Why cloudflared:
REM    - localhost.run: port 22 needs SSH key, port 2200 blocked
REM    - serveo.net: free subdomains are IP-locked
REM    - localtunnel: has "Continue" intermediate page that
REM      redirects to raw IP on mobile browsers (broken)
REM    - cloudflared: ZERO config, NO intermediate page,
REM      stable https://xxxx.trycloudflare.com URL, works on 4G
REM  Requires: cloudflared.exe in the same directory as this bat
REM  Download: github.com/cloudflare/cloudflared/releases
REM  After connecting it prints a https://*.trycloudflare.com URL.
REM  Open that URL directly on phone - no extra steps needed.
REM  Keep this window open while using external access.
REM ============================================================
REM NOTE (2026-07-09): Phone 4G gets Error 1033 even though local
REM   curl works. Possible causes: ISP UDP/QUIC interference,
REM   Cloudflare edge routing issue for CN mobile networks.
REM   ISSUE REMAINS OPEN - use internal network for now.
REM ============================================================
"%~dp0cloudflared.exe" tunnel --url http://127.0.0.1:5000 --no-autoupdate
pause
