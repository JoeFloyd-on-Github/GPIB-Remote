// One instrument: its controls and readouts grouped as the type's menu groups them (one card per
// channel), live values, actions, the control lock, and live charts of the readouts.

import { h, clear, valueText, age, bounds, rangeText, lockText, toast, errText, layoutOf, itemOf, decimals } from "./util.js";
import { TimeChart } from "./chart.js";

const WINDOWS = [[60, "1 min"], [300, "5 min"], [900, "15 min"], [3600, "1 h"], [14400, "4 h"]];

export class InstrumentView {
  constructor(app, id) {
    this.app = app;
    this.id = id;
    this.cells = new Map();     // key -> {update(entry)}
    this.state = {};
    this.charts = new Map();    // unit -> TimeChart
    this.el = h("section", { class: "view instrument" }, h("p", { class: "muted" }, "loading…"));
    this.off = app.client.on((ev) => this.onEvent(ev));
    this.timer = setInterval(() => this.tick(), 1000);
    this.load();
  }

  destroy() {
    this.dead = true;
    this.off();
    clearInterval(this.timer);
    for (const c of this.charts.values()) c.destroy();
    if (this.subscribed) this.app.client.unsubscribe(this.id);
  }

  get client() { return this.app.client; }
  get mine() { return this.lock && this.lock.session === this.client.session; }
  get othersLock() { return this.lock && this.lock.session !== this.client.session; }

  async load() {
    try {
      const st = await this.client.request("dev.state", { instance: this.id });
      this.summary = st.summary;
      this.lock = st.summary.lock;
      this.model = await this.app.model(st.type);
      const inst = await this.client.request("instances.get", { instance: this.id });
      this.limits = ((inst.instance.overrides || {}).limits) || {};
      if (this.dead) return;
      if (!this.subscribed) {
        const s = await this.client.subscribe(this.id);
        this.subscribed = true;
        this.state = s.state;
      } else {
        this.state = st.state;
      }
      if (this.dead) return;
      this.app.recordState(this.id, this.state);
      this.render();
    } catch (e) {
      clear(this.el).appendChild(h("div", { class: "card error" },
        h("h2", null, this.id), h("p", null, errText(e)),
        h("p", null, h("a", { href: "#/" }, "Back to the list"))));
    }
  }

  // -- events -----------------------------------------------------------------------------------
  onEvent(ev) {
    if (ev.event === "resync" && ev.instance === this.id) {
      this.app.recordState(this.id, ev.state);
      this.load();   // after a reconnect: state, lock and status may all have changed
      return;
    }
    if (ev.instance !== this.id && ev.id !== this.id) return;
    if (ev.event === "reading" || ev.event === "state") {
      const e = { value: ev.value, t: ev.t, source: ev.source || "read" };
      this.state[ev.key] = e;
      const c = this.cells.get(ev.key);
      if (c) c.update(e);
      for (const ch of this.charts.values()) if (ch.series.has(ev.key)) ch.redraw();
    } else if (ev.event === "lock") {
      this.lock = ev.lock;
      this.renderLock();
      this.applyLock();
    } else if (ev.event === "status") {
      this.summary.online = ev.online;
      this.renderStatus(ev.error);
    } else if (ev.event === "instance" && ev.op === "updated") {
      this.load();
    } else if (ev.event === "instance" && ev.op === "deleted") {
      clear(this.el).appendChild(h("div", { class: "card" }, h("p", null, this.id + " was deleted.")));
    }
  }

  tick() {
    for (const [k, c] of this.cells) if (c.ageEl) c.ageEl.textContent = age((this.state[k] || {}).t);
    for (const ch of this.charts.values()) ch.redraw();
    if (this.lockAge && this.lock) this.renderLock();
  }

  // -- requests ---------------------------------------------------------------------------------
  async set(key, value) {
    try {
      const e = await this.client.request("dev.set", { instance: this.id, key, value });
      this.state[key] = e;
      const c = this.cells.get(key);
      if (c) c.update(e);
      return true;
    } catch (err) {
      toast(key + ": " + errText(err), "error");
      const c = this.cells.get(key);
      if (c) c.update(this.state[key] || {});
      return false;
    }
  }

  async read(key) {
    try {
      const e = await this.client.request("dev.get", { instance: this.id, key });
      this.state[key] = e;
      const c = this.cells.get(key);
      if (c) c.update(e);
    } catch (err) { toast(key + ": " + errText(err), "error"); }
  }

  async action(name, d, channel) {
    if (d.confirm && !window.confirm((d.label || name) + (channel ? " (" + channel + ")" : "") + "?" +
        (d.description ? "\n\n" + d.description.trim() : ""))) return;
    try {
      await this.client.request("dev.action", { instance: this.id, action: name, channel: channel || null });
      toast((d.label || name) + ": done", "ok");
      if (d.refresh) this.refresh(true);
    } catch (err) { toast((d.label || name) + ": " + errText(err), "error"); }
  }

  async refresh(quiet) {
    try {
      const r = await this.client.request("dev.refresh", { instance: this.id });
      Object.assign(this.state, r.state);
      for (const [k, c] of this.cells) c.update(this.state[k] || {});
      if (!quiet) toast("Re-read every setting from the instrument", "ok");
    } catch (err) { toast(errText(err), "error"); }
  }

  async takeLock(force) {
    let reason;
    if (force) {
      reason = window.prompt("Take control from " + this.lock.holder + "?\nThey will be told, with this reason:", "");
      if (reason == null) return;
    }
    try {
      const r = await this.client.request("dev.lock", { instance: this.id, force: !!force, reason: reason || undefined });
      this.lock = r.lock;
      this.renderLock();
      this.applyLock();
    } catch (err) { toast(errText(err), "error"); }
  }

  async releaseLock() {
    try {
      await this.client.request("dev.unlock", { instance: this.id });
      this.lock = null;
      this.renderLock();
      this.applyLock();
    } catch (err) { toast(errText(err), "error"); }
  }

  async identify() {
    try {
      const r = await this.client.request("instances.identify", { instance: this.id });
      toast(r.probe + " → " + r.response + (r.match ? "  (matches the type)" : "  (does NOT match the type)"), r.match ? "ok" : "error", 8000);
    } catch (err) { toast(errText(err), "error"); }
  }

  // -- rendering --------------------------------------------------------------------------------
  render() {
    const m = this.model, s = this.summary;
    this.cells.clear();
    for (const c of this.charts.values()) c.destroy();
    this.charts.clear();
    this.statusEl = h("span", { class: "pill" });
    this.lockEl = h("div", { class: "lockbar" });
    const a = s.address;
    const head = h("div", { class: "inst-head" },
      h("div", null,
        h("h1", null, s.name || this.id, " ", this.statusEl),
        h("div", { class: "muted" }, this.id, " · ", m.title || m.type, " · ",
          h("a", { href: "#/types/" + encodeURIComponent(m.ref) }, m.ref), " · GPIB ", a.board + ":" + a.pad + (a.sad ? "." + a.sad : ""),
          m.verified ? "" : h("span", { class: "badge warn", title: "This type has not been checked against a real instrument" }, "unverified type"))),
      h("span", { class: "spacer" }),
      h("div", { class: "btns" },
        h("button", { onclick: () => this.refresh(), title: "Read every readable setting back from the instrument" }, "Re-read all"),
        h("button", { onclick: () => this.identify() }, "Identify"),
        h("a", { class: "button", href: "#/i/" + encodeURIComponent(this.id) + "/edit" }, "Edit")));
    const cards = h("div", { class: "cards" });
    for (const ent of layoutOf(m)) {
      if (typeof ent === "string") { cards.appendChild(this.card("General", [ent], null)); continue; }
      const [label, items] = Object.entries(ent)[0];
      if (label === "{channel}") {
        for (const [ch, cd] of Object.entries(m.channels || {})) cards.appendChild(this.card(cd.label || ch, items, ch));
      } else {
        cards.appendChild(this.card(label, items, null));
      }
    }
    this.chartBox = h("div", { class: "charts" });
    const win = h("select", { onchange: () => { for (const c of this.charts.values()) c.setWindow(+win.value); this.app.prefs.window = +win.value; } },
      WINDOWS.map(([v, l]) => h("option", { value: v, selected: v === (this.app.prefs.window || 300) }, l)));
    clear(this.el).append(head, this.lockEl,
      m.description ? h("p", { class: "muted desc" }, m.description.trim()) : "",
      cards,
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", null, "Live readings"), h("span", { class: "spacer" }),
          h("label", { class: "small" }, "Show last ", win)),
        h("p", { class: "muted small" }, "Readouts are polled while someone is watching. Tick “chart” on a reading to add or remove it."),
        this.chartBox));
    this.renderStatus();
    this.renderLock();
    for (const key of this.app.chartKeys(this.id, this.chartable())) this.chartOn(key);
    this.applyLock();
    // Settings nobody has read yet: read them back once, so the page doesn't show "?".
    const unknown = Object.entries(this.model.controls || {}).some(([n, d]) => d.get &&
      Object.entries(this.state).some(([k, e]) => (k === n || k.endsWith("." + n)) && e.value == null));
    if (unknown && this.summary.online !== false && !this.autoRefreshed) { this.autoRefreshed = true; this.refresh(true); }
  }

  chartable() {
    // Every numeric readout key, in layout order, with its unit and default "charted" flag.
    const out = [];
    const m = this.model;
    const seen = new Set();
    const addKey = (name, ch) => {
      const d = (m.readouts || {})[name];
      if (!d || d.kind === "text" || d.kind === "enum" || d.kind === "bool") return;
      const key = ch ? ch + "." + name : name;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ key, unit: d.unit || "", label: d.label || name, ch, polled: !!d.poll });
    };
    const walk = (items, ch) => {
      for (const it of items) {
        if (typeof it === "string") {
          const d = (m.readouts || {})[it];
          if (!d) continue;
          if (d.per_channel && !ch) for (const c of Object.keys(m.channels || {})) addKey(it, c);
          else addKey(it, d.per_channel ? ch : null);
        } else {
          const [label, sub] = Object.entries(it)[0];
          if (label === "{channel}") for (const c of Object.keys(m.channels || {})) walk(sub, c);
          else walk(sub, ch);
        }
      }
    };
    walk(layoutOf(m), null);
    return out;
  }

  chartOn(key) {
    const all = this.chartable();
    const info = all.find((x) => x.key === key);
    if (!info) return;
    let chart = this.charts.get(info.unit);
    if (!chart) {
      const title = info.unit ? info.unit === "V" ? "Volts" : info.unit === "A" ? "Amps" : info.unit : "Readings";
      chart = new TimeChart({ unit: info.unit, title, windowSec: this.app.prefs.window || 300 });
      this.charts.set(info.unit, chart);
      this.chartBox.appendChild(chart.el);
    }
    const sameUnit = all.filter((x) => x.unit === info.unit);
    const slot = Math.min(8, sameUnit.indexOf(info) + 1);
    const chLabel = info.ch ? ((this.model.channels[info.ch] || {}).label || info.ch) : "";
    const label = sameUnit.filter((x) => x.label === info.label).length > 1 ? chLabel : (chLabel ? chLabel + " " : "") + info.label;
    chart.addSeries(key, label, slot, this.app.history(this.id, key));
    chart.redraw();
    const c = this.cells.get(key);
    if (c && c.chartBox) c.chartBox.checked = true;
  }

  chartOff(key) {
    for (const [unit, chart] of this.charts) {
      if (!chart.series.has(key)) continue;
      chart.removeSeries(key);
      if (!chart.series.size) { chart.destroy(); chart.el.remove(); this.charts.delete(unit); }
    }
  }

  renderStatus(error) {
    const on = this.summary.online;
    this.statusEl.className = "pill " + (on === false ? "bad" : on ? "good" : "");
    this.statusEl.textContent = on === false ? "not responding" : on ? "online" : "not contacted";
    this.statusEl.title = error || "";
  }

  renderLock() {
    const L = this.lock;
    const btns = [];
    if (!L) btns.push(h("button", { class: "primary", onclick: () => this.takeLock(false) }, "Take control"));
    else if (this.mine) btns.push(h("button", { onclick: () => this.releaseLock() }, "Release control"));
    else btns.push(h("button", { class: "danger", onclick: () => this.takeLock(true) }, "Take over…"));
    const text = !L ? "Nobody has control: anyone may change settings. Take control to keep others out while you work."
      : this.mine ? "You have control. Others can watch; their changes are refused until you release it."
        : "Controlled by " + L.holder + " since " + age(L.since).replace(" ago", "") + " ago. You can watch; changes are refused.";
    this.lockAge = !!L;
    clear(this.lockEl).append(
      h("span", { class: "lockicon " + (!L ? "free" : this.mine ? "mine" : "other"), "aria-hidden": "true" }),
      h("span", { class: "locktext" }, h("strong", null, lockText(L, this.client.session)), " — ", text),
      h("span", { class: "spacer" }), ...btns);
    this.lockEl.className = "lockbar " + (!L ? "free" : this.mine ? "mine" : "other");
  }

  applyLock() {
    const ro = this.othersLock;
    this.el.querySelectorAll("[data-write]").forEach((e) => { e.disabled = ro; });
  }

  card(label, items, channel) {
    const rows = h("div", { class: "rows" });
    for (const it of items) {
      if (typeof it !== "string") {
        const [sub, subItems] = Object.entries(it)[0];
        rows.appendChild(h("div", { class: "subhead" }, sub));
        for (const s of subItems) if (typeof s === "string") this.row(rows, s, channel);
        continue;
      }
      this.row(rows, it, channel);
    }
    return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, label)), rows);
  }

  row(rows, name, channel, expanded) {
    const m = this.model;
    const [sec, d] = itemOf(m, name);
    if (!sec) return;
    if (d.per_channel && !channel) {
      for (const ch of Object.keys(m.channels || {})) this.row(rows, name, ch, true);
      return;
    }
    const ch = d.per_channel ? channel : null;
    const key = ch ? ch + "." + name : name;
    const chLabel = ch && expanded ? " (" + ((m.channels[ch] || {}).label || ch) + ")" : "";
    const label = h("div", { class: "rlabel", title: (d.description || "").trim() }, (d.label || name) + chLabel);
    if (sec === "actions") {
      rows.appendChild(h("div", { class: "row" }, label,
        h("div", { class: "rval muted small" }, (d.description || "").trim()),
        h("div", { class: "redit" }, h("button", { "data-write": true, class: d.confirm ? "danger" : "",
          onclick: () => this.action(name, d, ch) }, d.confirm ? "Run…" : "Run"))));
      return;
    }
    if (sec === "readouts") return rows.appendChild(this.readoutRow(key, d, label));
    rows.appendChild(this.controlRow(key, d, ch, label));
  }

  readoutRow(key, d, label) {
    const val = h("div", { class: "rval big" });
    const ageEl = h("span", { class: "age" });
    const numeric = !(d.kind === "text" || d.kind === "enum" || d.kind === "bool");
    const chartBox = numeric ? h("input", { type: "checkbox", onchange: () => {
      if (chartBox.checked) this.chartOn(key); else this.chartOff(key);
      this.app.setChartKey(this.id, key, chartBox.checked);
    } }) : null;
    const cell = {
      ageEl, chartBox,
      update: (e) => {
        val.textContent = valueText(e.value, d);
        ageEl.textContent = age(e.t);
        val.classList.toggle("stale", e.value == null);
      },
    };
    this.cells.set(key, cell);
    cell.update(this.state[key] || {});
    return h("div", { class: "row" }, label, h("div", { class: "rval-wrap" }, val, ageEl),
      h("div", { class: "redit" },
        h("button", { onclick: () => this.read(key), title: "Read it now" }, "Read"),
        chartBox ? h("label", { class: "small chk" }, chartBox, "chart") : ""));
  }

  controlRow(key, d, ch, label) {
    const m = this.model;
    const cur = h("div", { class: "rval" });
    const src = h("span", { class: "badge", hidden: true }, "");
    const edit = h("div", { class: "redit" });
    let update;
    if (d.kind === "bool") {
      const on = h("button", { "data-write": true, class: "seg", onclick: () => this.set(key, "on") }, "ON");
      const off = h("button", { "data-write": true, class: "seg", onclick: () => this.set(key, "off") }, "OFF");
      edit.appendChild(h("div", { class: "segs", role: "group", "aria-label": d.label || key }, on, off));
      update = (e) => {
        on.classList.toggle("sel", e.value === true);
        off.classList.toggle("sel", e.value === false);
        on.setAttribute("aria-pressed", e.value === true);
        off.setAttribute("aria-pressed", e.value === false);
      };
    } else if (d.kind === "enum") {
      const sel = h("select", { "data-write": true, "aria-label": d.label || key, onchange: () => this.set(key, sel.value) },
        h("option", { value: "", disabled: true }, "—"),
        Object.entries(d.options || {}).map(([k, o]) => h("option", { value: k }, k + (o && o.label ? " — " + o.label : ""))));
      edit.appendChild(sel);
      update = (e) => { sel.value = e.value == null ? "" : String(e.value); };
    } else {
      const [lo, hi] = d.kind === "text" ? [null, null] : bounds(m, d, ch, this.limits[key]);
      const inp = h("input", { "data-write": true, type: "text", inputmode: d.kind === "text" ? "text" : "decimal",
        class: d.kind === "text" ? "wide" : "num", "aria-label": (d.label || key) + " new value",
        placeholder: d.kind === "text" ? "" : rangeText(lo, hi, "") });
      const go = async () => {
        if (inp.value.trim() === "") return;
        if (await this.set(key, inp.value.trim())) inp.value = "";
      };
      inp.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") go();
        else if (ev.key === "Escape") inp.value = "";
        else if (d.kind === "number" && (ev.key === "ArrowUp" || ev.key === "ArrowDown")) {
          ev.preventDefault();
          const step = (d.resolution || 1) * (ev.shiftKey ? 10 : 1) * (ev.key === "ArrowUp" ? 1 : -1);
          const base = inp.value.trim() !== "" ? parseFloat(inp.value) : ((this.state[key] || {}).value || 0);
          let v = (isFinite(base) ? base : 0) + step;
          if (lo != null) v = Math.max(lo, v);
          if (hi != null) v = Math.min(hi, v);
          const dd = decimals(d.resolution);
          inp.value = dd != null ? v.toFixed(dd) : String(Number(v.toPrecision(10)));
        }
      });
      edit.append(inp, d.unit ? h("span", { class: "unit" }, d.unit) : "",
        h("button", { "data-write": true, onclick: go }, "Set"));
      if (d.kind !== "text" && (lo != null || hi != null)) edit.title = "Range " + rangeText(lo, hi, d.unit) + ". Enter sets; arrow keys step.";
      update = () => {};
    }
    const cell = {
      update: (e) => {
        cur.textContent = valueText(e.value, d);
        cur.classList.toggle("stale", e.value == null);
        src.hidden = e.source !== "assumed" && e.source !== "inferred";
        src.textContent = e.source;
        src.title = e.source === "inferred"
          ? "This instrument can't report the setting; it is judged from " + ((d.infer || {}).readout || "a reading") + "."
          : "This instrument can't report the setting; this is the last value set.";
        update(e);
      },
    };
    this.cells.set(key, cell);
    cell.update(this.state[key] || {});
    return h("div", { class: "row" }, label, h("div", { class: "rval-wrap" }, cur, src), edit);
  }
}
