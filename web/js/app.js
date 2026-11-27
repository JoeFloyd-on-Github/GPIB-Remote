// GPIB-Remote web client: connection, instrument list, routing between views, shared state
// (type models, reading history for charts, preferences in localStorage).

import { Client } from "./client.js";
import { h, clear, toast, lockText, errText } from "./util.js";
import { InstrumentView } from "./instrument.js";
import { InstanceEditor, TypesView, RawView, ServerView } from "./admin.js";

const MAX_HISTORY = 20000;

class App {
  constructor() {
    this.prefs = load("gpib.prefs", {});
    this.models = new Map();
    this.hist = new Map();        // "inst key" -> [[t, v], ...]
    this.instances = [];
    this.view = null;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    this.client = new Client(proto + "//" + location.host + "/ws",
      () => ({ client: "web", user: this.user(), host: "" }));
    this.client.on((ev) => this.onEvent(ev));
    this.client.onStatus((on) => this.onStatus(on));
    document.getElementById("user").addEventListener("click", () => this.askUser());
    this.renderUser();
    window.addEventListener("hashchange", () => this.route());
    window.addEventListener("beforeunload", () => save("gpib.prefs", this.prefs));
    setInterval(() => save("gpib.prefs", this.prefs), 10000);
    this.client.connect();
  }

  user() {
    if (!this.prefs.user) this.prefs.user = "web";
    return this.prefs.user;
  }

  askUser() {
    const name = window.prompt("Your name, shown to others in control locks and history.\n(Takes effect on reconnect: reload the page.)", this.user());
    if (name && name.trim()) {
      this.prefs.user = name.trim().slice(0, 64);
      save("gpib.prefs", this.prefs);
      this.renderUser();
      if (window.confirm("Reconnect now with the new name? Control locks you hold will be released.")) location.reload();
    }
  }

  renderUser() { document.getElementById("user").textContent = this.user(); }

  onStatus(on) {
    const c = document.getElementById("conn");
    c.textContent = on ? "connected" : "reconnecting…";
    c.className = "pill " + (on ? "good" : "bad");
    if (on) {
      if (!this.prefs.user) this.askUserFirst();
      this.loadInstances();
      // An instrument view resyncs itself (the client re-subscribes); others are rebuilt.
      if (!this.view || this.view.isHome || (this.view.needsReload && !(this.view instanceof InstrumentView))) this.route();
    } else if (this.view) {
      this.view.needsReload = true;
    }
  }

  askUserFirst() {
    // first visit: suggest naming yourself so locks don't all say "web"
    setTimeout(() => toast("Tip: click “" + this.user() + "” at the top right to set your name.", "info", 8000), 500);
  }

  async model(ref) {
    if (!this.models.has(ref)) this.models.set(ref, (await this.client.request("types.get", { type: ref })).model);
    return this.models.get(ref);
  }

  forgetModels() { this.models.clear(); }

  history(inst, key) {
    const k = inst + " " + key;
    if (!this.hist.has(k)) this.hist.set(k, []);
    return this.hist.get(k);
  }

  record(inst, key, t, v) {
    if (typeof v !== "number" || !isFinite(v) || !t) return;
    const a = this.history(inst, key);
    if (a.length && a[a.length - 1][0] >= t) return;
    a.push([t, v]);
    if (a.length > MAX_HISTORY) a.splice(0, a.length - MAX_HISTORY);
  }

  recordState(inst, state) {
    for (const [k, e] of Object.entries(state || {})) if (e && e.source === "read") this.record(inst, k, e.t, e.value);
  }

  // Which readouts are charted, remembered per instance; default: every polled readout.
  chartKeys(inst, chartable) {
    const saved = (this.prefs.charts || {})[inst];
    if (saved) return chartable.filter((c) => saved.includes(c.key)).map((c) => c.key);
    return chartable.filter((c) => c.polled).map((c) => c.key);
  }

  setChartKey(inst, key, on) {
    const charts = this.prefs.charts || (this.prefs.charts = {});
    const view = this.view;
    const cur = charts[inst] || (view && view.chartable ? this.chartKeys(inst, view.chartable()) : []);
    charts[inst] = on ? [...new Set([...cur, key])] : cur.filter((k) => k !== key);
  }

  onEvent(ev) {
    if (ev.event === "reading" || (ev.event === "state" && ev.source === "read")) this.record(ev.instance, ev.key, ev.t, ev.value);
    if (ev.event === "lock_broken") toast("Your control of " + ev.instance + " was taken by " + ev.by + (ev.reason ? ": " + ev.reason : ""), "error", 10000);
    if (ev.event === "lock_expired") toast("Your control of " + ev.instance + " was released after being idle", "error", 10000);
    if (ev.event === "lock" || ev.event === "status" || ev.event === "instance") {
      const i = this.instances.find((x) => x.id === ev.instance);
      if (ev.event === "lock" && i) { i.lock = ev.lock; this.renderList(); }
      else if (ev.event === "status" && i) { i.online = ev.online; this.renderList(); }
      else if (ev.event === "instance") this.loadInstances();
    }
  }

  async loadInstances() {
    try {
      this.instances = (await this.client.request("instances.list")).instances;
      this.renderList();
      if (this.view && this.view.isHome) this.route();
    } catch (e) { /* offline: the list stays as it was */ }
  }

  renderList() {
    const ul = clear(document.getElementById("instlist"));
    const cur = decodeURIComponent((location.hash.match(/^#\/i\/([^/]+)/) || [])[1] || "");
    if (!this.instances.length) ul.appendChild(h("li", { class: "muted small" }, "none yet"));
    for (const i of this.instances) {
      const lockCls = !i.lock ? "" : i.lock.session === this.client.session ? "mine" : "other";
      ul.appendChild(h("li", { class: i.id === cur ? "cur" : "" },
        h("a", { href: "#/i/" + encodeURIComponent(i.id) },
          h("span", { class: "dot " + (i.online === false ? "bad" : i.online ? "good" : ""), title: i.online === false ? "not responding" : i.online ? "online" : "not contacted yet" }),
          h("span", { class: "iname" }, i.name || i.id),
          i.lock ? h("span", { class: "lockicon small " + lockCls, title: lockText(i.lock, this.client.session) }) : "",
          h("span", { class: "isub" }, i.id + " · " + i.address.board + ":" + i.address.pad))));
    }
  }

  route() {
    const hash = location.hash || "#/";
    const path = hash.slice(1).split("?")[0];
    const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
    if (this.view && this.view.destroy) this.view.destroy();
    let view;
    if (parts[0] === "i" && parts[1] && parts[2] === "edit") view = new InstanceEditor(this, parts[1]);
    else if (parts[0] === "i" && parts[1]) view = new InstrumentView(this, parts[1]);
    else if (parts[0] === "new") view = new InstanceEditor(this, null);
    else if (parts[0] === "types") view = new TypesView(this, parts[1] || null);
    else if (parts[0] === "raw") view = new RawView(this);
    else if (parts[0] === "server") view = new ServerView(this);
    else view = this.home();
    this.view = view;
    clear(document.getElementById("main")).appendChild(view.el);
    const nav = parts[0] === "types" ? "types" : parts[0] === "raw" ? "raw" : parts[0] === "server" ? "server" : "instruments";
    document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("cur", a.dataset.nav === nav));
    this.renderList();
  }

  home() {
    const el = h("section", { class: "view" });
    const v = { el, isHome: true };
    if (!this.client.online) { el.appendChild(h("p", { class: "muted" }, "connecting…")); return v; }
    el.appendChild(h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", null, "Instruments"), h("span", { class: "spacer" }),
        h("a", { class: "button", href: "#/new" }, "Add an instrument…")),
      this.instances.length ? h("table", { class: "grid" },
        h("thead", null, h("tr", null, ["Instrument", "Type", "GPIB", "Status", "Control"].map((x) => h("th", null, x)))),
        h("tbody", null, this.instances.map((i) => h("tr", null,
          h("td", null, h("a", { href: "#/i/" + encodeURIComponent(i.id) }, i.name || i.id), h("div", { class: "muted small" }, i.id)),
          h("td", null, i.title, h("div", { class: "muted small" }, i.type)),
          h("td", null, i.address.board + ":" + i.address.pad + (i.address.sad ? "." + i.address.sad : "")),
          h("td", null, i.online === false ? "not responding" : i.online ? "online" : "not contacted yet"),
          h("td", null, lockText(i.lock, this.client.session))))))
        : h("p", { class: "muted" }, "No instruments yet. Add one, or find what is on the bus with Raw GPIB → Find listeners.")));
    return v;
  }
}

function load(key, dflt) {
  try { return JSON.parse(localStorage.getItem(key)) || dflt; } catch { return dflt; }
}

function save(key, v) {
  try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* private mode */ }
}

window.addEventListener("error", (e) => toast("Client error: " + errText(e.error || e.message), "error"));
window.app = new App();
