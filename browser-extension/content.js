// Only the project page can request fixed QW measurements, never arbitrary packets.
const channel = 'qwfwd-udp-v1';
window.addEventListener('message', event => {
  if (event.source !== window || event.origin !== location.origin) return;
  const request = event.data;
  if (!request || request.channel !== channel || request.direction !== 'request' ||
      typeof request.id !== 'string' || request.id.length > 64) return;
  if (request.op !== 'health' && request.op !== 'ping') return;
  chrome.runtime.sendMessage({ op: request.op, target: request.target }, response => {
    const error = chrome.runtime.lastError;
    window.postMessage({ channel, direction: 'response', id: request.id,
      result: error ? { error: 'extension_unavailable' } : response }, location.origin);
  });
});
