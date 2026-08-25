/**
 * 生产管理平台保活看门狗（Node 版，供 Windows 计划任务 SCG_Platform 以 SYSTEM 直接运行）
 * 与中转看门狗同构：开机构建一次 -> 运行 dotnet run --no-build -> 退出自动重启。
 * 启动即崩（<15s）视为需重建，下次循环先 dotnet build；正常崩溃走快速 --no-build 重启。
 * 端口已在监听则空转，避免重复实例抢端口。
 */
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const net = require('net');

const ROOT = 'E:\\workaaa\\shengchanguanli';
const projDir = path.join(ROOT, 'src', 'Platform');
const DOTNET = 'C:\\Program Files\\dotnet\\dotnet.exe';
const PLATFORM_PORT = 5000;
const logDir = path.join(ROOT, 'data', 'boot');
const logFile = path.join(logDir, 'platform_watchdog.log');

if (!fs.existsSync(logDir)) fs.mkdirSync(logDir, { recursive: true });

function log(m) {
  const line = `${new Date().toISOString()} ${m}`;
  try { fs.appendFileSync(logFile, line + '\n'); } catch (e) {}
  console.log(line);
}

function portInUse(cb) {
  const s = net.connect({ host: '127.0.0.1', port: PLATFORM_PORT });
  s.setTimeout(1500);
  s.on('connect', () => { s.destroy(); cb(true); });
  s.on('error', () => cb(false));
  s.on('timeout', () => { s.destroy(); cb(false); });
}

function runDotnet(args, onExit) {
  log(`spawn dotnet ${args.join(' ')}`);
  const out = fs.openSync(path.join(logDir, 'platform.log'), 'a');
  const err = fs.openSync(path.join(logDir, 'platform_err.log'), 'a');
  const child = spawn(DOTNET, args, { cwd: projDir, stdio: ['ignore', out, err] });
  child.on('exit', (code) => onExit(code == null ? -1 : code));
  child.on('error', (err) => { log('spawn error: ' + err.message); onExit(-1); });
}

function build(cb) {
  runDotnet(['build', '-v', 'quiet', '--nologo'], (code) => cb(code === 0));
}

let builtOk = false;

function start() {
  portInUse((inUse) => {
    if (inUse) {
      log(`port ${PLATFORM_PORT} in use, idle 15s`);
      setTimeout(start, 15000);
      return;
    }
    log('port not listening, ensuring build');
    const afterBuild = (ok) => {
      builtOk = ok;
      if (!ok) { log('build failed, retry 30s'); setTimeout(start, 30000); return; }
      log('dotnet run --no-build');
      const t0 = Date.now();
      runDotnet(['run', '--no-build'], (code) => {
        const dur = (Date.now() - t0) / 1000;
        log(`dotnet exited code=${code} dur=${dur.toFixed(0)}s`);
        if (dur < 15) { builtOk = false; log('startup crash (<15s), will rebuild next'); }
        setTimeout(start, 10000);
      });
    };
    if (builtOk) afterBuild(true);
    else build(afterBuild);
  });
}

log('=== platform-watchdog start ===');
// 初始构建一次，再进入保活循环
build((ok) => { builtOk = ok; log(`initial build ok=${ok}`); start(); });
