// DOM and formatting helpers shared by the views.

// h("div", {class: "x", onclick: fn, title: "t"}, child, "text", [more])
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  add(el, children);
  return el;
}

function add(el, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) add(el, c);
    else el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

export function decimals(res) {
  if (!res || res <= 0) return null;
  return Math.max(0, Math.floor(-Math.log10(res) + 1e-9));
}

export function numText(v, item) {
  const d = decimals(item && item.resolution);
  if (d != null && !Number.isInteger(v)) return v.toFixed(d);
  if (d != null) return v.toFixed(d);
  return Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(7)));
}

// A value for display, with the item's unit, like the CLI's fmt.value.
export function valueText(v, item) {
  item = item || {};
  if (v == null) return "?";
  if (typeof v === "boolean") return v ? "ON" : "OFF";
  if (typeof v === "number") return numText(v, item) + (item.unit ? " " + item.unit : "");
  const opt = item.kind === "enum" && item.options ? item.options[v] : null;
  return String(v) + (opt && opt.label ? " (" + opt.label + ")" : "");
}

export function age(t) {
  if (!t) return "";
  const s = Date.now() / 1000 - t;
  if (s < 2) return "now";
  if (s < 60) return Math.floor(s) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  return Math.floor(s / 3600) + "h ago";
}

export function clock(t) {
  const d = t ? new Date(t * 1000) : new Date();
  return d.toTimeString().slice(0, 8);
}

// [min, max] of a control for a channel, resolving "{channel.vmax}" style bounds.
export function bounds(model, d, channel, limits) {
  const b = (x) => {
    let v = d[x];
    if (typeof v === "string" && v.startsWith("{channel.")) {
      v = ((model.channels || {})[channel] || {})[v.slice(9, -1)];
    }
    return typeof v === "number" ? v : null;
  };
  let lo = b("min"), hi = b("max");
  if (limits) {
    if (lo == null || limits[0] > lo) lo = limits[0];
    if (hi == null || limits[1] < hi) hi = limits[1];
  }
  return [lo, hi];
}

export function rangeText(lo, hi, unit) {
  if (lo == null && hi == null) return "";
  const f = (x) => (x == null ? "" : String(Number(x.toPrecision(6))));
  return f(lo) + " … " + f(hi) + (unit ? " " + unit : "");
}

export function lockText(lock, mySession) {
  if (!lock) return "free";
  if (lock.session === mySession) return "you have control";
  return "controlled by " + lock.holder;
}

export function toast(msg, kind = "info", ms = 4000) {
  const box = document.getElementById("toasts");
  const el = h("div", { class: "toast " + kind, role: kind === "error" ? "alert" : "status" }, msg);
  box.appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? ms * 2 : ms);
}

export function errText(e) {
  if (!e) return "error";
  const err = e.error || {};
  let msg = err.msg || e.message || String(e);
  if (err.errors && err.errors.length) msg += ": " + err.errors.join("; ");
  return msg;
}

export function download(name, text, type = "text/plain") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = h("a", { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// The type's menu tree, or a default one built like the CLI's: what goes on which card.
export function layoutOf(model) {
  if (model.menu && model.menu.length) return model.menu;
  const glob = [], chan = [];
  for (const sec of ["controls", "readouts"]) {
    for (const [n, d] of Object.entries(model[sec] || {})) (d.per_channel ? chan : glob).push(n);
  }
  const tree = [];
  if (glob.length) tree.push({ Settings: glob });
  if (chan.length) tree.push({ "{channel}": chan });
  if (Object.keys(model.actions || {}).length) tree.push({ Actions: Object.keys(model.actions) });
  return tree;
}

export function itemOf(model, name) {
  for (const sec of ["controls", "readouts", "actions"]) {
    const d = (model[sec] || {})[name];
    if (d) return [sec, d];
  }
  return [null, null];
}
