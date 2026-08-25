/**
 * Node.js 中转服务看门狗
 * 作用：proxy-server.js 崩溃或退出时自动重启，保证维修解绑模块随时可用。
 * 用法：node watchdog.js
 */
const { spawn } = require('child_process');
const path = require('path');
const net = require('net');

const proxyJs = path.join(__dirname, 'proxy-server.js');
// 优先使用「与看门狗同一个 node 可执行文件」(process.execPath) 来拉起中转，
// 避免依赖系统 PATH 中的 'node' —— 在 SYSTEM / 服务账户 / 开机自启场景下 PATH 往往不含 node 目录，
// 会导致 spawn('node') 找不到命令、中转起不来。可用环境变量 UNBIND_NODE_EXE 强制指定。
const NODE_EXE = process.env.UNBIND_NODE_EXE || process.execPath;
const PROXY_PORT = 5001;
let restartDelay = 3000;
const maxDelay = 30000;

// 端口占用检查：已被别的实例监听则不要重复拉起（避免 EADDRINUSE 死循环）
function isPortInUse(cb) {
  const sock = new net.Socket();
  sock.setTimeout(1500);
  sock.on('connect', () => { sock.destroy(); cb(true); });
  sock.on('error', () => { cb(false); });
  sock.on('timeout', () => { sock.destroy(); cb(false); });
  sock.connect(PROXY_PORT, '127.0.0.1');
}

function startProxy() {
  isPortInUse((inUse) => {
    if (inUse) {
      console.log(`[watchdog] 端口 ${PROXY_PORT} 已被占用，10s 后重试（可能有其它实例在运行）`);
      setTimeout(startProxy, 10000);
      return;
    }
    console.log(`[watchdog] 启动中转服务: ${proxyJs} (node=${NODE_EXE})`);
    const child = spawn(NODE_EXE, [proxyJs], {
      cwd: __dirname,
      stdio: 'inherit'
    });

    child.on('exit', (code) => {
      console.log(`[watchdog] 中转服务退出，code=${code}，${restartDelay}ms 后重启`);
      setTimeout(() => {
        restartDelay = Math.min(restartDelay * 2, maxDelay);
        startProxy();
      }, restartDelay);
    });

    child.on('error', (err) => {
      console.error('[watchdog] 启动失败', err.message);
    });
  });
}

startProxy();
