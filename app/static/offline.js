/* Offline write queue for the shopping list. Forms/buttons marked [data-offline] are posted with fetch;
   when that fails (offline / server unreachable) the request is stored in localStorage and the UI is
   updated optimistically. The queue replays in order when the connection returns. */
(() => {
  const KEY = 'hearth-queue-v1';
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch { return []; } };
  const save = q => { try { localStorage.setItem(KEY, JSON.stringify(q)); } catch {} };
  const badge = document.getElementById('offline-badge');
  const show = () => { const n = load().length; if (badge) { badge.hidden = !n && navigator.onLine;
    badge.textContent = navigator.onLine ? `Syncing ${n} change${n === 1 ? '' : 's'}…` : (n ? `Offline · ${n} change${n === 1 ? '' : 's'} waiting` : 'Offline'); } };

  async function send(item) {
    const body = new URLSearchParams(item.body);
    const r = await fetch(item.url, {method: 'POST', body, headers: {'X-Requested-With': 'fetch'}, redirect: 'manual'});
    if (r.status >= 500) throw new Error('server ' + r.status);
    return r;
  }
  let replaying = false;
  async function replay() {
    let q = load();
    if (replaying || !q.length || !navigator.onLine) return show();
    replaying = true;
    show();
    while (q.length) {
      try { await send(q[0]); } catch { break; }
      q.shift(); save(q);
    }
    replaying = false;
    show();
    if (!q.length) location.reload();
  }
  window.hearthQueue = {
    async post(url, data, optimistic) {
      const item = {url, body: Object.fromEntries(new URLSearchParams(data)), at: Date.now()};
      try {
        if (!navigator.onLine) throw new Error('offline');
        await send(item);
        return true;
      } catch {
        const q = load(); q.push(item); save(q);
        if (optimistic) optimistic();
        show();
        return false;
      }
    },
  };
  window.addEventListener('online', replay);
  window.addEventListener('offline', show);
  document.addEventListener('DOMContentLoaded', () => { show(); replay(); });
})();
