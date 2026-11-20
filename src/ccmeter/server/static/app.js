// ccmeter dashboard. Preact + htm, no build step, nothing loaded from other origins.
// Data: /api/dashboard/overview (per-day numbers), /session-list and /session-detail/{id}.
import { Component, h, render } from "./vendor/preact.module.js";
import htm from "./vendor/htm.module.js";

const html = htm.bind(h);

// Series colors of the models, in a fixed order validated for this surface (OKLCH lightness
// band, chroma floor, color-vision-deficiency separation of neighbors, contrast). A model keeps
// its color whatever the filter; past the eighth model the rest share "other".
const MODEL_COLORS = ["#9b79ee", "#c68206", "#db5fa1", "#a6930b", "#12a7a7", "#e86154", "#0599ef", "#3fac4a"];
const OTHER_COLOR = "#8892a0";
const REFRESH_MS = 60_000;
const CARDS_PER_GROUP = 12;
const ITEMS_SHOWN = 10;
const MAX_DAY_BARS = 62;
const PERIODS = ["1", "7", "30", "all", "custom"];

const TEXT = {
  it: {
    periods: { 1: "1G", 7: "7G", 30: "30G", all: "STORICO", custom: "CUSTOM" },
    allProviders: "TUTTI",
    costLabel: (mode, n) =>
      mode === "1" ? "costo stimato oggi" : mode === "all" ? "costo stimato storico" : n === 1 ? "costo stimato 1 giorno" : `costo stimato ${n} giorni`,
    projection: "proiezione",
    perMonth: "/30 g",
    rawTokens: "token grezzi, come li conta Claude Code",
    ringHint: "esterno: costo per giorno, clic per isolarlo. Interno: quota per modello",
    byModel: "per modello",
    byHost: "per host",
    byProvider: "per provider",
    cache: "cache",
    hitRatio: "hit ratio",
    read: "lettura",
    write: "scrittura",
    sessions: "sessioni",
    sessionsHint: "clicca una sessione per vederne prompt e subagenti",
    groupBy: "raggruppa",
    host: "HOST",
    project: "PROGETTO",
    sortBy: "ordina",
    recent: "RECENTI",
    cost: "COSTO",
    showAll: (n) => `mostra tutte (${n})`,
    showFewer: "mostra meno",
    showAllItems: (n) => `mostra tutti (${n})`,
    noProject: "(senza progetto)",
    prePrompt: "prima del primo prompt",
    prompt: (n) => `prompt ${n}`,
    withSub: "con subagenti",
    prompts: "prompt",
    subagents: "subagenti della sessione",
    close: "chiudi",
    seen: (rel) => `visto ${rel}`,
    dataAt: (rel) => `dati ${rel}`,
    noContact: "nessun contatto dall'aggiornamento",
    noBatch: "nessun lotto ricevuto",
    unpricedBadge: "+ costo senza prezzo",
    dayKeys: "frecce per scorrere i giorni, Invio per isolarne uno",
    hostsWaiting: "Agenti gia' collegati:",
    now: "adesso",
    ago: (n, unit) => `${n} ${{ m: "min", h: "h", d: "g" }[unit]} fa`,
    lastIngest: "ultimo invio",
    loading: "caricamento...",
    unreachable: (msg) => `Server non raggiungibile (${msg}). Nuovo tentativo tra un minuto.`,
    retry: "riprova",
    emptyTitle: "Ancora nessun dato",
    emptyBody: "Avvia un agente: su questo PC",
    emptyOr: "oppure, su un altro PC,",
    unpriced: "senza prezzo, esclusi dai costi:",
    range: "intervallo",
    rangeTo: "fino al",
    periodLabel: (from, to) => (from === to ? `${from}, UTC` : `dal ${from} al ${to}, UTC`),
    noSessions: "Nessuna sessione nel periodo.",
    dayTable: "Costo per giorno",
    weekOf: (d) => `settimana dal ${d}`,
    sessionsCount: (n) => `${n} sessioni`,
    messages: "msg",
    sessionGone: "Questa sessione non ha consumi nel periodo scelto.",
    other: "altri",
  },
  en: {
    periods: { 1: "1D", 7: "7D", 30: "30D", all: "ALL", custom: "CUSTOM" },
    allProviders: "ALL",
    costLabel: (mode, n) =>
      mode === "1" ? "estimated cost today" : mode === "all" ? "estimated all-time cost" : `estimated ${n}-day cost`,
    projection: "projection",
    perMonth: "/30 d",
    rawTokens: "raw tokens, as Claude Code counts them",
    ringHint: "outer: cost per day, click to isolate it. Inner: share by model",
    byModel: "by model",
    byHost: "by host",
    byProvider: "by provider",
    cache: "cache",
    hitRatio: "hit ratio",
    read: "read",
    write: "write",
    sessions: "sessions",
    sessionsHint: "click a session to see its prompts and subagents",
    groupBy: "group by",
    host: "HOST",
    project: "PROJECT",
    sortBy: "sort",
    recent: "RECENT",
    cost: "COST",
    showAll: (n) => `show all (${n})`,
    showFewer: "show fewer",
    showAllItems: (n) => `show all (${n})`,
    noProject: "(no project)",
    prePrompt: "before the first prompt",
    prompt: (n) => `prompt ${n}`,
    withSub: "with subagents",
    prompts: "prompts",
    subagents: "session subagents",
    close: "close",
    seen: (rel) => `seen ${rel}`,
    dataAt: (rel) => `data ${rel}`,
    noContact: "no contact since the upgrade",
    noBatch: "no batch received yet",
    unpricedBadge: "+ unpriced usage",
    dayKeys: "arrow keys move between days, Enter isolates one",
    hostsWaiting: "Agents already connected:",
    now: "just now",
    ago: (n, unit) => `${n} ${{ m: "min", h: "h", d: "d" }[unit]} ago`,
    lastIngest: "last ingest",
    loading: "loading...",
    unreachable: (msg) => `Server unreachable (${msg}). Retrying in a minute.`,
    retry: "retry",
    emptyTitle: "No usage yet",
    emptyBody: "Start an agent: on this machine",
    emptyOr: "or, on another machine,",
    unpriced: "no price, left out of the costs:",
    range: "range",
    rangeTo: "to",
    periodLabel: (from, to) => (from === to ? `${from}, UTC` : `${from} to ${to}, UTC`),
    noSessions: "No sessions in this period.",
    dayTable: "Cost per day",
    weekOf: (d) => `week of ${d}`,
    sessionsCount: (n) => `${n} sessions`,
    messages: "msg",
    sessionGone: "This session has no usage in the selected period.",
    other: "other",
  },
};

// formatting --------------------------------------------------------------------------------

const localeOf = (lang) => (lang === "it" ? "it-IT" : "en-US");

function money(value, lang) {
  return "$" + (value || 0).toLocaleString(localeOf(lang), { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function tokens(value, lang) {
  const units = lang === "it" ? [[1e9, "Mld"], [1e6, "M"], [1e3, "k"]] : [[1e9, "B"], [1e6, "M"], [1e3, "k"]];
  for (const [size, unit] of units) {
    if (value >= size) return (value / size).toLocaleString(localeOf(lang), { maximumFractionDigits: 1 }) + unit;
  }
  return String(value || 0);
}

const shortModel = (model) => model.replace(/^claude-/, "");
const shortId = (id) => (id.length > 12 ? id.slice(0, 8) : id);

function dayLabel(iso, lang) {
  return new Date(iso + "T00:00:00Z").toLocaleDateString(localeOf(lang), { day: "numeric", month: "short", timeZone: "UTC" });
}

function stampLabel(stamp, lang) {
  // "YYYY-MM-DD HH:MM" in UTC
  if (!stamp) return "";
  return `${dayLabel(stamp.slice(0, 10), lang)} ${stamp.slice(11, 16)}`;
}

function addDays(iso, days) {
  const date = new Date(iso + "T00:00:00Z");
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

const todayUtc = () => new Date().toISOString().slice(0, 10);
const daysInRange = (from, to) => Math.round((Date.parse(to + "T00:00:00Z") - Date.parse(from + "T00:00:00Z")) / 86_400_000) + 1;

function relativeTime(utcStamp, T) {
  if (!utcStamp) return null;
  const parsed = Date.parse(utcStamp.replace(" ", "T") + (utcStamp.length <= 16 ? ":00Z" : "Z"));
  if (Number.isNaN(parsed)) return null;
  const minutes = Math.floor(Math.max(0, Date.now() - parsed) / 60_000);
  if (minutes < 1) return T.now;
  if (minutes < 60) return T.ago(minutes, "m");
  if (minutes < 48 * 60) return T.ago(Math.floor(minutes / 60), "h");
  return T.ago(Math.floor(minutes / 1440), "d");
}

const sum = (values) => values.reduce((total, value) => total + value, 0);

// state in the URL ----------------------------------------------------------------------------

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const isoDayOrEmpty = (value) => (value && ISO_DAY.test(value) && !Number.isNaN(Date.parse(value)) ? value : "");

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const period = params.get("p");
  return {
    period: PERIODS.includes(period) ? period : "7",
    from: isoDayOrEmpty(params.get("f")),
    to: isoDayOrEmpty(params.get("t")),
    provider: params.get("prov") || "",
    group: params.get("g") === "project" ? "project" : "host",
    sort: params.get("so") === "cost" ? "cost" : "recent",
    sel: params.get("s") || null,
    lang: ["it", "en"].includes(params.get("l")) ? params.get("l") : null,
  };
}

function writeHash(state) {
  const params = new URLSearchParams();
  if (state.period !== "7") params.set("p", state.period);
  if (state.period === "custom") {
    if (state.from) params.set("f", state.from);
    if (state.to) params.set("t", state.to);
  }
  if (state.provider) params.set("prov", state.provider);
  if (state.group !== "host") params.set("g", state.group);
  if (state.sort !== "recent") params.set("so", state.sort);
  if (state.sel) params.set("s", state.sel);
  params.set("l", state.lang);
  const hash = params.toString();
  history.replaceState(null, "", hash ? "#" + hash : location.pathname + location.search);
}

function initialLang() {
  try {
    const saved = localStorage.getItem("ccmeter.lang");
    if (saved === "it" || saved === "en") return saved;
  } catch {
    // storage blocked: fall back to the browser language
  }
  return (navigator.language || "en").toLowerCase().startsWith("it") ? "it" : "en";
}

function saveLang(lang) {
  try {
    localStorage.setItem("ccmeter.lang", lang);
  } catch {
    // not persisted, still applied
  }
}

// data ---------------------------------------------------------------------------------------

class HttpError extends Error {
  constructor(status) {
    super(`HTTP ${status}`);
    this.status = status;
  }
}

async function getJson(url) {
  // no-cache: the browser revalidates with the ETag and gets a 304 when nothing changed
  const response = await fetch(url, { cache: "no-cache", headers: { Accept: "application/json" } });
  if (!response.ok) throw new HttpError(response.status);
  return response.json();
}

function apiUrl(path, params = {}) {
  const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value)).toString();
  return `../api/dashboard/${path}${query ? "?" + query : ""}`;
}

// radial chart geometry (viewBox 0 0 460 460) --------------------------------------------------

const CENTER = 230;

function polar(radius, degrees) {
  const angle = ((degrees - 90) * Math.PI) / 180;
  return [CENTER + radius * Math.cos(angle), CENTER + radius * Math.sin(angle)];
}

function sectorPath(inner, outer, start, end) {
  const large = end - start > 180 ? 1 : 0;
  const [x0, y0] = polar(outer, start);
  const [x1, y1] = polar(outer, end);
  const [x2, y2] = polar(inner, end);
  const [x3, y3] = polar(inner, start);
  const f = (n) => n.toFixed(2);
  return `M${f(x0)} ${f(y0)}A${outer} ${outer} 0 ${large} 1 ${f(x1)} ${f(y1)}L${f(x2)} ${f(y2)}A${inner} ${inner} 0 ${large} 0 ${f(x3)} ${f(y3)}Z`;
}

function dayBuckets(from, to, modelsOfDay, lang, T) {
  const count = daysInRange(from, to);
  const step = count > MAX_DAY_BARS ? 7 : 1;
  const buckets = [];
  for (let offset = 0; offset < count; offset += step) {
    const start = addDays(from, offset);
    const end = addDays(from, Math.min(offset + step, count) - 1);
    const byModel = {};
    for (let day = start; day <= end; day = addDays(day, 1)) {
      for (const [name, cost] of Object.entries(modelsOfDay(day))) byModel[name] = (byModel[name] || 0) + cost;
    }
    const cost = sum(Object.values(byModel));
    const label = step === 1 ? dayLabel(start, lang) : T.weekOf(dayLabel(start, lang));
    buckets.push({ start, end, cost, byModel, label });
  }
  return buckets;
}

// the app ----------------------------------------------------------------------------------------

class App extends Component {
  constructor() {
    super();
    const hash = readHash();
    this.state = {
      ...hash,
      lang: hash.lang || initialLang(),
      base: null,
      view: null,
      viewProvider: null,
      error: null,
      sessions: null,
      sessionsKey: null,
      sessionsError: null,
      detail: null,
      detailKey: null,
      detailError: null,
      expanded: {},
      promptSort: "recent",
      agentSort: "cost",
      allPrompts: false,
      allAgents: false,
      hover: null,
    };
    this.loadRequest = 0;
    this.focusDay = null;
  }

  componentDidMount() {
    this.load();
    this.timer = setInterval(() => this.load(), REFRESH_MS);
    this.onHash = () => {
      const next = readHash();
      const providerChanged = next.provider !== this.state.provider;
      this.setState({ ...next, lang: next.lang || this.state.lang }, () =>
        providerChanged ? this.load() : this.syncSessions(),
      );
    };
    window.addEventListener("hashchange", this.onHash);
  }

  componentWillUnmount() {
    clearInterval(this.timer);
    window.removeEventListener("hashchange", this.onHash);
  }

  componentDidUpdate() {
    writeHash(this.state);
    document.documentElement.lang = this.state.lang;
  }

  async load() {
    const request = ++this.loadRequest;
    const asked = this.state.provider;
    try {
      const base = await getJson(apiUrl("overview"));
      let view = base;
      let provider = asked;
      if (provider && base.providers.includes(provider)) {
        view = await getJson(apiUrl("overview", { provider }));
      } else {
        provider = "";
      }
      // a newer load, or a provider chosen meanwhile, owns the screen now
      if (request !== this.loadRequest || this.state.provider !== asked) return;
      this.setState({ base, view, viewProvider: provider, provider, error: null }, () => this.syncSessions(true));
    } catch (error) {
      if (request === this.loadRequest) this.setState({ error: error.message || String(error) });
    }
  }

  sessionsKey() {
    const { from, to } = this.period();
    return `${from}|${to}|${this.state.provider}`;
  }

  detailKey() {
    return this.state.sel ? `${this.state.sel}|${this.sessionsKey()}` : null;
  }

  period() {
    const today = todayUtc();
    const days = (this.state.view || {}).days || [];
    const first = days.length ? days[0].date : today;
    const last = days.length ? days[days.length - 1].date : today;
    const end = last > today ? last : today;
    const mode = this.state.period;
    if (mode === "custom") {
      let from = this.state.from || first;
      let to = this.state.to || end;
      if (from > to) [from, to] = [to, from];
      return { from, to };
    }
    if (mode === "all") return { from: first < end ? first : end, to: end };
    return { from: addDays(today, 1 - Number(mode)), to: today };
  }

  // Every response is checked against the view that is on screen when it arrives: a slow
  // answer for a period or session the user already left is dropped, and the screen never
  // shows data under a label that does not match it.
  syncSessions(force = false) {
    const key = this.sessionsKey();
    if (force || key !== this.state.sessionsKey || this.state.sessionsError) {
      const { from, to } = this.period();
      getJson(apiUrl("session-list", { from, to, provider: this.state.provider }))
        .then((data) => {
          if (key === this.sessionsKey()) this.setState({ sessions: data.sessions, sessionsKey: key, sessionsError: null });
        })
        .catch((error) => {
          if (key === this.sessionsKey()) this.setState({ sessions: null, sessionsKey: key, sessionsError: error.message || String(error) });
        });
    }
    this.syncDetail(force);
  }

  syncDetail(force = false) {
    const key = this.detailKey();
    if (!key || (!force && key === this.state.detailKey && !this.state.detailError)) return;
    const { from, to } = this.period();
    getJson(apiUrl(`session-detail/${encodeURIComponent(this.state.sel)}`, { from, to, provider: this.state.provider }))
      .then((detail) => {
        if (key === this.detailKey()) this.setState({ detail, detailKey: key, detailError: null });
      })
      .catch((error) => {
        if (key !== this.detailKey()) return;
        this.setState({ detail: null, detailKey: key, detailError: error.status === 404 ? "gone" : error.message || String(error) });
      });
  }

  // state changes that move the period or the filter reload the period-dependent data
  setPeriod(patch) {
    this.setState({ ...patch, hover: null }, () => this.syncSessions());
  }

  setProvider(provider) {
    this.setState({ provider, sel: null, detail: null, detailKey: null }, () => this.load());
  }

  select(sessionId) {
    const sel = this.state.sel === sessionId ? null : sessionId;
    this.setState({ sel, detail: null, detailKey: null, detailError: null, allPrompts: false, allAgents: false }, () => {
      this.syncDetail(true);
      if (sel) requestAnimationFrame(() => document.getElementById("detail")?.scrollIntoView({ behavior: this.motion(), block: "nearest" }));
    });
  }

  selectDay(bar) {
    this.focusDay = null;
    this.setPeriod({ period: "custom", from: bar.start, to: bar.end, sel: null });
  }

  onDayKey(event, bar, count) {
    const moves = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      this.selectDay(bar);
      return;
    }
    let next = null;
    if (event.key in moves) next = (bar.index + moves[event.key] + count) % count;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = count - 1;
    if (next === null) return;
    event.preventDefault();
    this.focusDay = next;
    this.setState({ hover: next }, () => document.querySelector(`.day-hit[data-day="${next}"]`)?.focus());
  }

  contactText(contact, T) {
    const seen = relativeTime(contact && contact.last_seen, T);
    const data = relativeTime(contact && contact.last_data, T);
    if (!seen) return T.noContact;
    return T.seen(seen) + ", " + (data ? T.dataAt(data) : T.noBatch);
  }

  motion() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  }

  setLang(lang) {
    saveLang(lang);
    this.setState({ lang });
  }

  render() {
    const { lang, view, viewProvider, provider, base, error } = this.state;
    const T = TEXT[lang];
    // the overview on screen must be the one of the selected provider: while a switch is
    // loading, or after it failed, the previous provider's numbers are not shown under it
    if (!view || viewProvider !== provider) {
      return html`<main class="page">
        ${this.renderTopbar(T, null)}
        ${error
          ? html`<p class="notice" role="alert"><strong>${T.unreachable(error)}</strong><button type="button" onClick=${() => this.load()}>${T.retry}</button></p>`
          : html`<p class="boot">${T.loading}</p>`}
      </main>`;
    }
    if (!base.days.length) {
      return html`<main class="page">
        ${this.renderTopbar(T, null)}
        <section class="empty">
          <h1>${T.emptyTitle}</h1>
          <p>${T.emptyBody} <code>ccmeter local</code>, ${T.emptyOr} <code>ccmeter agent run</code>.</p>
          ${Object.keys(base.health.hosts || {}).length
            ? html`<p class="muted">${T.hostsWaiting} ${Object.entries(base.health.hosts)
                .map(([name, contact]) => `${name} (${this.contactText(contact, T)})`)
                .join(", ")}</p>`
            : null}
        </section>
      </main>`;
    }
    const model = this.computeModel(T);
    return html`<main class="page">
      ${this.renderTopbar(T, model)}
      ${error ? html`<p class="notice" role="alert"><strong>${T.unreachable(error)}</strong><button type="button" onClick=${() => this.load()}>${T.retry}</button></p>` : null}
      ${view.unpricedModels.length ? html`<p class="notice"><strong>${T.unpriced}</strong> ${view.unpricedModels.join(", ")}</p>` : null}
      <section class="core-row">
        ${this.renderModels(T, model)}
        ${this.renderCore(T, model)}
        ${this.renderHosts(T, model)}
      </section>
      ${this.renderSessions(T, model)}
    </main>`;
  }

  computeModel(T) {
    const { view, base, lang } = this.state;
    const { from, to } = this.period();
    const days = view.days.filter((day) => day.date >= from && day.date <= to);
    const dayModels = new Map(days.map((day) => [day.date, day.byModel]));
    const modelCost = {};
    const modelTok = {};
    const hostCost = {};
    const raw = { input: 0, output: 0, cache_read: 0, cache_write: 0 };
    for (const day of days) {
      for (const [name, cost] of Object.entries(day.byModel)) modelCost[name] = (modelCost[name] || 0) + cost;
      for (const [name, cost] of Object.entries(day.byHost)) hostCost[name] = (hostCost[name] || 0) + cost;
      for (const [name, tok] of Object.entries(day.byModelTok)) {
        const acc = modelTok[name] || (modelTok[name] = [0, 0, 0, 0]);
        tok.forEach((value, index) => (acc[index] += value));
      }
      for (const key of Object.keys(raw)) raw[key] += (day.rawTok || {})[key] || 0;
    }
    const total = sum(days.map((day) => day.total));
    const dayCount = daysInRange(from, to);
    // colors follow the model whatever period or provider is selected: they come from the
    // unfiltered data only, ranking by the last 30 days (models in use now) then all-time cost
    const recentFrom = addDays(todayUtc(), -29);
    const recentCost = {};
    for (const day of base.days) {
      if (day.date < recentFrom) continue;
      for (const [name, cost] of Object.entries(day.byModel)) recentCost[name] = (recentCost[name] || 0) + cost;
    }
    const ranked = [...base.models].sort((a, b) => (recentCost[b.model] || 0) - (recentCost[a.model] || 0) || b.cost - a.cost);
    const colorOf = {};
    ranked.forEach((item, index) => (colorOf[item.model] = index < MODEL_COLORS.length ? MODEL_COLORS[index] : OTHER_COLOR));
    const color = (name) => colorOf[name] || OTHER_COLOR;
    const colorRank = (value) => (value === OTHER_COLOR ? MODEL_COLORS.length : MODEL_COLORS.indexOf(value));
    const providerOf = Object.fromEntries(base.models.map((item) => [item.model, item.provider]));
    const models = view.models
      .map((item) => ({ name: item.model, cost: modelCost[item.model] || 0, tok: modelTok[item.model] || [0, 0, 0, 0] }))
      .filter((item) => item.cost > 0 || sum(item.tok) > 0)
      .sort((a, b) => b.cost - a.cost);
    const hostNames = new Set([...view.hosts.map((item) => item.host), ...Object.keys(base.health.hosts || {})]);
    const hosts = [...hostNames]
      .map((name) => ({ name, cost: hostCost[name] || 0, contact: (base.health.hosts || {})[name] || null }))
      .sort((a, b) => b.cost - a.cost || a.name.localeCompare(b.name));
    const providerCost = {};
    for (const [name, cost] of Object.entries(modelCost)) {
      const provider = providerOf[name] || "claude";
      providerCost[provider] = (providerCost[provider] || 0) + cost;
    }
    const cacheRead = sum(Object.values(modelTok).map((tok) => tok[2]));
    const cacheWrite = sum(Object.values(modelTok).map((tok) => tok[3]));
    const input = sum(Object.values(modelTok).map((tok) => tok[0]));
    const buckets = dayBuckets(from, to, (day) => dayModels.get(day) || {}, lang, T);
    return {
      from,
      to,
      total,
      dayCount,
      projection: (total / dayCount) * 30,
      models,
      hosts,
      providerCost,
      color,
      colorRank,
      cacheRead,
      cacheWrite,
      hitRatio: cacheRead / (cacheRead + input || 1),
      rawTotal: raw.input + raw.output + raw.cache_read + raw.cache_write,
      buckets,
      days,
    };
  }

  renderTopbar(T, model) {
    const { lang, base, period, provider } = this.state;
    const providers = base ? base.providers : [];
    const lastIngest = base ? relativeTime(base.health.last_ingest, T) : null;
    return html`<header>
      <div class="topbar">
        <div class="brand">CCMETER<span>://</span>NUCLEO</div>
        ${model ? html`<div class="meta">${T.periodLabel(dayLabel(model.from, lang), dayLabel(model.to, lang))}</div>` : null}
        <div class="spacer"></div>
        ${lastIngest ? html`<div class="meta">${T.lastIngest} ${lastIngest}</div>` : null}
        ${model
          ? html`<div class="segmented" role="group" aria-label="period">
              ${PERIODS.map(
                (mode) => html`<button type="button" aria-pressed=${String(period === mode)} onClick=${() => this.setPeriod({ period: mode, sel: null, detail: null })}>${T.periods[mode]}</button>`,
              )}
            </div>`
          : null}
        ${providers.length > 1
          ? html`<div class="segmented tint-core" role="group" aria-label="provider">
              <button type="button" aria-pressed=${String(!provider)} onClick=${() => this.setProvider("")}>${T.allProviders}</button>
              ${providers.map(
                (name) => html`<button type="button" aria-pressed=${String(provider === name)} onClick=${() => this.setProvider(name)}>${name.toUpperCase()}</button>`,
              )}
            </div>`
          : null}
        <div class="segmented tint-core" role="group" aria-label="language">
          <button type="button" aria-pressed=${String(lang === "it")} onClick=${() => this.setLang("it")}>ITA</button>
          <button type="button" aria-pressed=${String(lang === "en")} onClick=${() => this.setLang("en")}>ENG</button>
        </div>
      </div>
      ${model && period === "custom"
        ? html`<div class="range-row">
            <label for="range-from">${T.range}</label>
            <input id="range-from" type="date" value=${model.from} max=${model.to} onChange=${(event) => this.setPeriod({ from: event.target.value, sel: null })} />
            <label for="range-to">${T.rangeTo}</label>
            <input id="range-to" type="date" value=${model.to} min=${model.from} onChange=${(event) => this.setPeriod({ to: event.target.value, sel: null })} />
          </div>`
        : null}
    </header>`;
  }

  renderModels(T, model) {
    const { lang } = this.state;
    const top = Math.max(...model.models.map((item) => item.cost), 0.01);
    return html`<div class="side left">
      <div class="side-title">${T.byModel}<i style=${{ "--dot": "var(--model-label)" }}></i></div>
      ${model.models.map(
        (item) => html`<div>
          <div class="row-head">
            <span class="swatch" style=${{ background: model.color(item.name) }}></span>
            <span class="row-name">${shortModel(item.name)}</span>
            <span class="money">${money(item.cost, lang)}</span>
          </div>
          <div class="track"><span style=${{ width: Math.max(3, (item.cost / top) * 100) + "%", background: model.color(item.name) }}></span></div>
          <div class="row-foot">in ${tokens(item.tok[0], lang)}, out ${tokens(item.tok[1], lang)}, cache ${tokens(item.tok[2], lang)}</div>
        </div>`,
      )}
      <div class="cache-box">
        <div class="side-title">${T.cache}<i style=${{ "--dot": "var(--cache)" }}></i></div>
        <div>${T.read} <b>${tokens(model.cacheRead, lang)}</b>, ${T.write} <b>${tokens(model.cacheWrite, lang)}</b></div>
        <div class="row-foot">${T.hitRatio} ${(model.hitRatio * 100).toLocaleString(localeOf(lang), { maximumFractionDigits: 1 })}%</div>
      </div>
    </div>`;
  }

  renderCore(T, model) {
    const { lang, period, hover } = this.state;
    const buckets = model.buckets;
    const count = buckets.length;
    const top = Math.max(...buckets.map((bucket) => bucket.cost), 0.0001);
    const slice = 360 / count;
    const gap = count > 1 ? Math.min(2, slice * 0.15) : 0;
    const inner = 174;
    const maxLength = 38;
    const bars = buckets.map((bucket, index) => {
      const fromAngle = index * slice + gap / 2;
      const toAngle = count > 1 ? (index + 1) * slice - gap / 2 : 359.9;
      const length = bucket.cost > 0 ? Math.max(3, (bucket.cost / top) * maxLength) : 2;
      // one segment per model color, in a fixed order, so equal days with different models
      // look different; models past the palette share the "other" segment
      const byColor = new Map();
      for (const [name, cost] of Object.entries(bucket.byModel)) {
        if (cost > 0) byColor.set(model.color(name), (byColor.get(model.color(name)) || 0) + cost);
      }
      let radius = inner;
      const segments = [...byColor.entries()]
        .sort((a, b) => model.colorRank(a[0]) - model.colorRank(b[0]))
        .map(([color, cost]) => {
          const part = (cost / (bucket.cost || 1)) * length;
          const segment = { color, from: radius, to: radius + Math.max(0.6, part - 0.6) };
          radius += part;
          return segment;
        });
      return { ...bucket, fromAngle, toAngle, length, index, segments };
    });
    // inner ring: share of the period cost by model; models past the palette share "other"
    const ring = [];
    let otherCost = 0;
    for (const item of model.models) {
      if (model.color(item.name) === OTHER_COLOR) otherCost += item.cost;
      else if (item.cost > 0) ring.push({ name: item.name, cost: item.cost, color: model.color(item.name) });
    }
    if (otherCost > 0) ring.push({ name: T.other, cost: otherCost, color: OTHER_COLOR });
    const radius = 130;
    const circumference = 2 * Math.PI * radius;
    let offset = 0;
    const arcs = ring.map((item) => {
      const fraction = item.cost / (model.total || 1);
      const length = Math.max(1.5, fraction * circumference - (ring.length > 1 ? 4 : 0));
      const arc = { ...item, dash: `${length.toFixed(1)} ${(circumference - length).toFixed(1)}`, offset: (-offset * circumference).toFixed(1) };
      offset += fraction;
      return arc;
    });
    const hovered = hover !== null ? bars[hover] : null;
    const focused = Math.min(this.focusDay ?? bars.length - 1, bars.length - 1);
    return html`<div class="core-wrap"><div class="core">
      <svg viewBox="0 0 460 460" onMouseLeave=${() => this.setState({ hover: null })}>
        <circle cx="230" cy="230" r="216" fill="none" stroke="rgba(255,255,255,0.05)" stroke-width="1" />
        <circle cx="230" cy="230" r="160" fill="none" stroke="rgba(94,231,214,0.22)" stroke-width="1" />
        <circle cx="230" cy="230" r=${radius} fill="none" stroke="rgba(255,255,255,0.04)" stroke-width="14" />
        <g transform="rotate(-90 230 230)">
          ${arcs.map((arc) => html`<circle cx="230" cy="230" r=${radius} fill="none" stroke=${arc.color} stroke-width="14" stroke-dasharray=${arc.dash} stroke-dashoffset=${arc.offset} />`)}
        </g>
        ${bars.map((bar) =>
          bar.segments.length
            ? html`<g class=${"day-bar" + (hover === bar.index ? " active" : "")}>
                ${bar.segments.map((segment) => html`<path fill=${segment.color} d=${sectorPath(segment.from, segment.to, bar.fromAngle, bar.toAngle)} />`)}
              </g>`
            : html`<path class="day-bar empty" d=${sectorPath(inner, inner + bar.length, bar.fromAngle, bar.toAngle)} />`,
        )}
        <g role="group" aria-label=${`${T.dayTable}: ${T.dayKeys}`}>
          ${bars.map(
            (bar) => html`<path class="day-hit" data-day=${bar.index} role="button"
              tabindex=${bar.index === focused ? 0 : -1}
              aria-label=${`${bar.label}: ${money(bar.cost, lang)}`}
              d=${sectorPath(inner - 6, inner + maxLength + 6, bar.index * slice, (bar.index + 1) * slice)}
              onMouseEnter=${() => this.setState({ hover: bar.index })}
              onFocus=${() => this.setState({ hover: bar.index })}
              onBlur=${() => this.setState({ hover: null })}
              onKeyDown=${(event) => this.onDayKey(event, bar, bars.length)}
              onClick=${() => this.selectDay(bar)}>
              <title>${bar.label}: ${money(bar.cost, lang)}</title>
            </path>`,
          )}
        </g>
      </svg>
      <div class="core-text">
        <div class="core-label">${T.costLabel(period, model.dayCount)}</div>
        <div class="core-total">${money(model.total, lang)}</div>
        <div class="core-line">${T.projection} <b>${money(model.projection, lang)}</b>${T.perMonth}</div>
        <div class="core-small">${T.rawTokens}: <b>${tokens(model.rawTotal, lang)}</b></div>
        <div class="core-hover" aria-live="polite">${hovered ? `${hovered.label}: ${money(hovered.cost, lang)}` : ""}</div>
      </div>
    </div>
    <div class="core-hint">${T.ringHint}</div>
    </div>`;
  }

  renderHosts(T, model) {
    const { lang, provider, base } = this.state;
    const top = Math.max(...model.hosts.map((item) => item.cost), 0.01);
    const providers = Object.entries(model.providerCost).sort((a, b) => b[1] - a[1]);
    const providerTop = Math.max(...providers.map(([, cost]) => cost), 0.01);
    return html`<div class="side">
      <div class="side-title"><i style=${{ "--dot": "var(--host)" }}></i>${T.byHost}</div>
      ${model.hosts.map((item) => {
        return html`<div>
          <div class="row-head">
            <span class="money">${money(item.cost, lang)}</span>
            <span class="row-name">${item.name}</span>
          </div>
          <div class="track"><span style=${{ width: Math.max(3, (item.cost / top) * 100) + "%", background: "var(--host)" }}></span></div>
          <div class="row-foot">${this.contactText(item.contact, T)}</div>
        </div>`;
      })}
      ${!provider && base.providers.length > 1
        ? html`<div class="side-title"><i style=${{ "--dot": "var(--ink)" }}></i>${T.byProvider}</div>
            ${providers.map(
              ([name, cost]) => html`<div>
                <div class="row-head">
                  <span class="money">${money(cost, lang)}</span>
                  <span class="row-name">${name}</span>
                </div>
                <div class="track"><span style=${{ width: Math.max(3, (cost / providerTop) * 100) + "%", background: "var(--ink-muted)" }}></span></div>
              </div>`,
            )}`
        : null}
    </div>`;
  }

  renderSessions(T, model) {
    const { lang, group, sort, sel, expanded } = this.state;
    const current = this.state.sessionsKey === this.sessionsKey();
    const sessions = current ? this.state.sessions : null;
    const sessionsError = current ? this.state.sessionsError : null;
    const list = [...(sessions || [])];
    if (sort === "cost") list.sort((a, b) => b.total - a.total);
    const groups = [];
    const byKey = new Map();
    for (const item of list) {
      const key = group === "project" ? item.project || T.noProject : item.host;
      if (!byKey.has(key)) {
        byKey.set(key, []);
        groups.push(key);
      }
      byKey.get(key).push(item);
    }
    return html`<section class="sessions" aria-label=${T.sessions}>
      <div class="section-head">
        <div class="section-title">${T.sessions}</div>
        <div class="hint">${sessions ? T.sessionsCount(list.length) + ". " + T.sessionsHint : T.loading}</div>
        <div class="spacer"></div>
        <div class="hint">${T.groupBy}</div>
        <div class="segmented tint-host" role="group" aria-label=${T.groupBy}>
          <button type="button" aria-pressed=${String(group === "host")} onClick=${() => this.setState({ group: "host" })}>${T.host}</button>
          <button type="button" aria-pressed=${String(group === "project")} onClick=${() => this.setState({ group: "project" })}>${T.project}</button>
        </div>
        <div class="hint">${T.sortBy}</div>
        <div class="segmented" role="group" aria-label=${T.sortBy}>
          <button type="button" aria-pressed=${String(sort === "recent")} onClick=${() => this.setState({ sort: "recent" })}>${T.recent}</button>
          <button type="button" aria-pressed=${String(sort === "cost")} onClick=${() => this.setState({ sort: "cost" })}>${T.cost}</button>
        </div>
      </div>
      ${sessionsError ? html`<p class="notice" role="alert"><strong>${T.unreachable(sessionsError)}</strong></p>` : null}
      ${sessions && !list.length ? html`<p class="muted">${T.noSessions}</p>` : null}
      ${groups.map((key) => {
        const items = byKey.get(key);
        const open = expanded[group + ":" + key];
        const shown = open ? items : items.slice(0, CARDS_PER_GROUP);
        return html`<div class="group">
          <div class="group-head">
            <div class="group-name">${key}</div>
            <div class="group-meta">${T.sessionsCount(items.length)}, ${money(sum(items.map((item) => item.total)), lang)}</div>
            <div class="group-rule"></div>
          </div>
          <div class="cards">${shown.map((item) => this.renderCard(T, model, item))}</div>
          ${items.length > CARDS_PER_GROUP
            ? html`<div class="more"><button type="button" onClick=${() => this.setState({ expanded: { ...expanded, [group + ":" + key]: !open } })}>${open ? T.showFewer : T.showAll(items.length)}</button></div>`
            : null}
        </div>`;
      })}
      ${sel ? this.renderDetail(T, model) : null}
    </section>`;
  }

  renderCard(T, model, item) {
    const { lang, group, sel } = this.state;
    const meta = group === "project" ? item.host : stampLabel(item.started, lang);
    return html`<button type="button" class="card" aria-expanded=${String(sel === item.session_id)} onClick=${() => this.select(item.session_id)}>
      <div class="card-head">
        <span class="card-title">${group === "project" ? stampLabel(item.started, lang) : item.project || T.noProject}</span>
        <span class="money">${money(item.total, lang)}</span>
      </div>
      ${item.unpriced ? html`<div class="badge">${T.unpricedBadge}</div>` : null}
      <div class="card-meta" title=${item.session_id}>${shortId(item.session_id)}, ${meta}, ${item.promptCount} ${T.prompts}, ${item.messages} ${T.messages}</div>
      <div class="stack">
        ${item.models.map((entry) => html`<span style=${{ width: Math.max(2, (entry.cost / (item.total || 1)) * 100) + "%", background: model.color(entry.model) }}></span>`)}
      </div>
      <div class="chips">
        ${item.models.slice(0, 3).map((entry) => html`<span><span class="swatch" style=${{ background: model.color(entry.model) }}></span>${shortModel(entry.model)} ${money(entry.cost, lang)}</span>`)}
      </div>
    </button>`;
  }

  renderDetail(T, model) {
    const { lang, promptSort, agentSort, allPrompts, allAgents } = this.state;
    const current = this.state.detailKey === this.detailKey();
    const detail = current ? this.state.detail : null;
    const detailError = current ? this.state.detailError : null;
    const close = html`<button type="button" class="close" onClick=${() => this.select(this.state.sel)}>${T.close}</button>`;
    if (detailError) {
      return html`<div class="detail" id="detail">
        <div class="detail-head"><span class="muted">${detailError === "gone" ? T.sessionGone : T.unreachable(detailError)}</span><div class="spacer"></div>${close}</div>
      </div>`;
    }
    if (!detail) {
      return html`<div class="detail" id="detail"><div class="detail-head"><span class="muted">${T.loading}</span><div class="spacer"></div>${close}</div></div>`;
    }
    const session = detail.session;
    const prompts = [...detail.prompts];
    if (promptSort === "cost") prompts.sort((a, b) => b.main.cost + b.sub.cost - (a.main.cost + a.sub.cost));
    else prompts.sort((a, b) => b.index - a.index);
    const agents = [...detail.subagents];
    if (agentSort === "recent") agents.sort((a, b) => (b.lastTs || "").localeCompare(a.lastTs || ""));
    const promptsShown = allPrompts ? prompts : prompts.slice(0, ITEMS_SHOWN);
    const agentsShown = allAgents ? agents : agents.slice(0, ITEMS_SHOWN);
    return html`<div class="detail" id="detail">
      <div class="detail-head">
        <span class="detail-title">${session.project || T.noProject}</span>
        <span class="meta" title=${session.session_id}>${shortId(session.session_id)}, ${stampLabel(session.started, lang)} ${T.rangeTo} ${stampLabel(session.last, lang)}, ${session.host}</span>
        <div class="spacer"></div>
        <span class="money">${money(session.total, lang)}</span>
        ${session.unpriced ? html`<span class="badge">${T.unpricedBadge}</span>` : null}
        ${close}
      </div>
      <div class="pills">
        ${session.models.map(
          (entry) => html`<div class="pill">
            <span class="swatch" style=${{ background: model.color(entry.model) }}></span>
            <span class="name">${shortModel(entry.model)}</span>
            <span class="money">${money(entry.cost, lang)}</span>
            <span class="tokens">in ${tokens(entry.input, lang)}, out ${tokens(entry.output, lang)}, cache ${tokens(entry.cache_read, lang)}/${tokens(entry.cache_write, lang)}</span>
          </div>`,
        )}
      </div>
      <div class="sub-head">
        <div class="sub-title">${T.prompts} <span>(${prompts.length})</span></div>
        <div class="spacer"></div>
        <div class="segmented" role="group" aria-label=${T.sortBy}>
          <button type="button" aria-pressed=${String(promptSort === "recent")} onClick=${() => this.setState({ promptSort: "recent" })}>${T.recent}</button>
          <button type="button" aria-pressed=${String(promptSort === "cost")} onClick=${() => this.setState({ promptSort: "cost" })}>${T.cost}</button>
        </div>
      </div>
      <div class="prompts">${promptsShown.map((prompt) => this.renderPrompt(T, model, prompt))}</div>
      ${prompts.length > ITEMS_SHOWN
        ? html`<div class="more"><button type="button" onClick=${() => this.setState({ allPrompts: !allPrompts })}>${allPrompts ? T.showFewer : T.showAllItems(prompts.length)}</button></div>`
        : null}
      ${agents.length
        ? html`<div class="agents">
            <div class="sub-head">
              <div class="sub-title agents">${T.subagents} <span>(${agents.length})</span></div>
              <div class="spacer"></div>
              <div class="segmented tint-sub" role="group" aria-label=${T.sortBy}>
                <button type="button" aria-pressed=${String(agentSort === "cost")} onClick=${() => this.setState({ agentSort: "cost" })}>${T.cost}</button>
                <button type="button" aria-pressed=${String(agentSort === "recent")} onClick=${() => this.setState({ agentSort: "recent" })}>${T.recent}</button>
              </div>
            </div>
            <div class="agent-list">
              ${agentsShown.map(
                (agent) => html`<div class="agent">
                  <span class="key" title=${agent.key}>${shortId(agent.key)}</span>
                  <span><span class="swatch" style=${{ background: model.color(agent.model) }}></span> ${shortModel(agent.model)}</span>
                  <span class="tokens">${agent.messages} ${T.messages}, in ${tokens(agent.input, lang)}, out ${tokens(agent.output, lang)}, cache ${tokens(agent.cache_read, lang)}</span>
                  <span class="money">${money(agent.cost, lang)}</span>
                </div>`,
              )}
            </div>
            ${agents.length > ITEMS_SHOWN
              ? html`<div class="more sub"><button type="button" onClick=${() => this.setState({ allAgents: !allAgents })}>${allAgents ? T.showFewer : T.showAllItems(agents.length)}</button></div>`
              : null}
          </div>`
        : null}
    </div>`;
  }

  renderPrompt(T, model, prompt) {
    const { lang } = this.state;
    const names = [];
    for (const entry of [...prompt.models, ...prompt.subModels]) if (!names.includes(entry.model)) names.push(entry.model);
    const chips = names
      .map((name) => {
        const chat = (prompt.models.find((entry) => entry.model === name) || {}).cost || 0;
        const sub = (prompt.subModels.find((entry) => entry.model === name) || {}).cost || 0;
        return { name, chat, sub, total: chat + sub };
      })
      .filter((chip) => chip.total >= 0.005);
    return html`<div class="prompt">
      <div class="prompt-head">
        <span>${prompt.index === 0 ? T.prePrompt : T.prompt(prompt.index)}, ${dayLabel(prompt.date, lang)} ${prompt.ts}</span>
        <span class="money">${money(prompt.main.cost, lang)}</span>
      </div>
      ${prompt.sub.cost > 0 ? html`<div class="prompt-total">${T.withSub} ${money(prompt.main.cost + prompt.sub.cost, lang)}</div>` : null}
      <div class="prompt-tokens">in ${tokens(prompt.main.input, lang)}, out ${tokens(prompt.main.output, lang)}, cache ${tokens(prompt.main.cache_read, lang)}</div>
      <div class="chips">
        ${chips.map(
          (chip) => html`<span><span class="swatch" style=${{ background: model.color(chip.name) }}></span>${shortModel(chip.name)} ${money(chip.total, lang)}${chip.sub > 0 && chip.chat > 0 ? ` (chat ${money(chip.chat, lang)}, sub ${money(chip.sub, lang)})` : chip.sub > 0 ? " (sub)" : ""}</span>`,
        )}
      </div>
    </div>`;
  }
}

const root = document.getElementById("app");
root.textContent = ""; // drop the static "loading" placeholder: render() would keep it
render(html`<${App} />`, root);
