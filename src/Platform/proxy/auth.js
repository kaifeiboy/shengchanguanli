'use strict';
/**
 * auth.js — 维修解绑中转的「凭据加密存储 + JWT 自动续期」模块（零 npm 依赖）。
 *
 * - 凭据（账号/密码）用 AES-256-GCM 加密后存 secrets.json，密钥本机独立文件 secrets.key（仅本机可读，gitignore）。
 *   等价 DPAPI LocalMachine：保护「明文落盘/磁盘被盗」，本机任意进程（含开机自启的 SYSTEM）可解，无需口令，实现全自动续期。
 * - 登录接口（账号+密码，无验证码）拿回 JWT，缓存为运行时 token；
 *   接近过期（refreshLeadDays 天前）或遇 401 时自动重新登录换新 token，并落盘到 config.json。
 */
const fs = require('fs');
const path = require('path');
const https = require('https');
const http = require('http');
const crypto = require('crypto');
const { URL } = require('url');

const KEY_FILE = path.resolve(__dirname, 'secrets.key');

// ---- AES-256-GCM：本机密钥文件加密/解密（零外部依赖，可在 SYSTEM 下无人值守运行）----
function getKey() {
  if (!fs.existsSync(KEY_FILE)) {
    const k = crypto.randomBytes(32);
    fs.writeFileSync(KEY_FILE, k); // 首次生成，OS 文件系统 ACL 保护（仅本机/管理员可读）
  }
  return fs.readFileSync(KEY_FILE); // 32 字节
}
function dpapiProtect(plain) {
  const key = getKey();
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
  const enc = Buffer.concat([cipher.update(String(plain), 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, tag, enc]).toString('base64'); // [iv(12) | tag(16) | ciphertext]
}
function dpapiUnprotect(b64) {
  const buf = Buffer.from(b64, 'base64');
  const iv = buf.slice(0, 12);
  const tag = buf.slice(12, 28);
  const enc = buf.slice(28);
  const key = getKey();
  const decipher = crypto.createDecipheriv('aes-256-gcm', key, iv);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(enc), decipher.final()]).toString('utf8');
}

function loadSecrets(secretsFile) {
  try {
    if (!fs.existsSync(secretsFile)) return null;
    const raw = JSON.parse(fs.readFileSync(secretsFile, 'utf8'));
    if (!raw.username || !raw.password) return null;
    return {
      username: dpapiUnprotect(raw.username),
      password: dpapiUnprotect(raw.password),
      updatedAt: raw.updatedAt || null,
    };
  } catch (e) {
    console.error('[auth] 读取/解密凭据失败：', e.message);
    return null;
  }
}

function persistSecrets(secretsFile, creds) {
  const obj = {
    username: dpapiProtect(creds.username),
    password: dpapiProtect(creds.password),
    updatedAt: new Date().toISOString(),
  };
  const tmp = secretsFile + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(obj, null, 2));
  fs.renameSync(tmp, secretsFile); // 原子替换，避免半写
}

function decodeJwtExp(token) {
  try {
    const seg = String(token).split('.')[1];
    const p = JSON.parse(Buffer.from(seg, 'base64').toString('utf8'));
    return p.exp ? p.exp * 1000 : null;
  } catch (_) { return null; }
}

function needsRefresh(token, leadMs) {
  if (!token) return true;
  const exp = decodeJwtExp(token);
  if (!exp) return true;
  return Date.now() >= (exp - leadMs);
}

function doLogin(loginCfg, targetBaseUrl, creds) {
  return new Promise((resolve, reject) => {
    const url = new URL(loginCfg.path, targetBaseUrl);
    const body = `${encodeURIComponent(loginCfg.usernameField)}=${encodeURIComponent(creds.username)}&${encodeURIComponent(loginCfg.passwordField)}=${encodeURIComponent(creds.password)}`;
    const lib = url.protocol === 'https:' ? https : http;
    const opts = {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Content-Length': Buffer.byteLength(body),
        'User-Agent': 'SCG-Proxy/1.0',
        'Accept': 'application/json, text/plain, */*',
      },
      timeout: 10000,
    };
    if (url.protocol === 'https:') opts.rejectUnauthorized = false;
    const req = lib.request(url, opts, (res) => {
      const c = [];
      res.on('data', (x) => c.push(x));
      res.on('end', () => {
        const text = Buffer.concat(c).toString('utf8');
        let json = null; try { json = JSON.parse(text); } catch (_) {}
        if (res.statusCode === 200 && json && json.code === 200 && json.data) {
          resolve(json.data); // token
        } else {
          const msg = json ? (json.message || json.msg || JSON.stringify(json).slice(0, 200)) : text.slice(0, 200);
          reject(new Error('登录失败(HTTP ' + res.statusCode + ')：' + msg));
        }
      });
    });
    req.on('timeout', () => { req.destroy(); reject(new Error('登录请求超时(>10s)')); });
    req.on('error', (e) => reject(new Error('登录请求错误：' + e.message)));
    req.write(body);
    req.end();
  });
}

function createAuthManager(cfg) {
  const secretsFile = cfg.secretsFile ? path.resolve(__dirname, cfg.secretsFile) : path.join(__dirname, 'secrets.json');
  const loginCfg = Object.assign({
    path: '/login',
    method: 'form',
    usernameField: 'username',
    passwordField: 'password',
    tokenPath: 'data',
  }, cfg.login || {});
  const leadMs = (cfg.refreshLeadDays || 5) * 86400000;
  const state = {
    runtimeToken: (cfg.token || '').trim(),
    lastError: null,
    lastRefresh: null,
    hasCreds: false,
  };
  let refreshPromise = null;
  let timer = null;

  function getRuntimeToken() { return state.runtimeToken; }

  async function ensureFreshToken() {
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      if (!needsRefresh(state.runtimeToken, leadMs)) return state.runtimeToken;
      const creds = loadSecrets(secretsFile);
      if (!creds) { state.lastError = '未配置凭据（请在「更多→解绑账号」录入账号密码）'; throw new Error(state.lastError); }
      state.hasCreds = true;
      try {
        const tk = await doLogin(loginCfg, cfg.targetBaseUrl, creds);
        state.runtimeToken = tk;
        state.lastRefresh = new Date().toISOString();
        state.lastError = null;
        persistTokenToConfig(cfg, tk);
        console.log('[auth] token 已自动刷新，新有效期至', new Date(decodeJwtExp(tk)).toISOString());
        return tk;
      } catch (e) {
        state.lastError = e.message;
        throw e;
      } finally {
        setTimeout(() => { refreshPromise = null; }, 2000);
      }
    })();
    return refreshPromise;
  }

  async function setupCredentials(creds) {
    const tk = await doLogin(loginCfg, cfg.targetBaseUrl, creds); // 先验证登录是否成功
    persistSecrets(secretsFile, creds);                        // 验证通过 -> 持久化密文
    state.runtimeToken = tk;
    state.lastRefresh = new Date().toISOString();
    state.lastError = null;
    state.hasCreds = true;
    persistTokenToConfig(cfg, tk);
    return { token: tk, exp: decodeJwtExp(tk) };
  }

  function status() {
    const exp = decodeJwtExp(state.runtimeToken);
    return {
      hasCredentials: state.hasCreds || fs.existsSync(secretsFile),
      hasRuntimeToken: !!state.runtimeToken,
      tokenExp: exp,
      tokenExpISO: exp ? new Date(exp).toISOString() : null,
      needsRefresh: needsRefresh(state.runtimeToken, leadMs),
      loginUrl: new URL(loginCfg.path, cfg.targetBaseUrl).toString(),
      leadDays: cfg.refreshLeadDays || 5,
      lastError: state.lastError,
      lastRefresh: state.lastRefresh,
    };
  }

  async function init() {
    const creds = loadSecrets(secretsFile);
    state.hasCreds = !!creds;
    if (creds && needsRefresh(state.runtimeToken, leadMs)) {
      try { await ensureFreshToken(); }
      catch (e) { console.error('[auth] 启动自动刷新失败：', e.message); }
    }
    // 周期性自检：每 6 小时检查一次，临近过期（refreshLeadDays 内）则主动换发，
    // 无需等 401、也无需重启；unref 避免阻止进程退出。
    if (!timer) {
      timer = setInterval(() => {
        ensureFreshToken().catch((e) => console.error('[auth] 周期刷新失败：', e.message));
      }, 6 * 3600 * 1000);
      if (timer.unref) timer.unref();
    }
  }

  return { getRuntimeToken, ensureFreshToken, setupCredentials, status, init, needsRefresh };
}

// 把新 token 落盘到 config.json 的 token 字段（原子写），供重启后直接可用
function persistTokenToConfig(cfg, token) {
  try {
    const cf = cfg.__file;
    if (!cf || !fs.existsSync(cf)) return;
    const raw = JSON.parse(fs.readFileSync(cf, 'utf8'));
    raw.token = token;
    const tmp = cf + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(raw, null, 2));
    fs.renameSync(tmp, cf);
  } catch (e) {
    console.error('[auth] token 落盘到 config.json 失败：', e.message);
  }
}

module.exports = { createAuthManager, decodeJwtExp, needsRefresh, loadSecrets };
