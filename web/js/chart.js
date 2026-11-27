// Live time chart for readouts of one unit (one y-axis per chart: volts and amps never share).
// Canvas lines, 2px; series colours follow the entity (channel order in the type), never rank;
// legend always for 2+ series, direct labels at the line ends, crosshair tooltip, table view
// and CSV export.

import { h, clear, clock, numText, download } from "./util.js";

const MAX_POINTS = 20000;

export class TimeChart {
  constructor({ unit, title, windowSec }) {
    this.unit = unit || "";
    this.windowSec = windowSec || 300;
    this.series = new Map();   // key -> {label, slot, points: [[t, v]]}
    this.hoverX = null;
    this.showTable = false;
    this.canvas = h("canvas", { class: "chart-canvas", role: "img", "aria-label": title });
    this.tip = h("div", { class: "chart-tip", hidden: true });
    this.legend = h("div", { class: "chart-legend" });
    this.tableBox = h("div", { class: "chart-table", hidden: true });
    this.tableBtn = h("button", { class: "small", onclick: () => this.toggleTable() }, "Table");
    this.el = h("div", { class: "chart" },
      h("div", { class: "chart-head" },
        h("span", { class: "chart-title" }, title),
        this.legend,
        h("span", { class: "spacer" }),
        this.tableBtn,
        h("button", { class: "small", onclick: () => this.csv(), title: "Download the points as CSV" }, "CSV")),
      h("div", { class: "chart-plot" }, this.canvas, this.tip),
      this.tableBox);
    this.canvas.addEventListener("pointermove", (e) => { this.hoverX = e.offsetX; this.redraw(); });
    this.canvas.addEventListener("pointerleave", () => { this.hoverX = null; this.tip.hidden = true; this.redraw(); });
    this.ro = new ResizeObserver(() => this.redraw());
    this.ro.observe(this.canvas);
    this.mq = window.matchMedia("(prefers-color-scheme: dark)");
    this.onTheme = () => this.redraw();
    this.mq.addEventListener("change", this.onTheme);
  }

  destroy() { this.ro.disconnect(); this.mq.removeEventListener("change", this.onTheme); }

  addSeries(key, label, slot, points) {
    if (!this.series.has(key)) this.series.set(key, { label, slot, points: points || [] });
    this.renderLegend();
  }

  removeSeries(key) { this.series.delete(key); this.renderLegend(); this.redraw(); }

  push(key, t, v) {
    const s = this.series.get(key);
    if (!s || typeof v !== "number" || !isFinite(v)) return;
    s.points.push([t, v]);
    if (s.points.length > MAX_POINTS) s.points.splice(0, s.points.length - MAX_POINTS);
    this.redraw();
  }

  setWindow(sec) { this.windowSec = sec; this.redraw(); }

  renderLegend() {
    clear(this.legend);
    if (this.series.size < 2) return;
    for (const s of this.series.values()) {
      this.legend.appendChild(h("span", { class: "key" },
        h("span", { class: "swatch", style: "background: var(--series-" + s.slot + ")" }), s.label));
    }
  }

  toggleTable() {
    this.showTable = !this.showTable;
    this.tableBox.hidden = !this.showTable;
    this.tableBtn.classList.toggle("on", this.showTable);
    this.renderTable();
  }

  rows() {
    const times = new Set();
    for (const s of this.series.values()) for (const p of s.points) times.add(p[0]);
    const sorted = [...times].sort((a, b) => a - b);
    const maps = [...this.series.values()].map((s) => new Map(s.points));
    return sorted.map((t) => [t, ...maps.map((m) => (m.has(t) ? m.get(t) : null))]);
  }

  renderTable() {
    if (!this.showTable) return;
    const rows = this.rows().slice(-50).reverse();
    clear(this.tableBox).appendChild(h("table", { class: "grid" },
      h("thead", null, h("tr", null, h("th", null, "Time"),
        [...this.series.values()].map((s) => h("th", { class: "num" }, s.label + (this.unit ? " (" + this.unit + ")" : ""))))),
      h("tbody", null, rows.map((r) => h("tr", null, h("td", null, clock(r[0])),
        r.slice(1).map((v) => h("td", { class: "num" }, v == null ? "" : numText(v))))))));
  }

  csv() {
    const head = ["time_iso", "time_unix", ...[...this.series.keys()].map((k) => k + (this.unit ? " [" + this.unit + "]" : ""))];
    const lines = [head.join(",")];
    for (const r of this.rows()) {
      lines.push([new Date(r[0] * 1000).toISOString(), r[0].toFixed(3), ...r.slice(1).map((v) => (v == null ? "" : v))].join(","));
    }
    download("readings-" + new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-") + ".csv", lines.join("\n") + "\n", "text/csv");
  }

  redraw() {
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => { this.raf = null; this.draw(); this.renderTable(); });
  }

  draw() {
    const c = this.canvas, dpr = window.devicePixelRatio || 1;
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
    const font = "12px " + css.fontFamily;
    g.font = font;

    const now = Date.now() / 1000;
    const t1 = now, t0 = now - this.windowSec;
    let lo = Infinity, hi = -Infinity;
    const vis = [];
    for (const [key, s] of this.series) {
      const pts = s.points.filter((p) => p[0] >= t0 - 5);
      for (const p of pts) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
      vis.push({ key, s, pts });
    }
    if (lo === Infinity) {
      g.fillStyle = col("--text-muted");
      g.textAlign = "center";
      g.fillText("waiting for readings…", W / 2, H / 2);
      return;
    }
    if (hi - lo < 1e-12) { const pad = Math.max(Math.abs(hi) * 0.05, 1e-3); lo -= pad; hi += pad; }
    const ticks = niceTicks(lo, hi, 5);
    lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
    const yl = ticks.map((v) => fmtTick(v, ticks));
    const left = Math.max(...yl.map((s) => g.measureText(s).width)) + 12;
    const labelW = vis.length <= 4 ? Math.max(...vis.map((v) => g.measureText(v.s.label).width)) + 14 : 8;
    const R = W - labelW, T = 10, B = H - 22;
    const X = (t) => left + ((t - t0) / (t1 - t0)) * (R - left);
    const Y = (v) => B - ((v - lo) / (hi - lo)) * (B - T);

    // grid and axes: recessive
    g.strokeStyle = col("--grid");
    g.lineWidth = 1;
    g.fillStyle = col("--text-muted");
    g.textAlign = "right";
    g.textBaseline = "middle";
    ticks.forEach((v, i) => {
      const y = Math.round(Y(v)) + 0.5;
      g.beginPath(); g.moveTo(left, y); g.lineTo(R, y); g.stroke();
      g.fillText(yl[i], left - 6, y);
    });
    g.textAlign = "center";
    g.textBaseline = "top";
    const step = timeStep(this.windowSec);
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
      g.fillText(clock(t).slice(step >= 60 ? 0 : 0, step >= 60 ? 5 : 8), X(t), B + 6);
    }
    g.strokeStyle = col("--axis");
    g.beginPath(); g.moveTo(left, B + 0.5); g.lineTo(R, B + 0.5); g.stroke();

    // lines
    g.save();
    g.beginPath(); g.rect(left, T - 2, R - left, B - T + 4); g.clip();
    g.lineWidth = 2; g.lineJoin = "round"; g.lineCap = "round";
    for (const v of vis) {
      g.strokeStyle = col("--series-" + v.s.slot);
      g.beginPath();
      v.pts.forEach((p, i) => (i ? g.lineTo(X(p[0]), Y(p[1])) : g.moveTo(X(p[0]), Y(p[1]))));
      g.stroke();
      if (v.pts.length === 1) { g.fillStyle = g.strokeStyle; g.beginPath(); g.arc(X(v.pts[0][0]), Y(v.pts[0][1]), 4, 0, 7); g.fill(); }
    }
    g.restore();

    // direct labels at the line ends (text in text colour, keyed by a short stroke)
    if (vis.length <= 4) {
      const ends = vis.filter((v) => v.pts.length).map((v) => ({ v, y: Y(v.pts[v.pts.length - 1][1]) }))
        .sort((a, b) => a.y - b.y);
      for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 14) ends[i].y = ends[i - 1].y + 14;
      g.textAlign = "left"; g.textBaseline = "middle";
      for (const e of ends) {
        g.strokeStyle = col("--series-" + e.v.s.slot); g.lineWidth = 2;
        g.beginPath(); g.moveTo(R + 2, e.y); g.lineTo(R + 8, e.y); g.stroke();
        g.fillStyle = col("--text-secondary");
        if (vis.length > 1) g.fillText(e.v.s.label, R + 11, e.y);
      }
    }

    // crosshair + tooltip: every series at the nearest time
    if (this.hoverX != null && this.hoverX >= left && this.hoverX <= R) {
      const tx = t0 + ((this.hoverX - left) / (R - left)) * (t1 - t0);
      let best = null;
      for (const v of vis) for (const p of v.pts) if (best == null || Math.abs(p[0] - tx) < Math.abs(best - tx)) best = p[0];
      if (best != null) {
        const x = Math.round(X(best)) + 0.5;
        g.strokeStyle = col("--axis"); g.lineWidth = 1;
        g.beginPath(); g.moveTo(x, T); g.lineTo(x, B); g.stroke();
        clear(this.tip).appendChild(h("div", { class: "tip-time" }, clock(best)));
        for (const v of vis) {
          let near = null;
          for (const p of v.pts) if (near == null || Math.abs(p[0] - best) < Math.abs(near[0] - best)) near = p;
          if (!near) continue;
          g.fillStyle = col("--series-" + v.s.slot); g.strokeStyle = col("--surface-1"); g.lineWidth = 2;
          g.beginPath(); g.arc(X(near[0]), Y(near[1]), 4, 0, 7); g.fill(); g.stroke();
          this.tip.appendChild(h("div", { class: "tip-row" },
            h("span", { class: "tip-key", style: "background: var(--series-" + v.s.slot + ")" }),
            h("strong", null, numText(near[1]) + (this.unit ? " " + this.unit : "")), " ", v.s.label));
        }
        this.tip.hidden = false;
        const tw = this.tip.offsetWidth;
        this.tip.style.left = (x + 12 + tw > W ? x - 12 - tw : x + 12) + "px";
        this.tip.style.top = T + "px";
      }
    }
  }
}

function niceTicks(lo, hi, n) {
  const span = hi - lo;
  const raw = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || 10 * mag;
  const out = [];
  for (let v = Math.floor(lo / step) * step; v <= hi + step * 0.5; v += step) out.push(Number(v.toPrecision(12)));
  if (out[out.length - 1] < hi) out.push(Number((out[out.length - 1] + step).toPrecision(12)));
  return out;
}

function fmtTick(v, ticks) {
  const step = Math.abs(ticks[1] - ticks[0]) || 1;
  const d = Math.max(0, -Math.floor(Math.log10(step) + 1e-9) + (step / Math.pow(10, Math.floor(Math.log10(step))) % 1 ? 1 : 0));
  return v.toFixed(Math.min(d, 9));
}

function timeStep(win) {
  for (const s of [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400]) if (win / s <= 6) return s;
  return 21600;
}
