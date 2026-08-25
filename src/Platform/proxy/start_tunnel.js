// ⭐ 开机自启 Cloudflare 快速隧道（SCG_Tunnel 计划任务）
// 职责：拉起 cloudflared -> 解析 trycloudflare 最新域名 -> 写 data/tunnel_url.txt
//       （/api/network/info 自动读该文件 -> H5"更多-地址"实时显示最新外网地址）
// 保活：cloudflared 意外退出 3s 后自动重启并重新获取域名。
// 运行：node managed 22.22.2  <本文件>
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = 'E:\\workaaa\\shengchanguanli';
const CLOUDFLARED = path.join(ROOT, 'cloudflared.exe');
const TUNNEL_FILE = path.join(ROOT, 'data', 'tunnel_url.txt');
const TUNNEL_URL = 'http://localhost:5000';

function writeTunnelUrl(url) {
  try {
    const cur = fs.existsSync(TUNNEL_FILE) ? fs.readFileSync(TUNNEL_FILE, 'utf8').trim() : '';
    if (cur !== url) {
      fs.writeFileSync(TUNNEL_FILE, url + '\n');
      console.log(`[tunnel] 地址更新 -> ${url}`);
    } else {
      console.log(`[tunnel] 地址未变 ${url}`);
    }
  } catch (e) {
    console.error('[tunnel] 写 tunnel_url.txt 失败:', e.message);
  }
}

function start() {
  console.log(`[tunnel] 启动 cloudflared -> ${TUNNEL_URL}`);
  let proc;
  try {
    proc = spawn(CLOUDFLARED, ['tunnel', '--url', TUNNEL_URL, '--no-autoupdate'], {
      cwd: ROOT,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
  } catch (e) {
    console.error('[tunnel] spawn 失败:', e.message);
    setTimeout(start, 5000);
    return;
  }

  let urlFound = false;
  const onData = (d) => {
    const s = d.toString();
    const m = s.match(/https:\/\/[a-z0-9-]+\.trycloudflare\.com/);
    if (m && !urlFound) {
      urlFound = true;
      writeTunnelUrl(m[0]);
    }
  };
  proc.stdout.on('data', onData);
  proc.stderr.on('data', onData);

  proc.on('exit', (code) => {
    console.log(`[tunnel] cloudflared 退出 code=${code}，3s 后重启`);
    if (!urlFound) console.log('[tunnel] 未能解析隧道域名');
    setTimeout(start, 3000);
  });
  proc.on('error', (e) => {
    console.error('[tunnel] cloudflared 错误:', e.message);
    setTimeout(start, 5000);
  });
}

start();
