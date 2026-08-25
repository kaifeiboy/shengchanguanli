/**
 * 本地 Node.js 中转服务（维修解绑模块）
 *
 * 作用：把手机 H5 / .NET 后端发来的简单请求转发到目标内网系统 https://10.10.10.68:7443/，
 *       并注入目标系统已登录的 JWT（Authorization: Bearer），使手机端无需二次登录。
 *
 * 鉴权：目标系统用 JWT。PC 网页通过 http 拦截器自动加 Authorization: Bearer <token>，
 *       本服务用 config.json 的 token 字段做同样的事。
 *
 * 监听端口默认 5001（可在 config.json 修改），避免与 .NET 后端 5000 冲突。
 */

const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');

const CONFIG_PATH = process.env.UNBIND_PROXY_CONFIG || path.join(__dirname, 'config.json');

function loadConfig() {
  const raw = fs.readFileSync(CONFIG_PATH, 'utf8');
  return JSON.parse(raw);
}

let cfg = loadConfig();
cfg.__file = CONFIG_PATH;

// ---- 连接池（keep-alive）：避免每次查询都重建 TCP + TLS 握手 ----
// 默认 https 全局 Agent keepAlive=false，每次请求都重新握手，内网 https 目标尤为明显。
// 这里用共享 keep-alive Agent 复用连接，显著降低单次数据返回响应时间。
const httpsAgent = new https.Agent({ keepAlive: true, maxSockets: 64, maxFreeSockets: 16, timeout: 60000 });
const httpAgent  = new http.Agent({  keepAlive: true, maxSockets: 64, maxFreeSockets: 16 });

console.log(`[unbind-proxy] 配置加载自 ${CONFIG_PATH}`);
console.log(`[unbind-proxy] 目标系统：${cfg.targetBaseUrl}`);
console.log(`[unbind-proxy] 监听端口：${cfg.listenPort}`);
console.log(`[unbind-proxy] 演示模式(mock)：${cfg.mock ? '开启（返回模拟数据，不请求真实系统）' : '关闭（转发到真实目标系统）'}`);

// ---- 自动续期：凭据加密存储 + 登录接口换发 JWT ----
const authMod = require('./auth');
let auth = null;
try {
  auth = authMod.createAuthManager(cfg);
  console.log('[unbind-proxy] 自动续期模块已加载');
} catch (e) {
  console.error('[unbind-proxy] 自动续期模块加载失败：', e.message);
}

// 把对象序列化成 query string（仿 axios+Qs：数组用 repeat，跳过 null/空字符串）
function buildQuery(obj) {
  const parts = [];
  for (const [k, v] of Object.entries(obj || {})) {
    if (v === null || v === undefined) continue;
    if (typeof v === 'string' && v.trim() === '') continue;
    if (Array.isArray(v)) {
      v.forEach(item => {
        if (item === null || item === undefined) return;
        if (typeof item === 'string' && item.trim() === '') return;
        parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(item))}`);
      });
    } else {
      parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
    }
  }
  return parts.join('&');
}

function readJsonBody(req, cb) {
  const chunks = [];
  req.on('data', c => chunks.push(c));
  req.on('end', () => {
    let body = {};
    const raw = Buffer.concat(chunks).toString('utf8');
    if (raw) {
      try { body = JSON.parse(raw); } catch (e) { body = {}; }
    }
    cb(body);
  });
}

function sendJson(res, status, obj) {
  const headers = { 'Content-Type': 'application/json; charset=utf-8' };
  if (cfg.cors) {
    headers['Access-Control-Allow-Origin'] = '*';
    headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS';
    headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization';
  }
  res.writeHead(status, headers);
  res.end(JSON.stringify(obj));
}

// 仅允许本机（127.0.0.1 / ::1）访问敏感管理接口
function isLocal(req) {
  const a = (req.socket && req.socket.remoteAddress) || '';
  return a === '127.0.0.1' || a === '::1' || a === '::ffff:127.0.0.1';
}

function buildTargetHeaders() {
  const h = {
    'Content-Type': 'application/json; charset=utf-8',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9'
  };
  // 优先用运行时 token（自动续期更新的），回退到 config 静态 token
  const tok = (auth && auth.getRuntimeToken && auth.getRuntimeToken()) || (cfg.token || '');
  if (tok && tok.trim()) {
    h['Authorization'] = 'Bearer ' + tok.trim();
  }
  if (cfg.cookie && cfg.cookie.trim()) {
    h['Cookie'] = cfg.cookie.trim();
  }
  return h;
}

// 把请求转发到目标系统：method + path + 可选 query；bodyBuf 为空则不写 body
// attempt 用于 401 自动重登后重发一次（避免死循环）
function forwardToTarget(res, opts, attempt = 0) {
  const { method, targetPath, query, bodyBuf } = opts;
  const base = (cfg.targetBaseUrl || '').replace(/\/+$/, '');
  let urlStr = base + targetPath;
  if (query) urlStr += (urlStr.includes('?') ? '&' : '?') + query;

  const targetUrl = new URL(urlStr);
  const client = targetUrl.protocol === 'https:' ? https : http;
  const isHttps = targetUrl.protocol === 'https:';

  const options = {
    method: method,
    headers: buildTargetHeaders(),
    rejectUnauthorized: cfg.rejectUnauthorized !== false,
    agent: isHttps ? httpsAgent : httpAgent,
    timeout: 30000 // 目标系统 30s 无响应则主动断开，快速失败（502）
  };

  const preq = client.request(targetUrl, options, (pres) => {
    // 401：尝试刷新 token 后重发一次
    if (pres.statusCode === 401 && attempt === 0 && auth) {
      pres.resume(); // 丢弃本次响应体
      auth.ensureFreshToken().then(() => {
        forwardToTarget(res, opts, 1);
      }).catch((e) => {
        console.error('[unbind-proxy] 401 自动重登失败：', e.message);
        sendJson(res, 401, { code: 401, msg: '鉴权过期且自动重新登录失败：' + e.message });
      });
      return;
    }
    const chunks = [];
    pres.on('data', c => chunks.push(c));
    pres.on('end', () => {
      const buf = Buffer.concat(chunks);
      const ctype = (pres.headers['content-type'] || '').toLowerCase();
      let out = buf;
      if (ctype.includes('application/json')) {
        try {
          const json = JSON.parse(buf.toString('utf8'));
          out = Buffer.from(JSON.stringify(json), 'utf8');
        } catch (e) { /* 原样返回 */ }
      }
      const headers = { 'Content-Type': pres.headers['content-type'] || 'application/json; charset=utf-8' };
      if (cfg.cors) {
        headers['Access-Control-Allow-Origin'] = '*';
        headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS';
        headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization';
      }
      res.writeHead(pres.statusCode, headers);
      res.end(out);
    });
  });

  preq.on('timeout', () => {
    // 触发 timeout 事件：销毁请求，转由 error 事件返回 502
    preq.destroy(new Error('目标系统响应超时(>30s)'));
  });
  preq.on('error', (err) => {
    console.error('[unbind-proxy] 转发失败', err.message);
    sendJson(res, 502, { code: -1, msg: `连接目标系统失败：${err.message}` });
  });

  if (bodyBuf && bodyBuf.length > 0) preq.write(bodyBuf);
  preq.end();
}

function mockHandler(req, res, key) {
  readJsonBody(req, (body) => {
    const s = (cfg.mockData && cfg.mockData.sample) || {};
    let out;
    if (key === 'search') {
      const rec = {
        上盖码: (body.coverCode && String(body.coverCode)) || s['上盖码'] || '-',
        大板码: (body.bigCode && String(body.bigCode)) || s['大板码'] || '-',
        小板码: (body.smallCode && String(body.smallCode)) || s['小板码'] || '-',
        外箱码: (body.boxCode && String(body.boxCode)) || s['外箱码'] || '-',
        创建时间: s['创建时间'] || ''
      };
      out = { code: 0, msg: '演示数据', data: [rec] };
    } else if (key === 'outer') {
      out = { code: 0, msg: `外箱解绑成功（演示模式），外箱码：${body.boxCode || '-'}` };
    } else {
      out = { code: 0, msg: '维修解绑成功（演示模式）' };
    }
    sendJson(res, 200, out);
  });
}

const server = http.createServer((req, res) => {
  if (cfg.cors && req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization'
    });
    res.end();
    return;
  }

  const url = req.url || '/';
  console.log(`[unbind-proxy] ${req.method} ${url}`);

  // 重新加载配置
  if (url === '/_reload' && req.method === 'GET') {
    try {
      cfg = loadConfig();
      cfg.__file = CONFIG_PATH;
      auth = authMod.createAuthManager(cfg);
      auth.init().catch((e) => console.error('[auth] reload init:', e.message));
      sendJson(res, 200, { code: 0, msg: '配置已重新加载' });
    } catch (e) {
      sendJson(res, 500, { code: -1, msg: e.message });
    }
    return;
  }

  // 健康检查
  if (url === '/_health' && req.method === 'GET') {
    sendJson(res, 200, { code: 0, msg: 'ok', target: cfg.targetBaseUrl });
    return;
  }

  // ---- 自动续期管理接口（仅本机可访问）----
  if (url.startsWith('/_auth/') && !isLocal(req)) {
    sendJson(res, 403, { code: -1, msg: '该接口仅允许本机访问' });
    return;
  }
  if (url === '/_auth/status' && req.method === 'GET') {
    if (!auth) { sendJson(res, 200, { available: false }); return; }
    sendJson(res, 200, Object.assign({ available: true }, auth.status()));
    return;
  }
  if (url === '/_auth/refresh' && req.method === 'POST') {
    if (!auth) { sendJson(res, 503, { code: -1, msg: '自动续期未启用' }); return; }
    auth.ensureFreshToken().then(() => {
      sendJson(res, 200, { code: 0, msg: '已刷新', exp: auth.status().tokenExpISO });
    }).catch((e) => sendJson(res, 502, { code: -1, msg: e.message }));
    return;
  }
  if (url === '/_auth/setup' && req.method === 'POST') {
    if (!auth) { sendJson(res, 503, { code: -1, msg: '自动续期未启用' }); return; }
    readJsonBody(req, (body) => {
      const u = ((body.username || '') + '').trim();
      const p = (body.password || '') + '';
      if (!u || !p) { sendJson(res, 400, { code: -1, msg: '账号和密码不能为空' }); return; }
      auth.setupCredentials({ username: u, password: p }).then((r) => {
        sendJson(res, 200, { code: 0, msg: '账号密码已加密保存，自动续期已启用', exp: new Date(r.exp).toISOString() });
      }).catch((e) => sendJson(res, 502, { code: -1, msg: e.message }));
    });
    return;
  }

  const m = url.match(/^\/api\/unbind\/(search|outer|repair)$/);
  if (m) {
    const key = m[1];
    const route = cfg.routes && cfg.routes[key];
    if (!route) {
      sendJson(res, 404, { code: -1, msg: `未配置路由：${key}` });
      return;
    }
    if (cfg.mock) {
      mockHandler(req, res, key);
      return;
    }

    readJsonBody(req, (body) => {
      if (route.method === 'GET') {
        // 搜索：把 JSON body 转成 query string 透传给目标 GET 接口
        const query = buildQuery(body);
        forwardToTarget(res, { method: 'GET', targetPath: route.path, query, bodyBuf: null });
      } else {
        // 解绑：ids[] + route 里固定的 unbindType，拼成 query（与 PC 网页一致）
        const ids = Array.isArray(body.ids) ? body.ids : [];
        const qObj = { ids: ids, unbindType: route.unbindType };
        const query = buildQuery(qObj);
        forwardToTarget(res, { method: 'POST', targetPath: route.path, query, bodyBuf: null });
      }
    });
    return;
  }

  sendJson(res, 404, { code: -1, msg: '不支持的接口' });
});

server.listen(cfg.listenPort, '0.0.0.0', () => {
  console.log(`[unbind-proxy] 服务已启动：http://0.0.0.0:${cfg.listenPort}`);
  if (auth) auth.init().catch((e) => console.error('[auth] 启动自动刷新：', e.message));
});
