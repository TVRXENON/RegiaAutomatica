(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const CSRF = ($('meta[name="csrf"]') || {}).content || "";
  const TZ = "Europe/Rome";
  const page = document.body.dataset.page || "";
  let skew = 0; // differenza tra orologio del server e del dispositivo (ms)

  const fT = new Intl.DateTimeFormat("it-IT", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const fHM = new Intl.DateTimeFormat("it-IT", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
  const fD = new Intl.DateTimeFormat("it-IT", { timeZone: TZ, weekday: "long", day: "numeric", month: "long" });
  const now = () => (Date.now() + skew) / 1000;
  const hms = (ts) => (ts == null ? "—" : fT.format(new Date(ts * 1000)));
  const hm = (ts) => fHM.format(new Date(ts * 1000));
  const dur = (sec) => {
    sec = Math.max(0, Math.round(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
  };
  const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (id) => `<svg><use href="#i-${id}"/></svg>`;

  async function api(path, body) {
    const opt = { headers: { "X-CSRF": CSRF, Accept: "application/json" }, credentials: "same-origin" };
    if (body !== undefined) {
      opt.method = "POST";
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    let r;
    try { r = await fetch(path, opt); } catch (e) { throw new Error("Connessione assente: controlla la rete e riprova."); }
    if (r.status === 401) { location.href = "/accesso?next=" + encodeURIComponent(location.pathname); throw new Error("Accesso scaduto."); }
    let data = {};
    try { data = await r.json(); } catch (e) { /* risposta vuota */ }
    if (!r.ok) throw new Error(data.error || `Errore ${r.status}.`);
    return data;
  }

  let toastTimer;
  function toast(msg, bad = false) {
    const t = $("#toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.toggle("bad", bad);
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), bad ? 6000 : 3500);
  }

  // ---------- occhio per mostrare password e chiavi ----------
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-eye]");
    if (!b) return;
    const input = b.parentElement.querySelector("input");
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    b.querySelector("use").setAttribute("href", show ? "#i-eyeoff" : "#i-eye");
    const what = b.getAttribute("aria-label").includes("chiave") ? "la chiave" : "la password";
    b.setAttribute("aria-label", (show ? "Nascondi " : "Mostra ") + what);
    b.title = b.getAttribute("aria-label");
  });
  document.addEventListener("submit", (e) => {
    const f = e.target.closest("form[data-confirm]");
    if (f && !confirm(f.dataset.confirm)) e.preventDefault();
  });

  // ---------- orologio e stato ----------
  const TALLY = { in_onda: "In onda", fermo: "Ferma", errore: "Errore", non_configurato: "Da configurare", motore_fermo: "Motore fermo", avvio: "Avvio" };
  function tickClock() {
    const t = new Date(now() * 1000);
    const c = $("#clock"); if (c) c.textContent = fT.format(t);
    const bc = $("#big-clock");
    if (bc) { bc.firstChild.nodeValue = fT.format(t); const d = $("#big-date"); if (d) d.textContent = fD.format(t); }
    if (page === "onair") OnAir.tick();
  }
  let lastStatus = null;
  async function pollStatus() {
    if (!$("#tally")) return;
    try {
      const t0 = Date.now();
      const st = await api("/api/stato");
      skew = st.now * 1000 - (t0 + Date.now()) / 2;
      lastStatus = st;
      const code = st.engine.code === "in_onda" && !st.current ? "avvio" : st.engine.code;
      const tally = $("#tally");
      tally.dataset.code = code;
      tally.querySelector("span").textContent = TALLY[code] || code;
      tally.title = st.engine.message || TALLY[code] || "";
      if (page === "onair") OnAir.render(st);
    } catch (e) { /* riprova al prossimo giro */ }
  }

  // ---------- pagina In onda ----------
  const OnAir = {
    st: null,
    render(st) {
      this.st = st;
      const c = st.current, code = st.engine.code;
      const live = code === "in_onda" && !!c;
      $("#lamp").dataset.on = live ? "1" : "0";
      $("#lamp-text").textContent = live ? "In onda" : (TALLY[code] || "Collegamento in corso");
      $("#now-title").textContent = live ? c.title : (st.engine.message || "Nessun video in onda");
      const kinds = { riempitivo: '<span class="badge">Video di riempimento</span>', nero: '<span class="badge bad">Schermo nero: manca il video di riempimento</span>' };
      $("#now-kind").innerHTML = live ? (kinds[c.kind] || "") : "";
      $("#now-bar").hidden = !live;

      const msg = $("#state-msg");
      msg.className = "state-msg " + code;
      msg.textContent = code === "in_onda"
        ? "Trasmissione regolare verso il server RTMP."
        : (st.engine.message || "") + (code === "errore" ? " Il programma riprova da solo." : "");
      $("#btn-start").hidden = st.enabled;
      $("#btn-stop").hidden = !st.enabled;

      const ul = $("#warnings");
      ul.innerHTML = st.warnings.map((w) => `<li class="${w.level}">${esc(w.text)} <a href="${esc(w.link)}">Sistema</a></li>`).join("");

      const next = st.blocks.filter((b) => b.state !== "in_onda").slice(0, 10);
      $("#next-list").innerHTML = next.length
        ? next.map((b) => {
            const filler = b.kind !== "programma";
            const title = filler ? (b.kind === "nero" ? "Schermo nero" : `${esc(b.title)} in loop fino alle ${hm(b.end)}`) : esc(b.title);
            return `<li><span class="t num">${hms(b.start)}</span><span class="ti${filler ? " filler" : ""}">${title}</span><span class="d num">${dur(b.end - b.start)}</span></li>`;
          }).join("")
        : `<li><span class="muted">${st.enabled ? "Niente in programma nelle prossime ore." : "La trasmissione è ferma."}</span></li>`;
      this.strip();
      this.tick();
    },
    tick() {
      const st = this.st;
      if (!st || !st.current) return;
      const c = st.current, t = now();
      const p = Math.min(1, Math.max(0, (t - c.air_start) / Math.max(1, c.air_end - c.air_start)));
      $("#bar-fill").style.width = (p * 100).toFixed(2) + "%";
      $("#bar-fill").parentElement.setAttribute("aria-valuenow", Math.round(p * 100));
      $("#t-start").textContent = hms(c.air_start);
      $("#t-end").textContent = hms(c.air_end);
      $("#t-left").textContent = dur(c.air_end - t);
    },
    strip() {
      const el = $("#strip"), st = this.st;
      const t0 = now() - 300, span = 3 * 3600 + 300, t1 = t0 + span;
      const x = (t) => ((Math.min(Math.max(t, t0), t1) - t0) / span) * 100;
      let html = "";
      for (let k = Math.ceil(t0 / 1800) * 1800; k < t1; k += 1800) {
        html += `<span class="tick num" style="left:${x(k)}%">${hm(k)}</span>`;
      }
      for (const b of st.blocks) {
        if (b.end <= t0 || b.start >= t1) continue;
        const l = x(b.start), w = x(b.end) - l;
        const cls = b.state === "in_onda" ? "in_onda" : (b.kind !== "programma" ? "riempitivo" : "");
        html += `<div class="blk ${cls}" style="left:${l}%;width:calc(${w}% - 2px)" title="${esc(b.title)} ${hms(b.start)}"><span>${w > 6 ? esc(b.kind === "programma" ? b.title : (b.kind === "nero" ? "Nero" : "Riempitivo")) : ""}</span></div>`;
      }
      html += `<span class="nowline" style="left:${x(now())}%"></span>`;
      el.innerHTML = html;
      $("#strip-range").textContent = `${hm(now())} – ${hm(t1)}`;
    },
    init() {
      $("#btn-start").addEventListener("click", async () => { await api("/api/trasmissione", { action: "avvia" }); toast("Trasmissione avviata."); pollStatus(); });
      $("#btn-stop").addEventListener("click", async () => {
        if (!confirm("Fermare la trasmissione? Il server RTMP non riceverà più niente finché non la riavvii.")) return;
        await api("/api/trasmissione", { action: "ferma" }); toast("Trasmissione fermata."); pollStatus();
      });
      $("#btn-restart").addEventListener("click", async () => {
        if (!confirm("Riaprire il collegamento RTMP? La trasmissione si interrompe per qualche secondo e riprende dal punto giusto del palinsesto.")) return;
        await api("/api/trasmissione", { action: "riavvia" }); toast("Collegamento in riapertura.");
      });
      setInterval(() => this.st && this.strip(), 20000);
    },
  };

  // ---------- pagina Palinsesto ----------
  const STATE_BADGE = {
    in_onda: '<span class="badge live">In onda</span>',
    in_coda: '<span class="badge xenon">In arrivo</span>',
    trasmesso: '<span class="badge ok">Trasmesso</span>',
    fuori_orario: '<span class="badge warn">Oltre la mezzanotte, non va in onda</span>',
    non_conforme: '<span class="badge bad">Non conforme, verrà saltato</span>',
    saltato: '<span class="badge bad">Saltato</span>',
    errore: '<span class="badge bad">Errore</span>',
    interrotto: '<span class="badge warn">Interrotto</span>',
    non_trasmesso: '<span class="badge">Non trasmesso</span>',
  };
  const Pal = {
    day: null, today: null, data: null, busy: false, sortable: null,
    async init() {
      const root = $("#pal");
      this.day = root.dataset.day;
      this.today = root.dataset.today;
      $("#prev").addEventListener("click", () => this.go(this.shift(this.day, -1)));
      $("#next").addEventListener("click", () => this.go(this.shift(this.day, 1)));
      $("#pick").value = this.day;
      $("#pick").addEventListener("change", (e) => e.target.value && this.go(e.target.value));
      $("#days").addEventListener("click", (e) => { const a = e.target.closest("a.day"); if (a) { e.preventDefault(); this.go(a.dataset.day); } });
      window.addEventListener("popstate", () => { const d = location.pathname.split("/")[2]; if (d) this.go(d, false); });
      $("#btn-add").addEventListener("click", () => this.addUrls());
      $("#btn-clear").addEventListener("click", () => this.clear());
      $("#btn-copy").addEventListener("click", () => Copy.open());
      let st;
      $("#search").addEventListener("input", () => { clearTimeout(st); st = setTimeout(() => this.search(), 250); });
      $("#results").addEventListener("click", (e) => { const b = e.target.closest("button[data-url]"); if (b) this.addOne(b.dataset.url, b); });
      $("#list").addEventListener("click", (e) => this.onListClick(e));
      $("#list").addEventListener("change", (e) => { if (e.target.matches("input.title")) this.rename(e.target); });
      $("#list").addEventListener("keydown", (e) => { if (e.target.matches("input.title") && e.key === "Enter") e.target.blur(); });
      this.sortable = new Sortable($("#list"), {
        handle: ".grip", animation: 150, draggable: ".item", filter: ".locked",
        onStart: () => (this.busy = true),
        onEnd: () => { this.busy = false; this.saveOrder(); },
      });
      await Promise.all([this.load(), this.loadWeek(), this.search()]);
      setInterval(() => {
        const editing = document.activeElement && document.activeElement.matches("input.title");
        if (!this.busy && !editing && this.day >= this.shift(this.today, -1) && !$("#dlg-copy").open) this.load(true);
      }, 15000);
    },
    shift(d, n) { const x = new Date(d + "T12:00:00Z"); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); },
    async go(d, push = true) {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) return;
      this.day = d;
      $("#pick").value = d;
      if (push) history.pushState({}, "", "/palinsesto/" + d);
      await Promise.all([this.load(), this.loadWeek()]);
    },
    async loadWeek() {
      const w = await api("/api/settimana/" + this.day);
      this.today = w.today;
      $("#days").innerHTML = w.days.map((d) => {
        const fill = Math.min(1, d.total / 86400) * 100;
        const cls = ["day", d.day === w.today ? "is-today" : "", d.day < w.today ? "past" : ""].join(" ");
        const title = `${d.count} video, ${dur(d.total)}${d.bad ? `, ${d.bad} non conformi` : ""}`;
        return `<a class="${cls}" href="/palinsesto/${d.day}" data-day="${d.day}" ${d.day === this.day ? 'aria-current="date"' : ""} title="${title}">
          <span class="wd">${d.day === w.today ? "oggi" : esc(d.short.split(" ")[0])}</span><span class="dn">${d.short.split(" ")[1]}</span>
          <span class="fill${d.bad ? " bad" : ""}"><i style="width:${fill}%"></i></span></a>`;
      }).join("");
      const cur = $('#days [aria-current="date"]'), strip = $("#days");
      if (cur) strip.scrollLeft = cur.offsetLeft - strip.offsetLeft - (strip.clientWidth - cur.offsetWidth) / 2;
    },
    async load(quiet = false) {
      try {
        this.data = await api("/api/giorno/" + this.day);
        this.render();
        if (!quiet && this.data.day === this.data.today) {
          const cur = $("#list .item.in_onda") || $("#list .item.in_coda") || $("#list .item.previsto");
          const first = $("#list .item");
          if (cur && cur !== first) cur.scrollIntoView({ block: "center" });
        }
      } catch (e) { if (!quiet) toast(e.message, true); }
    },
    render() {
      const d = this.data, past = d.past;
      $("#day-title").textContent = d.label.charAt(0).toUpperCase() + d.label.slice(1) + (d.day === d.today ? ", oggi" : "");
      document.title = d.label + " · Palinsesto";
      const items = d.items;
      const ok = items.filter((i) => i.playable).length;
      $("#facts").innerHTML = items.length
        ? `<b>${items.length}</b> video, durata totale <b class="num">${dur(d.total)}</b>${ok < items.length ? `, <b>${items.length - ok}</b> ${items.length - ok === 1 ? "non conforme" : "non conformi"}` : ""}`
        : "Nessun video in programma";
      const notes = [];
      if (past) notes.push(`<div class="note">Giornata passata: vedi cosa è andato in onda. Puoi copiarla su altri giorni.</div>`);
      if (d.late_start > 1) notes.push(`<div class="note">La giornata parte alle <b class="num">${hms(d.anchor)}</b> perché il video precedente finisce dopo mezzanotte.</div>`);
      const out = items.filter((i) => i.state === "fuori_orario").length;
      if (out) notes.push(`<div class="note warn">${out === 1 ? "Un video non entra" : out + " video non entrano"} nella giornata: a mezzanotte parte il palinsesto del giorno dopo.</div>`);
      else if (d.spill) notes.push(`<div class="note">L'ultimo video finisce alle <b class="num">${hms(d.spill)}</b> del giorno dopo: il palinsesto successivo partirà a quell'ora.</div>`);
      if (!past && items.length && d.total < 86400 && !out && !d.spill) {
        const lastEnd = items.filter((i) => i.end).map((i) => i.end).pop();
        if (lastEnd) notes.push(`<div class="note">Dalle <b class="num">${hms(lastEnd)}</b> a mezzanotte va in onda il video di riempimento.</div>`);
      }
      $("#notes").innerHTML = notes.join("");
      $("#btn-clear").hidden = past || !items.length;
      $("#btn-copy").disabled = !items.length;
      $("#add").hidden = past;
      this.sortable.option("disabled", past);

      if (!items.length) {
        $("#list").innerHTML = `<div class="empty"><b>${past ? "Nessun video in questa giornata" : "La giornata è vuota"}</b>${past ? "" : "Aggiungi i video qui sotto, oppure copia un'altra giornata. Senza video va in onda il riempitivo."}</div>`;
        return;
      }
      $("#list").innerHTML = items.map((it) => {
        const locked = past || ["in_onda", "trasmesso"].includes(it.state);
        const info = it.info || {};
        const kbps = info.kbps ? `<span class="num">${info.kbps} kbps</span>` : "";
        const issues = !it.playable && it.issues && it.issues.length ? `<div class="issues">${esc(it.issues.join(" "))}</div>` : (it.note ? `<div class="issues">${esc(it.note)}</div>` : "");
        return `<div class="item ${it.state}${locked ? " locked" : ""}" data-id="${it.id}">
          <span class="grip" title="Trascina per spostare" aria-hidden="true">${past ? "" : icon("grip")}</span>
          <div class="times num"><b>${hms(it.start)}</b><span>${it.end ? hms(it.end) : ""}</span></div>
          <div class="what">
            <input class="title" value="${esc(it.title)}" aria-label="Titolo" ${past ? "readonly" : ""} data-old="${esc(it.title)}">
            <div class="meta">${STATE_BADGE[it.state] || ""}${kbps}<span class="url" title="${esc(it.url)}">${esc(decodeURIComponent(it.url.split("/").pop()))}</span></div>
            ${issues}
          </div>
          <div class="dur num">${dur(it.duration)}</div>
          <div class="acts">${past ? "" : `
            <button class="icon-btn" data-act="check" title="Ricontrolla il video" aria-label="Ricontrolla ${esc(it.title)}">${icon("check")}</button>
            <button class="icon-btn danger" data-act="del" title="Togli dalla giornata" aria-label="Togli ${esc(it.title)}">${icon("trash")}</button>`}
          </div></div>`;
      }).join("");
    },
    async onListClick(e) {
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      const row = b.closest(".item"), id = row.dataset.id;
      const title = $("input.title", row).value;
      if (b.dataset.act === "del") {
        const live = row.classList.contains("in_onda") || row.classList.contains("in_coda");
        const msg = live ? `«${title}» è già in onda o sta per andarci: toglierlo dal palinsesto non lo interrompe. Toglierlo comunque?` : `Togliere «${title}» da questa giornata?`;
        if (!confirm(msg)) return;
        try { this.data = await api(`/api/voce/${id}/elimina`, {}); this.render(); this.loadWeek(); toast("Video tolto."); }
        catch (err) { toast(err.message, true); }
      } else if (b.dataset.act === "check") {
        b.disabled = true; toast("Controllo del video in corso…");
        try { this.data = await api(`/api/voce/${id}/verifica`, {}); this.render(); this.loadWeek(); toast("Video ricontrollato."); }
        catch (err) { toast(err.message, true); b.disabled = false; }
      }
    },
    async rename(input) {
      const row = input.closest(".item"), t = input.value.trim();
      if (!t) { input.value = input.dataset.old; return; }
      if (t === input.dataset.old) return;
      try { await api(`/api/voce/${row.dataset.id}/titolo`, { title: t }); input.dataset.old = t; toast("Titolo salvato."); }
      catch (err) { toast(err.message, true); input.value = input.dataset.old; }
    },
    async saveOrder() {
      const ids = $$("#list .item").map((r) => r.dataset.id);
      try { this.data = await api(`/api/giorno/${this.day}/ordine`, { ids }); this.render(); toast("Ordine salvato."); }
      catch (err) { toast(err.message, true); this.load(); }
    },
    async addUrls() {
      const box = $("#urls");
      const lines = box.value.split(/\n+/).map((l) => l.trim()).filter(Boolean);
      if (!lines.length) { box.focus(); return; }
      const btn = $("#btn-add"), prog = $("#add-progress"), status = $("#add-status");
      btn.disabled = true; prog.innerHTML = "";
      const failed = [];
      for (let i = 0; i < lines.length; i++) {
        status.textContent = `Controllo ${i + 1} di ${lines.length}…`;
        const li = document.createElement("li");
        li.textContent = "Controllo " + lines[i];
        prog.appendChild(li);
        try {
          const r = await api(`/api/giorno/${this.day}/aggiungi`, { url: lines[i] });
          li.className = r.conforme ? "ok" : "bad";
          li.textContent = r.conforme ? `Aggiunto: ${r.title}` : `Aggiunto ma non conforme, verrà saltato: ${r.title}. ${r.issues.join(" ")}`;
        } catch (err) {
          failed.push(lines[i]);
          li.className = "bad";
          li.textContent = `Non aggiunto: ${lines[i]}. ${err.message}`;
        }
      }
      box.value = failed.join("\n");
      status.textContent = failed.length ? `${failed.length} indirizzi non aggiunti: sono rimasti nel riquadro.` : "Fatto.";
      btn.disabled = false;
      await Promise.all([this.load(), this.loadWeek(), this.search()]);
    },
    async addOne(url, btn) {
      btn.disabled = true;
      try {
        const r = await api(`/api/giorno/${this.day}/aggiungi`, { url });
        toast(r.conforme ? `Aggiunto in fondo: ${r.title}` : `Aggiunto ma non conforme: ${r.title}`, !r.conforme);
        await Promise.all([this.load(), this.loadWeek()]);
      } catch (err) { toast(err.message, true); }
      btn.disabled = false;
    },
    async search() {
      const q = $("#search").value.trim();
      const r = await api("/api/video/cerca", { q });
      $("#results").innerHTML = r.results.length
        ? r.results.map((m) => `<li><div class="st">${esc(m.title)}<div class="sd num">${dur(m.duration)}${m.ok ? "" : " · non conforme"}</div></div>
            <button class="btn" data-url="${esc(m.url)}" style="min-height:36px">Aggiungi</button></li>`).join("")
        : `<li><span class="muted small">${q ? "Nessun video trovato." : "I video che aggiungi compariranno qui, per riusarli in altri giorni."}</span></li>`;
    },
    async clear() {
      if (!confirm(`Togliere tutti i video di ${this.data.label}? Al loro posto andrà in onda il riempitivo.`)) return;
      try { this.data = await api(`/api/giorno/${this.day}/svuota`, {}); this.render(); this.loadWeek(); toast("Giornata svuotata."); }
      catch (err) { toast(err.message, true); }
    },
  };

  // ---------- finestra Copia su altri giorni ----------
  const MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"];
  const Copy = {
    sel: new Set(), month: null, counts: {},
    open() {
      const dlg = $("#dlg-copy");
      this.sel = new Set();
      const start = Pal.shift(Pal.today, 1) > Pal.day ? Pal.shift(Pal.today, 1) : Pal.shift(Pal.day, 1);
      this.month = start.slice(0, 7);
      $("#copy-src").textContent = Pal.data.label;
      if (!dlg.dataset.ready) {
        dlg.dataset.ready = "1";
        $("#cal-prev").addEventListener("click", () => this.moveMonth(-1));
        $("#cal-next").addEventListener("click", () => this.moveMonth(1));
        $("#cal").addEventListener("click", (e) => {
          const b = e.target.closest("button[data-d]"); if (!b) return;
          this.sel.has(b.dataset.d) ? this.sel.delete(b.dataset.d) : this.sel.add(b.dataset.d);
          this.draw();
        });
        dlg.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) dlg.close(); });
        $$("[data-quick]", dlg).forEach((b) => b.addEventListener("click", () => this.quick(b.dataset.quick)));
        $("#copy-go").addEventListener("click", () => this.go());
      }
      this.loadMonth().then(() => dlg.showModal());
    },
    moveMonth(n) {
      const [y, m] = this.month.split("-").map(Number);
      const d = new Date(Date.UTC(y, m - 1 + n, 1));
      this.month = d.toISOString().slice(0, 7);
      this.loadMonth();
    },
    async loadMonth() {
      try { this.counts = (await api("/api/mese/" + this.month)).counts; } catch (e) { this.counts = {}; }
      this.draw();
    },
    minDay() { return Pal.shift(Pal.today, 0); },
    draw() {
      const [y, m] = this.month.split("-").map(Number);
      $("#cal-month").textContent = `${MESI[m - 1]} ${y}`;
      const first = new Date(Date.UTC(y, m - 1, 1));
      const lead = (first.getUTCDay() + 6) % 7;
      const days = new Date(Date.UTC(y, m, 0)).getUTCDate();
      let html = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"].map((d) => `<span class="h">${d}</span>`).join("");
      for (let i = 0; i < lead; i++) html += `<span class="pad"></span>`;
      for (let d = 1; d <= days; d++) {
        const iso = `${this.month}-${String(d).padStart(2, "0")}`;
        const dis = iso < this.minDay() || iso === Pal.day;
        const n = this.counts[iso];
        html += `<button type="button" data-d="${iso}" aria-pressed="${this.sel.has(iso)}" ${dis ? "disabled" : ""} class="${n ? "filled" : ""}"
          aria-label="${d} ${MESI[m - 1]}${n ? `, ha già ${n} video` : ""}">${d}${n ? `<small>${n}</small>` : ""}</button>`;
      }
      $("#cal").innerHTML = html;
      const n = this.sel.size;
      $("#copy-count").textContent = n ? `${n} ${n === 1 ? "giorno scelto" : "giorni scelti"}` : "Nessun giorno scelto";
      $("#copy-go").disabled = !n;
    },
    quick(kind) {
      if (kind === "none") this.sel.clear();
      if (kind === "week") for (let k = 1; k <= 4; k++) this.sel.add(Pal.shift(Pal.day, 7 * k));
      if (kind === "next7") for (let k = 1; k <= 7; k++) { const d = Pal.shift(Pal.day, k); if (d >= this.minDay()) this.sel.add(d); }
      this.draw();
    },
    async go() {
      const days = Array.from(this.sel).sort();
      const replacing = days.filter((d) => this.counts[d]).length;
      if (replacing && !confirm(`${replacing === 1 ? "Un giorno ha" : replacing + " giorni hanno"} già un palinsesto che verrà sostituito. Continuare?`)) return;
      try {
        const r = await api(`/api/giorno/${Pal.day}/copia`, { days });
        $("#dlg-copy").close();
        toast(`Copiato su ${r.copied.length} ${r.copied.length === 1 ? "giorno" : "giorni"}.`);
        Pal.loadWeek();
      } catch (err) { toast(err.message, true); }
    },
  };

  // ---------- Impostazioni: copie di sicurezza ----------
  function initSettings() {
    const now_ = $("#bk-now"), list = $("#bk-list");
    if (now_) now_.addEventListener("click", async () => {
      now_.disabled = true;
      try { const r = await api("/api/copie/ora", {}); toast(`Copia salvata: ${r.name}`); }
      catch (e) { toast(e.message, true); }
      now_.disabled = false;
    });
    if (list) list.addEventListener("click", async () => {
      const ul = $("#bk-files");
      ul.innerHTML = "<li class='muted'>Lettura…</li>";
      try {
        const r = await api("/api/copie/elenco");
        ul.innerHTML = r.files.length ? r.files.slice(0, 30).map((f) => `<li><span style="flex:1">${esc(f.name)}</span><span class="muted small num">${Math.max(1, Math.round(f.size / 1024))} kB</span>
          <button class="btn danger" data-restore="${esc(f.name)}" style="min-height:34px">Ripristina</button></li>`).join("")
          : "<li class='muted'>Nessuna copia ancora salvata.</li>";
      } catch (e) { ul.innerHTML = ""; toast(e.message, true); }
    });
    const files = $("#bk-files");
    if (files) files.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-restore]"); if (!b) return;
      if (!confirm(`Ripristinare «${b.dataset.restore}»? Palinsesti, impostazioni e utenti attuali verranno sostituiti.`)) return;
      try { await api("/api/copie/ripristina", { name: b.dataset.restore }); toast("Copia ripristinata."); setTimeout(() => location.reload(), 1200); }
      catch (err) { toast(err.message, true); }
    });
  }

  // ---------- Storico: schede ----------
  function initHistory() {
    $$("[data-tab]").forEach((b) => b.addEventListener("click", () => {
      $$("[data-tab]").forEach((x) => x.setAttribute("aria-selected", x === b ? "true" : "false"));
      $$("[role=tabpanel]").forEach((p) => (p.hidden = p.id !== b.dataset.tab));
    }));
  }

  // ---------- avvio ----------
  if ($("#tally")) {
    tickClock();
    setInterval(tickClock, 1000);
    pollStatus();
    setInterval(pollStatus, page === "onair" ? 3000 : 10000);
  }
  if (page === "onair") OnAir.init();
  if (page === "palinsesto") Pal.init();
  if (page === "impostazioni") initSettings();
  if (page === "storico") initHistory();
})();
