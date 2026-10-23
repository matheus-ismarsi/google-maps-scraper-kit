// WhatsApp Web bridge for the CRM. Unofficial: it drives web.whatsapp.com in a hidden Chrome through
// whatsapp-web.js, logged in with a QR code like "Aparelhos conectados" on the phone.
// The CRM server (server.py) starts it and talks to it on 127.0.0.1 only, with a per-run token.
//   GET  /status            -> { status, qr, me, name, error }
//   POST /send {to, body}   -> { id, wa_id }
//   POST /logout            (unlinks this device; a new QR code follows)
//   POST /shutdown          (closes Chrome cleanly; the CRM calls it when it stops)
// Incoming messages and delivery acks are pushed to the CRM at POST {CRM_URL}/api/bridge/event.
const fs = require('fs');
const http = require('http');
const path = require('path');
const QRCode = require('qrcode');
const { Client, LocalAuth } = require('whatsapp-web.js');

const PORT = Number(process.env.BRIDGE_PORT || 8766);
const CRM_URL = process.env.CRM_URL || 'http://127.0.0.1:8765';
const TOKEN = process.env.BRIDGE_TOKEN || '';
const DATA = process.env.BRIDGE_DATA || path.join(__dirname, '..', 'data', 'wa-session');

function findBrowser() {
  const env = process.env;
  const candidates = [
    env.BROWSER_PATH,
    `${env.PROGRAMFILES}\\Google\\Chrome\\Application\\chrome.exe`,
    `${env['PROGRAMFILES(X86)']}\\Google\\Chrome\\Application\\chrome.exe`,
    `${env.LOCALAPPDATA}\\Google\\Chrome\\Application\\chrome.exe`,
    `${env['PROGRAMFILES(X86)']}\\Microsoft\\Edge\\Application\\msedge.exe`,
    `${env.PROGRAMFILES}\\Microsoft\\Edge\\Application\\msedge.exe`,
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/usr/bin/google-chrome', '/usr/bin/chromium',
  ];
  return candidates.find(p => p && fs.existsSync(p));
}

const log = (...a) => console.log('[whatsapp]', ...a);
const state = { status: 'starting', qr: null, me: null, name: null, error: null };
const browser = findBrowser();
if (!browser) { state.status = 'error'; state.error = 'Chrome ou Edge não encontrado. Instale o Google Chrome.'; }

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: DATA }),
  puppeteer: { headless: true, executablePath: browser, args: ['--no-first-run', '--disable-gpu'] },
});

client.on('qr', async qr => {
  state.status = 'qr'; state.error = null;
  state.qr = await QRCode.toDataURL(qr, { margin: 1, width: 280 });
  log('Escaneie o QR code no CRM (botão WhatsApp).');
});
client.on('authenticated', () => { state.status = 'authenticating'; state.qr = null; });
client.on('ready', () => {
  state.status = 'ready'; state.qr = null; state.error = null;
  state.me = client.info?.wid?.user || null; state.name = client.info?.pushname || null;
  log(`Conectado como +${state.me}${state.name ? ` (${state.name})` : ''}.`);
});
client.on('auth_failure', msg => { state.status = 'auth_failure'; state.error = String(msg); log('Falha no login:', msg); });
client.on('disconnected', reason => {
  state.status = 'disconnected'; state.error = String(reason); state.me = null;
  log('Desconectado:', reason, '— gerando um novo QR code em instantes.');
  setTimeout(start, 3000);
});

async function push(event) {
  for (let i = 0; i < 4; i++) {
    try {
      const r = await fetch(`${CRM_URL}/api/bridge/event`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Bridge-Token': TOKEN }, body: JSON.stringify(event),
      });
      if (r.ok) return;
    } catch {}
    await new Promise(r => setTimeout(r, 1500 * (i + 1)));
  }
  log('Não consegui entregar um evento ao CRM:', event.type);
}

const MEDIA = { image: '📷 Imagem', ptt: '🎤 Áudio', audio: '🎤 Áudio', video: '🎬 Vídeo', document: '📄 Documento', sticker: 'Figurinha', location: '📍 Localização', vcard: '👤 Contato' };
client.on('message', async msg => {
  if (msg.fromMe || msg.isStatus || msg.from.endsWith('@g.us') || msg.from.endsWith('@broadcast') || msg.from.endsWith('@newsletter')) return;
  let number = msg.from.split('@')[0], name = '';
  try { const c = await msg.getContact(); number = c.number || number; name = c.pushname || c.name || ''; } catch {}
  const body = msg.type === 'chat' ? msg.body
    : `[${MEDIA[msg.type] || msg.type} recebido. Veja no celular.]` + (msg.body && msg.type !== 'chat' && msg.type !== 'ptt' ? `\n${msg.body}` : '');
  push({ type: 'message', id: msg.id._serialized, from: number, chat: msg.from, name, body, timestamp: msg.timestamp });
});
client.on('message_ack', (msg, ack) => {
  if (!msg.fromMe) return;
  const status = { [-1]: 'failed', 1: 'sent', 2: 'delivered', 3: 'read', 4: 'read' }[ack];
  if (status) push({ type: 'ack', id: msg.id._serialized, status });
});

const sleep = ms => new Promise(r => setTimeout(r, ms));
async function send(to, body) {
  if (state.status !== 'ready') throw Object.assign(new Error('O WhatsApp não está conectado. Abra o botão WhatsApp e escaneie o QR code.'), { http: 409 });
  // getNumberId also resolves Brazilian numbers with or without the 9th digit.
  const wid = await client.getNumberId(String(to).replace(/\D/g, ''));
  if (!wid) throw Object.assign(new Error('Esse número não tem WhatsApp.'), { http: 404 });
  // Behave like a person: show "digitando…" for a moment before the message goes out.
  try {
    const chat = await client.getChatById(wid._serialized);
    await chat.sendStateTyping();
    await sleep(Math.min(6000, 1200 + body.length * 25 + Math.random() * 1500));
    await chat.clearState();
  } catch {}
  const m = await client.sendMessage(wid._serialized, body, { linkPreview: false });
  return { id: m.id._serialized, wa_id: wid.user };
}

http.createServer(async (req, res) => {
  const reply = (code, obj) => { res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };
  if (req.headers['x-bridge-token'] !== TOKEN) return reply(403, { error: 'token inválido' });
  let raw = '';
  for await (const chunk of req) raw += chunk;
  try {
    if (req.method === 'GET' && req.url === '/status') return reply(200, state);
    if (req.method === 'POST' && req.url === '/send') {
      const { to, body } = JSON.parse(raw || '{}');
      if (!to || !body) return reply(400, { error: 'to e body são obrigatórios' });
      return reply(200, await send(to, body));
    }
    if (req.method === 'POST' && req.url === '/logout') {
      try { await client.logout(); } catch {}
      state.status = 'starting'; state.me = null;
      start();
      return reply(200, { ok: true });
    }
    if (req.method === 'POST' && req.url === '/shutdown') {
      reply(200, { ok: true });
      try { await client.destroy(); } catch {}
      process.exit(0);
    }
    reply(404, { error: 'not found' });
  } catch (e) {
    reply(e.http || 500, { error: e.message });
  }
}).listen(PORT, '127.0.0.1', () => log(`ponte ouvindo em 127.0.0.1:${PORT}`));

// WhatsApp Web sometimes reloads itself while it boots ("Execution context was destroyed"):
// close the browser and try again a few times before giving up.
async function start(attempt = 1) {
  state.status = 'starting'; state.error = null;
  try {
    await client.initialize();
  } catch (e) {
    if (attempt >= 5) { state.status = 'error'; state.error = e.message; log('Erro ao iniciar:', e.message); return; }
    log(`Tentando de novo (${attempt}/4): ${e.message}`);
    try { await client.destroy(); } catch {}
    await sleep(2000 * attempt);
    return start(attempt + 1);
  }
}
if (browser) {
  log('Iniciando o WhatsApp Web…');
  start();
}
// If the CRM went away without stopping us (window closed, crash), close Chrome and exit.
let misses = 0;
setInterval(async () => {
  try { misses = (await fetch(`${CRM_URL}/api/bridge/ping`)).ok ? 0 : misses + 1; } catch { misses++; }
  if (misses >= 3) { log('O CRM fechou; encerrando o WhatsApp Web.'); try { await client.destroy(); } catch {} process.exit(0); }
}, 15000);
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(sig, async () => { try { await client.destroy(); } catch {} process.exit(0); });
