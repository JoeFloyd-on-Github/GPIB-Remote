// WebSocket client for the GPIB-Remote protocol: the same JSON messages as TCP port 9000.
// request() returns a Promise; events go to listeners. Reconnects by itself, then says hello
// again and re-subscribes, so views survive a server restart.

export class ServerError extends Error {
  constructor(err) {
    super((err && (err.msg || err.code)) || "error");
    this.code = err && err.code;
    this.error = err || {};
  }
}

export class Client {
  constructor(url, hello) {
    this.url = url;
    this.hello = hello;            // () => {client, user, host}
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Set();
    this.statusListeners = new Set();
    this.subs = new Map();         // instance -> count
    this.online = false;
    this.session = null;
    this.retry = 0;
  }

  connect() {
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = async () => {
      this.retry = 0;
      try {
        const r = await this._send("hello", this.hello());
        this.session = r.session;
        // A server restarted with different web files (a deploy): this page's code is out of
        // date, so load the new one rather than run old code against the new server.
        if (r.web_build) {
          if (this.webBuild && this.webBuild !== r.web_build) { location.reload(); return; }
          this.webBuild = r.web_build;
        }
        const resync = [];
        for (const inst of this.subs.keys()) {
          const s = await this._send("subscribe", { instance: inst }).catch(() => null);
          if (s) resync.push({ event: "resync", instance: inst, state: s.state });
        }
        this.online = true;
        this._status(true);
        resync.forEach((ev) => this._emit(ev));
      } catch (e) {
        console.error("hello failed", e);
      }
    };
    ws.onmessage = (m) => {
      let msg;
      try { msg = JSON.parse(m.data); } catch { return; }
      if (msg.id != null && this.pending.has(msg.id)) {
        const p = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.ok) p.resolve(msg.result || {}); else p.reject(new ServerError(msg.error));
      } else if (msg.event) {
        this._emit(msg);
      }
    };
    ws.onclose = () => {
      const was = this.online;
      this.online = false;
      for (const p of this.pending.values()) p.reject(new ServerError({ code: "disconnected", msg: "connection to the server lost" }));
      this.pending.clear();
      if (was || this.retry === 0) this._status(false);
      const delay = Math.min(10000, 500 * 2 ** this.retry++);
      setTimeout(() => this.connect(), delay);
    };
  }

  _send(op, params) {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new ServerError({ code: "offline", msg: "not connected to the server" }));
        return;
      }
      const id = this.nextId++;
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(Object.assign({}, params, { id, op })));
    });
  }

  request(op, params = {}) {
    if (!this.online) return Promise.reject(new ServerError({ code: "offline", msg: "not connected to the server" }));
    return this._send(op, params);
  }

  // Reference-counted, so two views of one instrument share a subscription.
  async subscribe(instance) {
    this.subs.set(instance, (this.subs.get(instance) || 0) + 1);
    return this.request("subscribe", { instance });
  }

  async unsubscribe(instance) {
    const n = (this.subs.get(instance) || 0) - 1;
    if (n > 0) { this.subs.set(instance, n); return; }
    this.subs.delete(instance);
    if (this.online) await this.request("unsubscribe", { instance }).catch(() => {});
  }

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  onStatus(fn) { this.statusListeners.add(fn); return () => this.statusListeners.delete(fn); }
  _emit(ev) { for (const fn of [...this.listeners]) { try { fn(ev); } catch (e) { console.error(e); } } }
  _status(on) { for (const fn of [...this.statusListeners]) { try { fn(on); } catch (e) { console.error(e); } } }
}
