// The network analyzer's screen, drawn from its sweep data: one panel per measurement channel
// (each with its own scale, as the analyzer's split display), in the channel's format:
// rectangular (log/linear magnitude, phase, delay, SWR, real, imaginary), Smith chart or polar.
// The type's `display` section names the readouts and settings used. Traces are read by the
// "Live" loop or "Read sweep"; readings fetched by anyone else watching update this view too.

import { h, clear, numText, download, errText } from "./util.js";

const FORMATS = {
  MLOG: { label: "Log mag", unit: "dB" },
  MLIN: { label: "Lin mag", unit: "" },
  PHAS: { label: "Phase", unit: "°" },
  GDEL: { label: "Delay", unit: "s" },
  SWR: { label: "SWR", unit: "" },
  REAL: { label: "Real", unit: "" },
  IMAG: { label: "Imaginary", unit: "" },
  MIMP: { label: "Impedance mag", unit: "\u03a9" },
  SMIT: { label: "Smith chart", unit: "", complex: true },
  POL: { label: "Polar", unit: "", complex: true },
};
const Z0 = 50;

export function hz(v) {
  if (v == null || !isFinite(v)) return "?";
  const a = Math.abs(v);
  const [d, u] = a >= 1e9 ? [1e9, "GHz"] : a >= 1e6 ? [1e6, "MHz"] : a >= 1e3 ? [1e3, "kHz"] : [1, "Hz"];
  return Number((v / d).toPrecision(7)) + " " + u;
}

function valText(v, unit) {
  if (v == null || !isFinite(v)) return "?";
  if (unit === "s") {
    const a = Math.abs(v);
    const [d, u] = a >= 1 || a === 0 ? [1, "s"] : a >= 1e-3 ? [1e-3, "ms"] : a >= 1e-6 ? [1e-6, "µs"] : [1e-9, "ns"];
    return Number((v / d).toPrecision(5)) + " " + u;
  }
  return numText(Number(v.toPrecision(6))) + (unit ? " " + unit : "");
}

export class AnalyzerDisplay {
  constructor(view) {
    this.view = view;
    this.d = view.model.display;
    this.channels = Object.keys(view.model.channels || {}).length ? Object.keys(view.model.channels) : [null];
    this.data = new Map();          // channel -> {complex, values, t, start, stop}
    this.hover = new Map();         // channel -> {x, y} pointer position in its canvas
    this.panels = new Map();        // channel -> {el, canvas, tip, title, mk}
    this.live = !!view.app.prefs.vnaLive;
    this.showTable = false;
    this.status = h("span", { class: "muted small" });
    this.liveBtn = h("button", { class: "small", onclick: () => this.setLive(!this.live),
      title: "Read the sweeps continuously while this page is open (each 1601-point sweep takes about a second on the bus)" }, "Live");
    this.panelBox = h("div", { class: "vna-panels" });
    this.tableBox = h("div", { class: "chart-table vna-table", hidden: true });
    this.tableBtn = h("button", { class: "small", onclick: () => this.toggleTable() }, "Table");
    this.el = h("div", { class: "card vna" },
      h("div", { class: "card-head" }, h("h2", null, "Display"), this.status, h("span", { class: "spacer" }),
        this.liveBtn,
        h("button", { class: "small", onclick: () => this.readOnce(), title: "Read each channel's present sweep once" }, "Read sweep"),
        this.tableBtn,
        h("button", { class: "small", onclick: () => this.csv(), title: "Download the sweep data as CSV" }, "CSV")),
      this.panelBox, this.tableBox);
    this.ro = new ResizeObserver(() => this.redraw());
    this.mq = window.matchMedia("(prefers-color-scheme: dark)");
    this.onTheme = () => this.redraw();
    this.mq.addEventListener("change", this.onTheme);
    this.build();
    this.setLive(this.live);
  }

  destroy() {
    this.dead = true;
    this.live = false;
    this.ro.disconnect();
    this.mq.removeEventListener("change", this.onTheme);
  }

  // -- state helpers ----------------------------------------------------------------------------
  key(name, ch) { return ch ? ch + "." + name : name; }
  val(name, ch) {
    if (!name) return null;
    const e = this.view.state[this.key(name, ch)];
    return e ? e.value : null;
  }
  fmt(ch) { return String(this.val(this.d.format, ch) || "MLOG").toUpperCase(); }
  isComplex(ch) { return !!(FORMATS[this.fmt(ch)] || {}).complex; }
  // Whether a formatted trace holds real, imaginary pairs: on analyzers whose formatted trace is
  // the complex data in Smith and polar formats (display.formatted_complex), two values per point.
  formattedPairs(ch, values) {
    if (!this.d.formatted_complex) return false;
    const pts = Number(this.val(this.d.points, ch));
    return pts > 0 ? values.length === 2 * pts : this.isComplex(ch);
  }
  traceName(ch) { return this.isComplex(ch) && !this.d.formatted_complex ? this.d.complex : this.d.formatted; }
  enabled(ch) { return !this.d.enabled || this.val(this.d.enabled, ch) !== false; }
  slot(ch) { return Math.min(8, this.channels.indexOf(ch) + 1); }
  chLabel(ch) { return ch ? ((this.view.model.channels[ch] || {}).label || ch) : ""; }

  // -- events from the instrument view ----------------------------------------------------------
  onEvent(key, entry) {
    for (const ch of this.channels) {
      for (const [name, complex] of [[this.d.formatted, false], [this.d.complex, true]]) {
        if (name && key === this.key(name, ch) && Array.isArray(entry.value)) {
          this.data.set(ch, { complex: complex || this.formattedPairs(ch, entry.value), values: entry.value, t: entry.t,
            start: this.val(this.d.start, ch), stop: this.val(this.d.stop, ch) });
          this.redraw();
          return;
        }
      }
    }
    // a setting that changes what is drawn (format, scale, channel on/off ...)
    const names = Object.entries(this.d).filter(([k]) => k !== "kind").map(([, v]) => v);
    if (names.some((n) => key === n || key.endsWith("." + n))) {
      if (this.d.enabled && key.endsWith("." + this.d.enabled)) this.build();
      this.redraw();
    }
  }

  // -- reading sweeps ---------------------------------------------------------------------------
  setLive(on) {
    this.live = on;
    this.view.app.prefs.vnaLive = on;
    if (this.view.app.savePrefs) this.view.app.savePrefs();
    this.liveBtn.classList.toggle("on", on);
    this.liveBtn.setAttribute("aria-pressed", on);
    if (on && !this.looping) this.loop();
  }

  async readOnce() {
    for (const ch of this.channels) {
      if (!this.enabled(ch) || this.dead) continue;
      const name = this.traceName(ch);
      const t0 = performance.now();
      try {
        const e = await this.view.client.request("dev.get", { instance: this.view.id, key: this.key(name, ch) });
        this.view.state[this.key(name, ch)] = e;
        this.onEvent(this.key(name, ch), e);
        this.status.textContent = "read " + new Date().toLocaleTimeString() + " · " +
          ((performance.now() - t0) / 1000).toFixed(1) + " s per sweep";
        this.status.classList.remove("bad-text");
      } catch (err) {
        this.status.textContent = this.chLabel(ch) + ": " + errText(err);
        this.status.classList.add("bad-text");
        throw err;
      }
    }
  }

  async loop() {
    this.looping = true;
    try {
      while (this.live && !this.dead) {
        try {
          await this.readOnce();
          await new Promise((r) => setTimeout(r, 250));
        } catch (err) {
          await new Promise((r) => setTimeout(r, 3000));
        }
      }
    } finally { this.looping = false; }
  }

  // -- layout -----------------------------------------------------------------------------------
  build() {
    clear(this.panelBox);
    this.ro.disconnect();
    this.panels.clear();
    for (const ch of this.channels) {
      if (!this.enabled(ch)) continue;
      const canvas = h("canvas", { class: "vna-canvas", role: "img" });
      const tip = h("div", { class: "chart-tip", hidden: true });
      const title = h("span", { class: "chart-title" });
      const mk = h("span", { class: "muted small vna-marker" });
      const el = h("div", { class: "vna-panel" },
        h("div", { class: "chart-head" },
          h("span", { class: "swatch", style: "background: var(--series-" + this.slot(ch) + ")" }), title,
          h("span", { class: "spacer" }), mk),
        h("div", { class: "chart-plot" }, canvas, tip));
      canvas.addEventListener("pointermove", (e) => { this.hover.set(ch, { x: e.offsetX, y: e.offsetY }); this.redraw(); });
      canvas.addEventListener("pointerleave", () => { this.hover.delete(ch); tip.hidden = true; this.redraw(); });
      this.ro.observe(canvas);
      this.panels.set(ch, { el, canvas, tip, title, mk });
      this.panelBox.appendChild(el);
    }
    if (!this.panels.size) this.panelBox.appendChild(h("p", { class: "muted" }, "Every channel is switched off."));
    this.redraw();
  }

  redraw() {
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => {
      this.raf = null;
      for (const [ch, p] of this.panels) this.drawPanel(ch, p);
      this.renderTable();
    });
  }

  // -- drawing ----------------------------------------------------------------------------------
  drawPanel(ch, p) {
    const fmt = this.fmt(ch), F = FORMATS[fmt] || { label: fmt, unit: "" };
    const meas = this.val(this.d.measurement, ch);
    p.title.textContent = [this.chLabel(ch), meas, F.label].filter(Boolean).join("  ·  ");
    p.canvas.setAttribute("aria-label", p.title.textContent + " sweep");
    const mf = this.val(this.d.marker_freq, ch), mv = this.val(this.d.marker_value, ch);
    let mtext = mv != null && !F.complex ? valText(mv, F.unit) : "";
    if (fmt === "SMIT") {           // the analyzer reads the marker as R + jX in Smith format
      const r = this.val(this.d.marker_resistance, ch), x = this.val(this.d.marker_reactance, ch);
      if (r != null && x != null) mtext = valText(r, "") + (x < 0 ? " \u2212 j" : " + j") + valText(Math.abs(x), "\u03a9");
    } else if (fmt === "POL") {     // and as magnitude and phase in polar format
      const m = this.val(this.d.marker_magnitude, ch), ph = this.val(this.d.marker_phase, ch);
      if (m != null && ph != null) mtext = valText(m, "") + " \u2220 " + valText(ph, "\u00b0");
    }
    p.mk.textContent = this.d.marker && this.val(this.d.marker, ch) === false ? "" :
      (mf != null ? "Marker 1: " + hz(mf) + (mtext ? "   " + mtext : "") : "");

    const c = p.canvas, dpr = window.devicePixelRatio || 1;
    const W = c.clientWidth, H = c.clientHeight;
    if (!W || !H) return;
    if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) {
      c.width = Math.round(W * dpr); c.height = Math.round(H * dpr);
    }
    const g = c.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    const css = getComputedStyle(c);
    const col = (n) => css.getPropertyValue(n).trim();
    g.font = "12px " + css.fontFamily;
    const ctx = { g, col, W, H, ch, F, p, series: col("--series-" + this.slot(ch)),
      markerRead: !!(F.complex && mtext) };    // the analyzer's own marker reading, else worked out from the trace

    const data = this.data.get(ch);
    const wantComplex = !!F.complex;
    if (!data || data.complex !== wantComplex) {
      g.fillStyle = col("--text-muted"); g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(this.live ? "reading the sweep…" : "Press “Read sweep”, or “Live”, to show the analyzer's trace", W / 2, H / 2);
      p.tip.hidden = true;
      return;
    }
    const n = wantComplex ? Math.floor(data.values.length / 2) : data.values.length;
    const start = data.start != null ? data.start : this.val(this.d.start, ch);
    const stop = data.stop != null ? data.stop : this.val(this.d.stop, ch);
    const freq = (i) => (n > 1 && start != null && stop != null ? start + (stop - start) * i / (n - 1) : i);
    ctx.n = n; ctx.freq = freq; ctx.data = data;
    ctx.mi = mf != null && start != null && stop != null && stop !== start && this.val(this.d.marker, ch) !== false
      ? Math.max(0, Math.min(n - 1, Math.round((mf - start) / (stop - start) * (n - 1)))) : null;
    if (wantComplex) this.drawCircular(ctx, fmt === "SMIT");
    else this.drawRect(ctx, start, stop);
  }

  drawRect(ctx, start, stop) {
    const { g, col, W, H, ch, F, data, n, p } = ctx;
    const vals = data.values;
    let per = this.val(this.d.scale, ctx.ch), ref = this.val(this.d.ref_level, ch), pos = this.val(this.d.ref_position, ch);
    if (!(per > 0) || ref == null) {           // no scale known: fit the data
      let lo = Infinity, hi = -Infinity;
      for (const v of vals) if (isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      if (hi - lo < 1e-12) { lo -= 1; hi += 1; }
      per = niceStep((hi - lo) / 10); pos = 5;
      ref = Math.round(((hi + lo) / 2) / per) * per;
    }
    if (pos == null) pos = 5;
    const top = ref + (10 - pos) * per, bot = ref - pos * per;
    const labels = [];
    for (let i = 0; i <= 10; i++) labels.push(valText(bot + i * per, ""));
    const left = Math.max(...labels.map((s) => g.measureText(s).width)) + 14, R = W - 10, T = 8, B = H - 24;
    const X = (i) => left + (n > 1 ? i / (n - 1) : 0.5) * (R - left);
    const Y = (v) => B - ((v - bot) / (top - bot)) * (B - T);

    g.lineWidth = 1;
    g.strokeStyle = col("--grid");
    g.fillStyle = col("--text-muted");
    g.textAlign = "right"; g.textBaseline = "middle";
    for (let i = 0; i <= 10; i++) {
      const y = Math.round(B - i * (B - T) / 10) + 0.5;
      g.beginPath(); g.moveTo(left, y); g.lineTo(R, y); g.stroke();
      if (i % 2 === 0 || B - T > 260) g.fillText(labels[i], left - 6, y);
    }
    for (let i = 0; i <= 10; i++) {
      const x = Math.round(left + i * (R - left) / 10) + 0.5;
      g.beginPath(); g.moveTo(x, T); g.lineTo(x, B); g.stroke();
    }
    // reference line and its marker at the left edge
    const yr = Math.round(Y(ref)) + 0.5;
    g.strokeStyle = col("--axis");
    g.beginPath(); g.moveTo(left, yr); g.lineTo(R, yr); g.stroke();
    g.fillStyle = ctx.series;
    g.beginPath(); g.moveTo(left - 1, yr); g.lineTo(left - 7, yr - 4); g.lineTo(left - 7, yr + 4); g.closePath(); g.fill();
    g.fillStyle = col("--text-muted");
    g.textBaseline = "top";
    g.textAlign = "left"; g.fillText(hz(start), left, B + 6);
    g.textAlign = "center"; g.fillText(hz((start + stop) / 2), (left + R) / 2, B + 6);
    g.textAlign = "right"; g.fillText(hz(stop), R, B + 6);
    g.textAlign = "left"; g.textBaseline = "top";
    g.fillText(valText(per, F.unit) + "/div   ref " + valText(ref, F.unit), left + 4, T + 2);

    g.save();
    g.beginPath(); g.rect(left, T, R - left, B - T); g.clip();
    g.strokeStyle = ctx.series; g.lineWidth = 2; g.lineJoin = "round";
    g.beginPath();
    vals.forEach((v, i) => (i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v))));
    g.stroke();
    g.restore();
    if (ctx.mi != null) this.markerAt(ctx, X(ctx.mi), Math.max(T + 10, Math.min(B, Y(vals[ctx.mi]))));

    // hover: crosshair at the nearest point
    const hv = this.hover.get(ch);
    if (hv && hv.x >= left && hv.x <= R && n) {
      const i = Math.max(0, Math.min(n - 1, Math.round((hv.x - left) / (R - left) * (n - 1))));
      const x = Math.round(X(i)) + 0.5;
      g.strokeStyle = col("--axis"); g.lineWidth = 1;
      g.beginPath(); g.moveTo(x, T); g.lineTo(x, B); g.stroke();
      this.dot(ctx, X(i), Math.max(T, Math.min(B, Y(vals[i]))));
      this.tip(ctx, x, T, hz(ctx.freq(i)), [valText(vals[i], F.unit)]);
    } else p.tip.hidden = true;
  }

  drawCircular(ctx, smith) {
    const { g, col, W, H, ch, data, n, p } = ctx;
    const v = data.values;
    const size = Math.min(W - 20, H - 20), r0 = size / 2, cx = W / 2, cy = H / 2;
    const X = (re) => cx + re * r0, Y = (im) => cy - im * r0;
    g.lineWidth = 1;
    g.strokeStyle = col("--grid");
    g.fillStyle = col("--text-muted");
    g.font = "11px " + getComputedStyle(p.canvas).fontFamily;
    g.save();
    g.beginPath(); g.arc(cx, cy, r0, 0, 2 * Math.PI); g.clip();
    if (smith) {
      for (const r of [0.2, 0.5, 1, 2, 5]) {        // constant resistance
        g.beginPath(); g.arc(X(r / (1 + r)), cy, r0 / (1 + r), 0, 2 * Math.PI); g.stroke();
      }
      for (const x of [0.2, 0.5, 1, 2, 5]) {        // constant reactance (arcs)
        for (const s of [1, -1]) { g.beginPath(); g.arc(X(1), Y(s / x), r0 / x, 0, 2 * Math.PI); g.stroke(); }
      }
    } else {
      for (const m of [0.2, 0.4, 0.6, 0.8]) { g.beginPath(); g.arc(cx, cy, m * r0, 0, 2 * Math.PI); g.stroke(); }
      for (let a = 0; a < 180; a += 30) {
        const t = a * Math.PI / 180;
        g.beginPath(); g.moveTo(X(-Math.cos(t)), Y(-Math.sin(t))); g.lineTo(X(Math.cos(t)), Y(Math.sin(t))); g.stroke();
      }
    }
    g.strokeStyle = col("--axis");
    g.beginPath(); g.moveTo(cx - r0, cy); g.lineTo(cx + r0, cy); g.stroke();
    g.restore();
    g.strokeStyle = col("--axis");
    g.beginPath(); g.arc(cx, cy, r0, 0, 2 * Math.PI); g.stroke();
    g.textAlign = "center"; g.textBaseline = "bottom";
    if (smith) for (const r of [0.2, 0.5, 1, 2, 5]) g.fillText(String(r * Z0), X((r - 1) / (r + 1)), cy - 2);
    else for (const m of [0.2, 0.4, 0.6, 0.8, 1]) g.fillText(String(m), X(m), cy - 2);

    g.save();
    g.beginPath(); g.arc(cx, cy, r0 + 1, 0, 2 * Math.PI); g.clip();
    g.strokeStyle = ctx.series; g.lineWidth = 2; g.lineJoin = "round";
    g.beginPath();
    for (let i = 0; i < n; i++) (i ? g.lineTo(X(v[2 * i]), Y(v[2 * i + 1])) : g.moveTo(X(v[0]), Y(v[1])));
    g.stroke();
    g.restore();
    if (ctx.mi != null) this.markerAt(ctx, X(v[2 * ctx.mi]), Y(v[2 * ctx.mi + 1]));
    if (ctx.mi != null && !ctx.markerRead) p.mk.textContent = "Marker 1: " + hz(ctx.freq(ctx.mi)) + "   " + this.gammaText(v[2 * ctx.mi], v[2 * ctx.mi + 1], smith)[0];

    const hv = this.hover.get(ch);
    if (hv && n) {
      let best = 0, bd = Infinity;
      for (let i = 0; i < n; i++) {
        const d = (X(v[2 * i]) - hv.x) ** 2 + (Y(v[2 * i + 1]) - hv.y) ** 2;
        if (d < bd) { bd = d; best = i; }
      }
      if (bd < 40 * 40) {
        const x = X(v[2 * best]), y = Y(v[2 * best + 1]);
        this.dot(ctx, x, y);
        this.tip(ctx, x, y, hz(ctx.freq(best)), this.gammaText(v[2 * best], v[2 * best + 1], smith));
        return;
      }
    }
    p.tip.hidden = true;
  }

  gammaText(re, im, smith) {
    const mag = Math.hypot(re, im), ang = Math.atan2(im, re) * 180 / Math.PI;
    const rl = mag > 0 ? (-20 * Math.log10(mag)).toFixed(2) + " dB return loss" : "";
    if (!smith) return [mag.toFixed(4) + " ∠ " + ang.toFixed(1) + "°", rl];
    const den = (1 - re) ** 2 + im ** 2;
    if (den < 1e-12) return ["open (Z → ∞)", rl];
    const zr = Z0 * (1 - re * re - im * im) / den, zx = Z0 * (2 * im) / den;
    return [numText(Number(zr.toPrecision(4))) + (zx < 0 ? " − j" : " + j") + numText(Number(Math.abs(zx).toPrecision(4))) + " Ω",
      "|Γ| " + mag.toFixed(4) + " ∠ " + ang.toFixed(1) + "°", rl];
  }

  markerAt(ctx, x, y) {
    const { g, col } = ctx;
    g.fillStyle = col("--text-primary");
    g.beginPath(); g.moveTo(x, y - 3); g.lineTo(x - 5, y - 12); g.lineTo(x + 5, y - 12); g.closePath(); g.fill();
    g.fillStyle = col("--text-secondary"); g.textAlign = "center"; g.textBaseline = "bottom";
    g.fillText("1", x, y - 13);
  }

  dot(ctx, x, y) {
    const { g, col } = ctx;
    g.fillStyle = ctx.series; g.strokeStyle = col("--surface-1"); g.lineWidth = 2;
    g.beginPath(); g.arc(x, y, 4, 0, 7); g.fill(); g.stroke();
  }

  tip(ctx, x, y, head, lines) {
    const { p, W } = ctx;
    clear(p.tip).appendChild(h("div", { class: "tip-time" }, head));
    for (const l of lines.filter(Boolean)) p.tip.appendChild(h("div", { class: "tip-row" }, h("strong", null, l)));
    p.tip.hidden = false;
    const tw = p.tip.offsetWidth;
    p.tip.style.left = (x + 12 + tw > W ? x - 12 - tw : x + 12) + "px";
    p.tip.style.top = Math.max(0, y - 10) + "px";
  }

  // -- table and CSV ----------------------------------------------------------------------------
  columns() {
    const cols = [];
    for (const ch of this.channels) {
      const data = this.data.get(ch);
      if (!data || !this.enabled(ch)) continue;
      const n = data.complex ? Math.floor(data.values.length / 2) : data.values.length;
      const s = data.start, e = data.stop;
      const f = (i) => (n > 1 && s != null && e != null ? s + (e - s) * i / (n - 1) : i);
      const F = FORMATS[this.fmt(ch)] || { unit: "" };
      cols.push({ ch, data, n, f, unit: data.complex ? "" : F.unit });
    }
    return cols;
  }

  toggleTable() {
    this.showTable = !this.showTable;
    this.tableBox.hidden = !this.showTable;
    this.tableBtn.classList.toggle("on", this.showTable);
    this.renderTable();
  }

  renderTable() {
    if (!this.showTable) return;
    const cols = this.columns();
    if (!cols.length) { clear(this.tableBox).appendChild(h("p", { class: "muted" }, "No sweep read yet.")); return; }
    const box = clear(this.tableBox);
    for (const c of cols) {
      const head = c.data.complex ? ["Frequency", "Real", "Imaginary"] : ["Frequency", "Value" + (c.unit ? " (" + c.unit + ")" : "")];
      const rows = [];
      for (let i = 0; i < c.n; i++) {
        rows.push(h("tr", null, h("td", { class: "num" }, hz(c.f(i))),
          c.data.complex
            ? [h("td", { class: "num" }, numText(c.data.values[2 * i])), h("td", { class: "num" }, numText(c.data.values[2 * i + 1]))]
            : h("td", { class: "num" }, numText(c.data.values[i]))));
      }
      box.append(h("h3", null, this.chLabel(c.ch) + " · " + c.n + " points"),
        h("table", { class: "grid" }, h("thead", null, h("tr", null, head.map((x) => h("th", { class: "num" }, x)))),
          h("tbody", null, rows)));
    }
  }

  csv() {
    const cols = this.columns();
    if (!cols.length) return;
    const lines = [];
    for (const c of cols) {
      lines.push("# " + this.chLabel(c.ch) + " " + (this.val(this.d.measurement, c.ch) || "") + " " + this.fmt(c.ch) +
        " read " + new Date((c.data.t || Date.now() / 1000) * 1000).toISOString());
      lines.push(c.data.complex ? "frequency_hz,real,imag" : "frequency_hz,value" + (c.unit ? " [" + c.unit + "]" : ""));
      for (let i = 0; i < c.n; i++) {
        lines.push(c.data.complex ? [c.f(i), c.data.values[2 * i], c.data.values[2 * i + 1]].join(",")
          : [c.f(i), c.data.values[i]].join(","));
      }
    }
    download(this.view.id + "-sweep-" + new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-") + ".csv",
      lines.join("\n") + "\n", "text/csv");
  }
}

function niceStep(raw) {
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  return [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag;
}
