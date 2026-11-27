// Instance editor, device types (browse, YAML, validate, save new versions), raw GPIB console,
// and server information.

import { h, clear, toast, errText, age, clock, download, rangeText, bounds } from "./util.js";

function field(label, input, help) {
  return h("label", { class: "field" }, h("span", { class: "flabel" }, label), input,
    help ? h("span", { class: "fhelp" }, help) : "");
}

function historyList(commits) {
  if (!commits.length) return h("p", { class: "muted" }, "No history (the data directory is not versioned).");
  return h("table", { class: "grid" },
    h("thead", null, h("tr", null, h("th", null, "When"), h("th", null, "Who"), h("th", null, "Change"), h("th", null, "Rev"))),
    h("tbody", null, commits.map((c) => h("tr", null, h("td", null, c.date), h("td", null, c.author),
      h("td", null, c.message), h("td", { class: "mono" }, (c.rev || "").slice(0, 8))))));
}

// -- instance create / edit ------------------------------------------------------------------------
export class InstanceEditor {
  constructor(app, id) {
    this.app = app;
    this.id = id;          // null: create
    this.el = h("section", { class: "view" }, h("p", { class: "muted" }, "loading…"));
    this.load();
  }

  destroy() {}

  async load() {
    const c = this.app.client;
    try {
      this.types = (await c.request("types.list")).types;
      if (this.id) {
        const r = await c.request("instances.get", { instance: this.id });
        this.inst = r.instance;
        this.history = (await c.request("instances.history", { instance: this.id, limit: 20 })).commits;
      } else {
        this.inst = { id: "", name: "", type: "", address: { board: 0, pad: 1, sad: 0 } };
      }
      this.render();
    } catch (e) {
      clear(this.el).appendChild(h("div", { class: "card error" }, h("p", null, errText(e))));
    }
  }

  render() {
    const inst = this.inst, ov = inst.overrides || {}, ss = inst.safe_state || {};
    const creating = !this.id;
    const [tname, tver] = (inst.type || "").split("@");
    const typeSel = h("select", { required: true },
      creating ? h("option", { value: "" }, "choose a type…") : "",
      this.types.map((t) => h("option", { value: t.type, selected: t.type === tname }, t.type + " — " + t.title)));
    const verSel = h("select");
    const fillVers = () => {
      const t = this.types.find((x) => x.type === typeSel.value);
      clear(verSel);
      for (const v of (t ? t.versions : []).slice().reverse()) {
        verSel.appendChild(h("option", { value: v, selected: v === tver }, v + (t && v === t.latest ? " (latest)" : "")));
      }
    };
    typeSel.addEventListener("change", fillVers);
    fillVers();
    const f = {
      id: h("input", { type: "text", value: inst.id, required: true, pattern: "[a-z0-9][a-z0-9._-]*", disabled: !creating,
        placeholder: "bench-psu" }),
      name: h("input", { type: "text", value: inst.name || "", placeholder: "Bench PSU" }),
      board: h("input", { type: "number", min: 0, max: 15, value: inst.address.board || 0, class: "num" }),
      pad: h("input", { type: "number", min: 0, max: 30, value: inst.address.pad, class: "num" }),
      sad: h("input", { type: "number", min: 0, max: 126, value: inst.address.sad || 0, class: "num" }),
      poll_mode: h("select", null, [["subscribed", "while someone is watching"], ["always", "always"], ["off", "never"]]
        .map(([v, l]) => h("option", { value: v, selected: (inst.poll_mode || "subscribed") === v }, l))),
      disc: h("select", null, [["leave", "leave outputs as they are"], ["outputs_off", "switch outputs off"]]
        .map(([v, l]) => h("option", { value: v, selected: (ss.on_client_disconnect || "leave") === v }, l))),
      stop: h("select", null, [["leave", "leave outputs as they are"], ["outputs_off", "switch outputs off"]]
        .map(([v, l]) => h("option", { value: v, selected: (ss.on_server_stop || "leave") === v }, l))),
      limits: h("textarea", { rows: 3, class: "mono", placeholder: "e.g. P6V.voltage = 0 : 5.5" },
        Object.entries(ov.limits || {}).map(([k, [lo, hi]]) => k + " = " + lo + " : " + hi).join("\n")),
      poll: h("textarea", { rows: 2, class: "mono", placeholder: "e.g. v_meas = 1s" },
        Object.entries(ov.poll || {}).map(([k, v]) => k + " = " + v).join("\n")),
      notes: h("textarea", { rows: 3 }, inst.notes || ""),
      verify: h("input", { type: "checkbox", checked: true }),
    };
    const form = h("form", { class: "card form", onsubmit: (e) => { e.preventDefault(); this.save(f, typeSel, verSel); } },
      h("h2", null, creating ? "Add an instrument" : "Edit " + this.id + " (revision " + inst.revision + ")"),
      h("div", { class: "fgrid" },
        field("ID", f.id, creating ? "lower case, digits, . _ -; used in commands" : "can't be changed"),
        field("Name", f.name),
        field("Type", typeSel), field("Version", verSel, "a newer type version only applies when you choose it"),
        field("GPIB board", f.board), field("Primary address", f.pad), field("Secondary address", f.sad, "0 = none"),
        field("Poll readouts", f.poll_mode),
        field("When the controlling client disconnects", f.disc),
        field("When the server stops", f.stop),
        field("Narrower limits", f.limits, "one per line: KEY = MIN : MAX (within the type's range)"),
        field("Poll intervals", f.poll, "one per line: READOUT = interval (e.g. 2s, 1min) or off"),
        field("Notes", f.notes),
        creating ? field("Check identity", h("span", null, f.verify, " send the type's identity query after creating")) : ""),
      h("div", { class: "btns" },
        h("button", { class: "primary", type: "submit" }, creating ? "Create" : "Save"),
        h("a", { class: "button", href: creating ? "#/" : "#/i/" + encodeURIComponent(this.id) }, "Cancel"),
        creating ? "" : h("span", { class: "spacer" }),
        creating ? "" : h("button", { type: "button", class: "danger", onclick: () => this.remove() }, "Delete instance…")));
    clear(this.el).append(form);
    if (!creating) this.el.appendChild(h("div", { class: "card" }, h("h2", null, "History"), historyList(this.history)));
  }

  parse(f, typeSel, verSel) {
    const limits = {}, poll = {};
    for (const line of f.limits.value.split("\n").map((s) => s.trim()).filter(Boolean)) {
      const m = line.match(/^([\w.]+)\s*=\s*(-?[\d.eE+-]+)\s*:\s*(-?[\d.eE+-]+)$/);
      if (!m) throw new Error("limit line not understood: " + line);
      limits[m[1]] = [parseFloat(m[2]), parseFloat(m[3])];
    }
    for (const line of f.poll.value.split("\n").map((s) => s.trim()).filter(Boolean)) {
      const m = line.match(/^(\w+)\s*=\s*(\S+)$/);
      if (!m) throw new Error("poll line not understood: " + line);
      poll[m[1]] = m[2];
    }
    if (!typeSel.value) throw new Error("choose a type");
    const overrides = Object.assign({}, this.inst.overrides || {});
    delete overrides.limits; delete overrides.poll;
    if (Object.keys(limits).length) overrides.limits = limits;
    if (Object.keys(poll).length) overrides.poll = poll;
    const doc = {
      name: f.name.value.trim(),
      type: typeSel.value + "@" + verSel.value,
      address: { board: +f.board.value, pad: +f.pad.value, sad: +f.sad.value },
      poll_mode: f.poll_mode.value,
      safe_state: { on_client_disconnect: f.disc.value, on_server_stop: f.stop.value },
      notes: f.notes.value.trim(),
    };
    if (Object.keys(overrides).length || this.inst.overrides) doc.overrides = overrides;
    if (!doc.notes && !this.inst.notes) delete doc.notes;
    if (!doc.name && !this.inst.name) delete doc.name;
    // Don't write defaults into a document that didn't have them.
    if (!this.inst.poll_mode && doc.poll_mode === "subscribed") delete doc.poll_mode;
    if (!this.inst.safe_state && doc.safe_state.on_client_disconnect === "leave" && doc.safe_state.on_server_stop === "leave") delete doc.safe_state;
    return doc;
  }

  async save(f, typeSel, verSel) {
    let doc;
    try { doc = this.parse(f, typeSel, verSel); } catch (e) { toast(e.message, "error"); return; }
    const c = this.app.client;
    try {
      if (!this.id) {
        doc.id = f.id.value.trim();
        const r = await c.request("instances.create", { instance: doc, verify: f.verify.checked });
        const idt = r.identify;
        if (idt && idt.error) toast("Created, but the instrument didn't answer " + idt.probe + ": " + idt.error, "error", 8000);
        else if (idt) toast("Created. " + idt.probe + " → " + idt.response + (idt.match ? "" : "  (does NOT match the type)"), idt.match ? "ok" : "error", 8000);
        else toast("Created " + doc.id, "ok");
        location.hash = "#/i/" + encodeURIComponent(doc.id);
      } else {
        const changes = {};
        for (const [k, v] of Object.entries(doc)) if (JSON.stringify(this.inst[k]) !== JSON.stringify(v)) changes[k] = v;
        if (!Object.keys(changes).length) { toast("Nothing changed"); return; }
        const r = await c.request("instances.update", { instance: this.id, changes, revision: this.inst.revision });
        toast("Saved " + this.id + " (revision " + r.instance.revision + "): " + Object.keys(changes).join(", "), "ok");
        location.hash = "#/i/" + encodeURIComponent(this.id);
      }
    } catch (e) { toast(errText(e), "error", 8000); }
  }

  async remove() {
    if (!window.confirm("Delete the instance " + this.id + "?\nThe instrument itself is not touched, and the history is kept.")) return;
    try {
      await this.app.client.request("instances.delete", { instance: this.id });
      toast("Deleted " + this.id, "ok");
      location.hash = "#/";
    } catch (e) { toast(errText(e), "error"); }
  }
}

// -- types -------------------------------------------------------------------------------------------
export class TypesView {
  constructor(app, ref) {
    this.app = app;
    this.ref = ref;
    this.el = h("section", { class: "view" }, h("p", { class: "muted" }, "loading…"));
    this.load();
  }

  destroy() {}

  async load() {
    try {
      if (this.ref === "new") return this.renderEditor(null);
      if (this.ref) return this.renderType();
      const types = (await this.app.client.request("types.list")).types;
      clear(this.el).append(h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", null, "Device types"), h("span", { class: "spacer" }),
          h("a", { class: "button", href: "#/types/new" }, "New type…")),
        h("p", { class: "muted small" }, "A type describes an instrument's controls, readouts and actions. Published versions never change; edits are saved as a new version."),
        h("table", { class: "grid" },
          h("thead", null, h("tr", null, ["Type", "Title", "Latest", "Versions", ""].map((x) => h("th", null, x)))),
          h("tbody", null, types.map((t) => h("tr", null,
            h("td", null, h("a", { href: "#/types/" + encodeURIComponent(t.type) }, t.type)),
            h("td", null, t.title),
            h("td", null, t.latest),
            h("td", null, t.versions.join(", ")),
            h("td", null, t.builtin ? h("span", { class: "badge" }, "built-in") : h("span", { class: "badge" }, "added"),
              t.verified ? "" : h("span", { class: "badge warn" }, "unverified"))))))));
    } catch (e) {
      clear(this.el).appendChild(h("div", { class: "card error" }, h("p", null, errText(e))));
    }
  }

  async renderType() {
    const c = this.app.client;
    const r = await c.request("types.get", { type: this.ref, format: "both" });
    const m = r.model;
    const hist = (await c.request("types.history", { type: m.type, limit: 20 }).catch(() => ({ commits: [] }))).commits;
    const list = (await c.request("types.list")).types.find((t) => t.type === m.type);
    const chans = Object.entries(m.channels || {});
    const ch0 = chans.length ? chans[0][0] : null;
    const rowsOf = (sec) => Object.entries(m[sec] || {}).map(([n, d]) => h("tr", null,
      h("td", { class: "mono" }, n), h("td", null, d.label || ""), h("td", null, d.kind || (sec === "readouts" ? "number" : "")),
      h("td", null, d.per_channel ? "per channel" : ""),
      h("td", null, sec === "controls" ? (d.kind === "enum" ? Object.keys(d.options || {}).join(", ")
        : rangeText(...bounds(m, d, ch0), d.unit) + (d.per_channel && chans.length > 1 ? " (" + (m.channels[ch0].label || ch0) + ")" : ""))
        : sec === "readouts" ? (d.unit || "") + (d.poll ? "  polled " + d.poll : "") : (d.confirm ? "asks first" : "")),
      h("td", { class: "muted" }, (d.description || "").trim())));
    const table = (title, sec) => Object.keys(m[sec] || {}).length ? [h("h3", null, title), h("table", { class: "grid" },
      h("thead", null, h("tr", null, ["Name", "Label", "Kind", "", "Range / unit", "Description"].map((x) => h("th", null, x)))),
      h("tbody", null, rowsOf(sec)))] : "";
    clear(this.el).append(
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", null, m.title || m.type), h("span", { class: "spacer" }),
          list && list.versions.length > 1 ? h("select", { onchange: (e) => { location.hash = "#/types/" + encodeURIComponent(m.type + "@" + e.target.value); } },
            list.versions.slice().reverse().map((v) => h("option", { value: v, selected: v === m.version }, "version " + v))) : "",
          h("a", { class: "button", href: "#/types/new?from=" + encodeURIComponent(m.ref) }, "Edit as new version…"),
          h("button", { onclick: () => download(m.type + "-" + m.version + ".yaml", r.yaml) }, "Download YAML")),
        h("p", { class: "muted" }, m.ref, " · ", m.manufacturer || "", " ", m.model || "", " · ",
          m.builtin ? "built-in" : "added", " · ", m.verified ? "verified on an instrument" : "not verified on an instrument"),
        m.description ? h("p", null, m.description.trim()) : "",
        chans.length ? [h("h3", null, "Channels"), h("p", null, chans.map(([k, d]) => (d.label || k) + " (" + k + ")").join(", "))] : "",
        table("Controls", "controls"), table("Readouts", "readouts"), table("Actions", "actions")),
      h("div", { class: "card" }, h("h2", null, "YAML"), h("pre", { class: "yaml" }, r.yaml)),
      h("div", { class: "card" }, h("h2", null, "History"), historyList(hist)));
  }

  async renderEditor() {
    const from = new URLSearchParams(location.hash.split("?")[1] || "").get("from") || "generic.scpi";
    const r = await this.app.client.request("types.get", { type: from, format: "yaml" });
    const text = h("textarea", { class: "yaml-edit mono", spellcheck: "false", rows: 30 }, r.yaml);
    const msgs = h("div", { class: "msgs" });
    const message = h("input", { type: "text", placeholder: "what changed (saved in the history)", class: "wide" });
    const validate = async () => {
      try {
        const v = await this.app.client.request("types.validate", { yaml: text.value });
        clear(msgs).append(v.valid
          ? h("p", { class: "ok" }, "Valid: " + v.ref + (v.exists ? " — this version already exists; change `version:` to save a new one" : " — ready to save"))
          : h("div", { class: "error" }, h("p", null, "Not valid:"), h("ul", null, v.errors.map((e) => h("li", null, e)))));
        return v;
      } catch (e) { toast(errText(e), "error"); return null; }
    };
    const save = async () => {
      const v = await validate();
      if (!v || !v.valid) return;
      try {
        const p = await this.app.client.request("types.put", { yaml: text.value, message: message.value.trim() || undefined });
        toast(p.added ? "Saved " + p.ref : p.ref + " is unchanged", "ok");
        this.app.forgetModels();
        location.hash = "#/types/" + encodeURIComponent(p.ref);
      } catch (e) {
        clear(msgs).append(h("div", { class: "error" }, h("p", null, errText(e))));
      }
    };
    clear(this.el).append(h("div", { class: "card" },
      h("h2", null, "New type or version"),
      h("p", { class: "muted small" }, "Starting from ", h("span", { class: "mono" }, r.ref),
        ". Change `type:` for a new type, or keep it and raise `version:` for a new version of this one. ",
        "See docs/device-type-format.md for every field."),
      text, msgs,
      h("div", { class: "btns" }, message,
        h("button", { onclick: validate }, "Validate"),
        h("button", { class: "primary", onclick: save }, "Save"),
        h("a", { class: "button", href: "#/types" }, "Cancel"))));
  }
}

// -- raw console ----------------------------------------------------------------------------------
export class RawView {
  constructor(app) {
    this.app = app;
    const p = app.prefs.raw || (app.prefs.raw = { board: 0, pad: 5, timeout: 3 });
    this.board = h("input", { type: "number", min: 0, max: 15, value: p.board, class: "num" });
    this.pad = h("input", { type: "number", min: 0, max: 30, value: p.pad, class: "num" });
    this.timeout = h("input", { type: "number", min: 0.1, max: 1000, step: "any", value: p.timeout, class: "num" });
    this.cmd = h("input", { type: "text", class: "wide mono", placeholder: "*IDN?", "aria-label": "command",
      onkeydown: (e) => this.key(e) });
    this.log = h("div", { class: "rawlog mono", "aria-live": "polite" });
    this.hist = app.prefs.rawHistory || (app.prefs.rawHistory = []);
    this.hpos = this.hist.length;
    const op = (name, label, title) => h("button", { title, onclick: () => this.run(name) }, label);
    this.el = h("section", { class: "view" }, h("div", { class: "card" },
      h("h2", null, "Raw GPIB"),
      h("p", { class: "muted small" }, "Commands go straight to the address. Refused while someone else controls an instrument at that address. Enter queries when the command ends in ?, otherwise writes."),
      h("div", { class: "btns" }, field("Board", this.board), field("Address", this.pad), field("Timeout (s)", this.timeout),
        h("span", { class: "spacer" }), op("listeners", "Find listeners", "Which addresses have a device (no commands sent)")),
      h("div", { class: "btns" }, this.cmd,
        op("query", "Query", "Write, then read the answer"), op("write", "Write"), op("read", "Read")),
      h("div", { class: "btns" }, op("poll", "Serial poll"), op("clear", "Device clear"), op("local", "Go to local"),
        op("trigger", "Trigger"), h("span", { class: "spacer" }),
        h("button", { onclick: () => clear(this.log) }, "Clear log")),
      this.log));
  }

  destroy() {}

  key(e) {
    if (e.key === "Enter") this.run(/\?\s*$/.test(this.cmd.value) ? "query" : "write");
    else if (e.key === "ArrowUp" && this.hpos > 0) { this.cmd.value = this.hist[--this.hpos]; e.preventDefault(); }
    else if (e.key === "ArrowDown") { this.hpos = Math.min(this.hist.length, this.hpos + 1); this.cmd.value = this.hist[this.hpos] || ""; e.preventDefault(); }
  }

  line(dir, text, cls) {
    this.log.appendChild(h("div", { class: "rawline " + (cls || "") }, h("span", { class: "muted" }, clock() + " "), dir + " ", text));
    this.log.scrollTop = this.log.scrollHeight;
  }

  async run(op) {
    const board = +this.board.value, pad = +this.pad.value, timeout = +this.timeout.value;
    Object.assign(this.app.prefs.raw, { board, pad, timeout });
    const c = this.app.client, data = this.cmd.value;
    const at = board + ":" + pad;
    try {
      if (op === "listeners") {
        const r = await c.request("bus.listeners", { board });
        this.line("board " + board, "listeners: " + (r.pads.join(", ") || "none"));
        return;
      }
      if ((op === "query" || op === "write") && !data.trim()) { this.cmd.focus(); return; }
      if (op === "query" || op === "write") {
        if (this.hist[this.hist.length - 1] !== data) this.hist.push(data);
        if (this.hist.length > 100) this.hist.shift();
        this.hpos = this.hist.length;
      }
      if (op === "query") {
        this.line(at + " →", data);
        const r = await c.request("raw.query", { board, pad, data, timeout });
        this.line(at + " ←", JSON.stringify(r.data).slice(1, -1), "in");
      } else if (op === "write") {
        this.line(at + " →", data);
        await c.request("raw.write", { board, pad, data, timeout });
      } else if (op === "read") {
        const r = await c.request("raw.read", { board, pad, timeout });
        this.line(at + " ←", JSON.stringify(r.data).slice(1, -1), "in");
      } else if (op === "poll") {
        const r = await c.request("bus.serial_poll", { board, pad });
        this.line(at, "status byte " + r.status + " (0x" + r.status.toString(16).padStart(2, "0") + ", bits " +
          [7, 6, 5, 4, 3, 2, 1, 0].map((b) => (r.status >> b) & 1).join("") + ")");
      } else {
        await c.request("bus." + op, { board, pad });
        this.line(at, op + " sent");
      }
      if (op === "write" || op === "query") this.cmd.select();
    } catch (e) {
      this.line(at, errText(e), "err");
    }
  }
}

// -- server ------------------------------------------------------------------------------------------
export class ServerView {
  constructor(app) {
    this.app = app;
    this.el = h("section", { class: "view" }, h("p", { class: "muted" }, "loading…"));
    this.off = app.client.on((ev) => { if (ev.event === "lock") this.load(); });
    this.timer = setInterval(() => this.load(), 5000);
    this.load();
  }

  destroy() { this.off(); clearInterval(this.timer); }

  async load() {
    const c = this.app.client;
    try {
      const [info, locks] = await Promise.all([c.request("server.info"), c.request("dev.locks")]);
      const up = info.uptime;
      const upText = up < 3600 ? Math.floor(up / 60) + " min" : up < 86400 ? (up / 3600).toFixed(1) + " h" : (up / 86400).toFixed(1) + " days";
      clear(this.el).append(
        h("div", { class: "card" }, h("h2", null, "Server"),
          h("p", null, "gpibremote " + info.version + ", protocol " + info.protocol + ", up " + upText + ". You are session #" + c.session + "."),
          h("p", null, "Boards: " + info.boards.map((b) => b.board + " (" + b.backend + ")").join(", ")),
          h("p", { class: "muted small" }, "Plain TCP and HTTP without TLS: use on a LAN or over a VPN only.")),
        h("div", { class: "card" }, h("h2", null, "Connected clients"),
          h("table", { class: "grid" },
            h("thead", null, h("tr", null, ["#", "Who", "Client", "Via", "Address"].map((x) => h("th", null, x)))),
            h("tbody", null, (info.sessions || []).map((s) => h("tr", { class: s.id === c.session ? "me" : "" },
              h("td", null, s.id), h("td", null, s.who), h("td", null, s.client || ""), h("td", null, s.transport || ""), h("td", { class: "mono" }, s.peer)))))),
        h("div", { class: "card" }, h("h2", null, "Control locks"),
          Object.keys(locks.locks).length ? h("table", { class: "grid" },
            h("thead", null, h("tr", null, ["Instrument", "Held by", "Since", "Idle"].map((x) => h("th", null, x)))),
            h("tbody", null, Object.entries(locks.locks).map(([i, l]) => h("tr", null,
              h("td", null, h("a", { href: "#/i/" + encodeURIComponent(i) }, i)), h("td", null, l.holder),
              h("td", null, age(l.since)), h("td", null, Math.round(l.idle) + " s")))))
            : h("p", { class: "muted" }, "Nobody holds a lock.")));
    } catch (e) {
      clear(this.el).appendChild(h("div", { class: "card error" }, h("p", null, errText(e))));
    }
  }
}
