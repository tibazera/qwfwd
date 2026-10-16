const HOST = 'com.qwfwd.ping';
let port;
let sequence = 0;
const pending = new Map();

function allowed(sender) {
  try {
    const url = new URL(sender.url);
    return sender.id === chrome.runtime.id && sender.frameId === 0 &&
      url.origin === 'https://tibazera.github.io' && url.pathname.startsWith('/qwfwd/');
  } catch { return false; }
}

chrome.runtime.onMessage.addListener((request, sender, reply) => {
  if (!allowed(sender)) { reply({ error: 'origin_denied' }); return; }
  if (!request || !['health', 'ping'].includes(request.op) ||
      (request.op === 'ping' && (typeof request.target !== 'string' || request.target.length > 32))) {
    reply({ error: 'invalid_request' }); return;
  }
  if (pending.size >= 8) { reply({ error: 'busy' }); return; }
  let requestId;
  try {
    if (!port) {
      port = chrome.runtime.connectNative(HOST);
      port.onMessage.addListener(message => {
        const item = pending.get(message.id);
        if (!item) return;
        clearTimeout(item.timer); pending.delete(message.id); item.reply(message);
      });
      port.onDisconnect.addListener(() => {
        const detail = chrome.runtime.lastError?.message;
        port = undefined;
        for (const item of pending.values()) {
          clearTimeout(item.timer); item.reply({ error: 'helper_unavailable', detail });
        }
        pending.clear();
      });
    }
    const id = String(++sequence);
    requestId = id;
    const timer = setTimeout(() => {
      pending.delete(id); reply({ error: 'helper_timeout' });
    }, 15000);
    pending.set(id, { reply, timer });
    port.postMessage({ id, op: request.op, target: request.target });
    return true;
  } catch {
    const item = pending.get(requestId);
    if (item) { clearTimeout(item.timer); pending.delete(requestId); }
    reply({ error: 'helper_unavailable' });
  }
});
