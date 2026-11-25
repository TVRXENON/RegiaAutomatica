"""Pagina web di gestione della regia automatica."""
import json
import os
import secrets
import time
import uuid
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template, request,
                   session, url_for, flash)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from . import backup, db, media, schedule

def _asset_version():
    import hashlib
    h = hashlib.sha1()
    folder = os.path.join(os.path.dirname(__file__), "static")
    for name in sorted(os.listdir(folder)):
        with open(os.path.join(folder, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:10]


ASSET_VERSION = _asset_version()
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
        "settembre", "ottobre", "novembre", "dicembre"]


def day_label(d, short=False):
    if isinstance(d, str):
        d = date.fromisoformat(d)
    if short:
        return f"{GIORNI[d.weekday()][:3]} {d.day}"
    return f"{GIORNI[d.weekday()]} {d.day} {MESI[d.month - 1]}"


def secret_key():
    path = os.path.join(db.DATA_DIR, "secret.key")
    os.makedirs(db.DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
        except FileExistsError:
            time.sleep(0.2)
    with open(path) as f:
        return f.read().strip()


def create_app():
    db.init()
    # la versione è nel percorso: la CDN di Bunny ignora i parametri dopo il punto di domanda
    app = Flask(__name__, static_url_path=f"/static/{ASSET_VERSION}")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(
        SECRET_KEY=secret_key(),
        SESSION_COOKIE_NAME="regia",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=60),
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    )
    bootstrap_admin()

    # ---------- servizio ----------

    @app.before_request
    def open_db():
        g.con = db.connect()

    @app.teardown_request
    def close_db(exc):
        con = g.pop("con", None)
        if con is not None:
            con.close()

    @app.after_request
    def no_cache(resp):
        if not request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"
        else:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    @app.context_processor
    def common():
        return {"station": db.setting(g.con, "station_name", "TVR Xenon"),
                "user": current_user(), "csrf": csrf_token(), "asset_version": ASSET_VERSION}

    def current_user():
        if "uid" not in session:
            return None
        if "user" not in g:
            g.user = g.con.execute("SELECT id, username, name FROM users WHERE id=?",
                                   (session["uid"],)).fetchone()
        return g.user

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def check_csrf():
        token = request.headers.get("X-CSRF") or request.form.get("csrf")
        if not token or token != session.get("csrf"):
            abort(400, "Sessione scaduta: ricarica la pagina.")

    def login_required(api=False):
        def deco(fn):
            @wraps(fn)
            def wrapper(*a, **kw):
                if not current_user():
                    session.clear()
                    if api or request.path.startswith("/api/"):
                        return jsonify({"error": "Accesso scaduto: entra di nuovo."}), 401
                    return redirect(url_for("login", next=request.path))
                if request.method == "POST":
                    check_csrf()
                return fn(*a, **kw)
            return wrapper
        return deco

    @app.errorhandler(400)
    def bad_request(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": e.description}), 400
        flash(e.description, "errore")
        return redirect(request.referrer or url_for("onair"))

    # ---------- accesso ----------

    @app.route("/accesso", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            check_csrf()
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            u = g.con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if u and check_password_hash(u["pw_hash"], password):
                remember = request.form.get("remember") == "1"
                session.clear()
                session["uid"] = u["id"]
                session.permanent = remember
                nxt = request.args.get("next") or ""
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("onair"))
            time.sleep(1)
            error = "Nome utente o password non corretti."
        return render_template("login.html", error=error)

    @app.route("/esci", methods=["POST"])
    def logout():
        check_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.route("/salute")
    def health():
        st = db.get_state(g.con, "engine") or {}
        return jsonify({"web": "ok", "motore": st.get("code"), "battito": st.get("heartbeat")})

    # ---------- pagine ----------

    @app.route("/")
    @login_required()
    def onair():
        return render_template("onair.html", page="onair")

    @app.route("/palinsesto")
    @app.route("/palinsesto/<day>")
    @login_required()
    def palinsesto(day=None):
        try:
            d = date.fromisoformat(day) if day else db.today()
        except ValueError:
            return redirect(url_for("palinsesto"))
        return render_template("palinsesto.html", page="palinsesto", day=d.isoformat(),
                               today=db.today().isoformat())

    @app.route("/storico")
    @login_required()
    def storico():
        days = []
        for n in (1, 2, 3):
            d = db.today() - timedelta(days=n)
            recs = schedule.records(g.con, since=db.midnight(d), until=db.next_midnight(d))
            rows = []
            for r in recs:
                prev = rows[-1] if rows else None
                if (prev and r["kind"] in ("riempitivo", "nero") and prev["kind"] == r["kind"]
                        and r["status"] == "ok" and prev["status"] == "ok" and abs(r["air_start"] - prev["air_end"]) < 2):
                    prev["air_end"] = r["air_end"]
                    prev["loops"] += 1
                else:
                    r["loops"] = 1
                    rows.append(r)
            days.append({"day": d.isoformat(), "label": day_label(d), "rows": rows,
                         "programmi": sum(1 for r in recs if r["kind"] == "programma" and r["status"] == "ok"),
                         "problemi": sum(1 for r in recs if r["status"] in ("errore", "saltato", "interrotto"))})
        return render_template("storico.html", page="storico", days=days)

    @app.route("/impostazioni", methods=["GET", "POST"])
    @login_required()
    def impostazioni():
        con = g.con
        if request.method == "POST":
            f = request.form
            section = f.get("section")
            old = db.settings(con)
            if section == "trasmissione":
                url = (f.get("rtmp_url") or "").strip()
                if url and not url.startswith(("rtmp://", "rtmps://")):
                    flash("L'indirizzo RTMP deve iniziare con rtmp:// o rtmps://.", "errore")
                    return redirect(url_for("impostazioni"))
                db.set_setting(con, "station_name", (f.get("station_name") or "").strip() or "TVR Xenon")
                db.set_setting(con, "rtmp_url", url)
                db.set_setting(con, "rtmp_key", (f.get("rtmp_key") or "").strip())
                db.set_setting(con, "realign_hour", str(max(0, min(23, int(f.get("realign_hour") or 5)))))
                if url != old["rtmp_url"] or (f.get("rtmp_key") or "").strip() != old["rtmp_key"]:
                    db.send_command(con, "restart")
                    flash("Indirizzo di trasmissione salvato: il collegamento si riapre tra pochi secondi.", "ok")
                else:
                    flash("Impostazioni salvate.", "ok")
            elif section == "riempitivo":
                url = media.normalize_url(f.get("filler_url") or "")
                if not url:
                    db.set_setting(con, "filler_url", "")
                    flash("Video di riempimento rimosso: quando non c'è niente in programma andrà in onda lo schermo nero.", "errore")
                else:
                    row, err = media.check(con, url, refresh=True)
                    if err:
                        flash(err, "errore")
                        return redirect(url_for("impostazioni"))
                    db.set_setting(con, "filler_url", url)
                    if row["ok"]:
                        flash("Video di riempimento salvato e verificato.", "ok")
                    else:
                        flash("Video salvato ma non conforme: " + " ".join(json.loads(row["issues"])), "errore")
            elif section == "formato":
                try:
                    vals = {k: str(int(float((f.get(k) or "0").replace(",", ".")))) for k in
                            ("max_kbps", "width", "height", "fps", "audio_rate")}
                except ValueError:
                    flash("Controlla i valori del formato: servono numeri.", "errore")
                    return redirect(url_for("impostazioni"))
                for k, v in vals.items():
                    db.set_setting(con, k, v)
                media.recheck_all(con)
                flash("Formato salvato. Tutti i video già inseriti sono stati ricontrollati.", "ok")
            elif section == "storage":
                db.set_setting(con, "storage_host", f.get("storage_host") or "storage.bunnycdn.com")
                db.set_setting(con, "storage_zone", (f.get("storage_zone") or "").strip())
                if (f.get("storage_password") or "").strip():
                    db.set_setting(con, "storage_password", f.get("storage_password").strip())
                db.set_setting(con, "backup_folder", (f.get("backup_folder") or "regia-backup").strip().strip("/"))
                try:
                    if backup.configured(db.settings(con)):
                        name = backup.backup_now(con)
                        flash(f"Collegamento a Bunny Storage riuscito: salvata la copia «{name}».", "ok")
                    else:
                        flash("Impostazioni salvate.", "ok")
                except Exception as e:  # noqa: BLE001
                    flash(f"Bunny Storage non risponde come previsto: {e}", "errore")
            return redirect(url_for("impostazioni"))

        s = db.settings(con)
        filler = None
        if s.get("filler_url"):
            r = con.execute("SELECT * FROM media WHERE url=?", (s["filler_url"],)).fetchone()
            if r:
                filler = dict(r)
                filler["issues"] = json.loads(filler["issues"])
                filler["info"] = json.loads(filler["info"])
        return render_template("impostazioni.html", page="impostazioni", s=s, filler=filler,
                               regions=backup.REGIONS, storage_ok=backup.configured(s))

    @app.route("/utenti", methods=["GET", "POST"])
    @login_required()
    def utenti():
        con = g.con
        if request.method == "POST":
            f = request.form
            action = f.get("action")
            if action == "nuovo":
                username = (f.get("username") or "").strip()
                name = (f.get("name") or "").strip() or username
                pw = f.get("password") or ""
                if len(username) < 3 or len(pw) < 8:
                    flash("Il nome utente deve avere almeno 3 caratteri e la password almeno 8.", "errore")
                elif con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                    flash(f"Il nome utente «{username}» esiste già.", "errore")
                else:
                    con.execute("INSERT INTO users(username, name, pw_hash, created_at) VALUES(?,?,?,?)",
                                (username, name, generate_password_hash(pw), time.time()))
                    db.bump(con)
                    flash(f"Utente {name} creato.", "ok")
            elif action == "password":
                uid = int(f.get("uid") or 0)
                pw = f.get("password") or ""
                if len(pw) < 8:
                    flash("La password deve avere almeno 8 caratteri.", "errore")
                else:
                    con.execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(pw), uid))
                    db.bump(con)
                    flash("Password cambiata.", "ok")
            elif action == "elimina":
                uid = int(f.get("uid") or 0)
                if uid == session.get("uid"):
                    flash("Non puoi eliminare l'utente con cui sei entrato.", "errore")
                elif con.execute("SELECT COUNT(*) FROM users").fetchone()[0] <= 1:
                    flash("Deve restare almeno un utente.", "errore")
                else:
                    con.execute("DELETE FROM users WHERE id=?", (uid,))
                    db.bump(con)
                    flash("Utente eliminato.", "ok")
            return redirect(url_for("utenti"))
        users = con.execute("SELECT id, username, name, created_at FROM users ORDER BY name COLLATE NOCASE").fetchall()
        return render_template("utenti.html", page="utenti", users=users)

    # ---------- API: stato ----------

    def engine_state():
        st = db.get_state(g.con, "engine") or {}
        if not st or time.time() - (st.get("heartbeat") or 0) > 20:
            st = {"code": "motore_fermo",
                  "message": "Il motore di messa in onda non risponde. Se dura più di un minuto, riavvia il container da Bunny."}
        return st

    def item_json(it):
        return {k: it.get(k) for k in ("id", "title", "url", "duration", "start", "end", "state",
                                       "issues", "info", "note", "playable", "position")}

    @app.route("/api/stato")
    @login_required(api=True)
    def api_stato():
        con, now = g.con, time.time()
        s = db.settings(con)
        st = engine_state()
        today = db.today()
        current = con.execute("SELECT * FROM aired WHERE air_start <= ? AND air_end > ? AND status IN ('ok') "
                              "ORDER BY air_start DESC LIMIT 1", (now, now)).fetchone()
        blocks = []
        if current and st["code"] == "in_onda":
            blocks.append({"title": current["title"], "kind": current["kind"], "start": current["air_start"],
                           "end": current["air_end"], "state": "in_onda"})
        horizon = now + 6 * 3600
        if st["code"] == "in_onda":
            for r in con.execute("SELECT * FROM aired WHERE air_start > ? AND status='ok' ORDER BY air_start", (now,)):
                blocks.append({"title": r["title"], "kind": r["kind"], "start": r["air_start"],
                               "end": r["air_end"], "state": "in_coda"})
            t = blocks[-1]["end"] if blocks else now
            for d in (today, today + timedelta(days=1)):
                if t > horizon:
                    break
                tl = schedule.timeline(con, d, now)
                for it in tl["items"]:
                    if it["state"] == "previsto" and it["start"] is not None and it["start"] >= t - 1:
                        blocks.append({"title": it["title"], "kind": "programma", "start": it["start"],
                                       "end": it["end"], "state": "previsto"})
                        t = it["end"]
                if t < tl["end_of"]:
                    blocks.append({"title": "Riempitivo" if s.get("filler_url") else "Schermo nero",
                                   "kind": "riempitivo", "start": t, "end": tl["end_of"], "state": "previsto"})
                    t = tl["end_of"]
        warnings = collect_warnings(con, s, today)
        return jsonify({"now": now, "engine": st, "enabled": s.get("broadcast_enabled") == "1",
                        "current": dict(current) if current and st["code"] == "in_onda" else None,
                        "blocks": [b for b in blocks if b["end"] > now][:40],
                        "warnings": warnings, "rtmp": bool(s.get("rtmp_url"))})

    def collect_warnings(con, s, today):
        w = []
        if not s.get("rtmp_url"):
            w.append({"level": "errore", "text": "Manca l'indirizzo RTMP di destinazione.", "link": "/impostazioni"})
        if not s.get("filler_url"):
            w.append({"level": "errore", "text": "Manca il video di riempimento: nei vuoti va in onda lo schermo nero.",
                      "link": "/impostazioni"})
        else:
            r = con.execute("SELECT ok FROM media WHERE url=?", (s["filler_url"],)).fetchone()
            if not r or not r["ok"]:
                w.append({"level": "errore", "text": "Il video di riempimento non è conforme al formato richiesto.",
                          "link": "/impostazioni"})
        for d in (today, today + timedelta(days=1)):
            items = schedule.day_items(con, d)
            bad = [i for i in items if not i["playable"]]
            label = "oggi" if d == today else "domani"
            if not items and d != today:
                w.append({"level": "avviso", "text": f"Il palinsesto di {label} è vuoto.",
                          "link": f"/palinsesto/{d.isoformat()}"})
            if bad:
                n = len(bad)
                text = (f"Un video di {label} non è conforme e verrà saltato." if n == 1
                        else f"{n} video di {label} non sono conformi e verranno saltati.")
                w.append({"level": "avviso", "text": text,
                          "link": f"/palinsesto/{d.isoformat()}"})
        if not backup.configured(s):
            w.append({"level": "avviso", "text": "Le copie di sicurezza su Bunny Storage non sono attive.",
                      "link": "/impostazioni#copie"})
        elif s.get("last_backup_error"):
            w.append({"level": "avviso", "text": "Ultima copia di sicurezza non riuscita: " + s["last_backup_error"],
                      "link": "/impostazioni#copie"})
        return w

    @app.route("/api/trasmissione", methods=["POST"])
    @login_required(api=True)
    def api_trasmissione():
        action = (request.get_json(silent=True) or {}).get("action")
        if action == "avvia":
            db.set_setting(g.con, "broadcast_enabled", "1")
        elif action == "ferma":
            db.set_setting(g.con, "broadcast_enabled", "0")
        elif action == "riavvia":
            db.send_command(g.con, "restart")
        else:
            abort(400, "Azione sconosciuta.")
        return jsonify({"ok": True})

    # ---------- API: palinsesto ----------

    def parse_day(day):
        try:
            return date.fromisoformat(day)
        except ValueError:
            abort(400, "Data non valida.")

    def day_payload(d):
        con, now = g.con, time.time()
        tl = schedule.timeline(con, d, now)
        today = db.today()
        return {"day": d.isoformat(), "label": day_label(d), "today": today.isoformat(), "now": now,
                "start_of": tl["start_of"], "end_of": tl["end_of"], "anchor": tl["anchor"],
                "late_start": tl["late_start"], "spill": tl["spill"], "total": tl["total"],
                "past": d < today, "items": [item_json(it) for it in tl["items"]]}

    @app.route("/api/giorno/<day>")
    @login_required(api=True)
    def api_giorno(day):
        return jsonify(day_payload(parse_day(day)))

    @app.route("/api/settimana/<day>")
    @login_required(api=True)
    def api_settimana(day):
        d0 = parse_day(day)
        out = []
        for n in range(-3, 18):
            d = d0 + timedelta(days=n)
            items = schedule.day_items(g.con, d)
            out.append({"day": d.isoformat(), "short": day_label(d, short=True), "count": len(items),
                        "total": sum(i["duration"] for i in items if i["playable"]),
                        "bad": sum(1 for i in items if not i["playable"]),
                        "weekday": d.weekday(), "month": MESI[d.month - 1]})
        return jsonify({"days": out, "today": db.today().isoformat()})

    @app.route("/api/mese/<ym>")
    @login_required(api=True)
    def api_mese(ym):
        try:
            first = date.fromisoformat(ym + "-01")
        except ValueError:
            abort(400, "Mese non valido.")
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        rows = g.con.execute("SELECT day, COUNT(*) AS n FROM items WHERE day >= ? AND day < ? GROUP BY day",
                             (first.isoformat(), nxt.isoformat())).fetchall()
        return jsonify({"counts": {r["day"]: r["n"] for r in rows}})

    def renumber(con, day, ids):
        for pos, iid in enumerate(ids):
            con.execute("UPDATE items SET position=? WHERE id=? AND day=?", (pos, iid, str(day)))

    @app.route("/api/giorno/<day>/aggiungi", methods=["POST"])
    @login_required(api=True)
    def api_aggiungi(day):
        d = parse_day(day)
        data = request.get_json(silent=True) or {}
        row, err = media.check(g.con, data.get("url") or "", refresh=bool(data.get("refresh")))
        if err:
            return jsonify({"error": err}), 422
        after = data.get("after")
        con = g.con
        ids = [r["id"] for r in con.execute("SELECT id FROM items WHERE day=? ORDER BY position", (str(d),))]
        new_id = uuid.uuid4().hex
        title = (data.get("title") or "").strip() or row["title"]
        con.execute("INSERT INTO items(id, day, position, url, title) VALUES(?,?,?,?,?)",
                    (new_id, str(d), len(ids), row["url"], title))
        if after and after in ids:
            ids.insert(ids.index(after) + 1, new_id)
        else:
            ids.append(new_id)
        renumber(con, d, ids)
        db.bump(con)
        return jsonify({"ok": True, "id": new_id, "conforme": bool(row["ok"]),
                        "issues": json.loads(row["issues"]), "title": title})

    @app.route("/api/giorno/<day>/ordine", methods=["POST"])
    @login_required(api=True)
    def api_ordine(day):
        d = parse_day(day)
        ids = (request.get_json(silent=True) or {}).get("ids") or []
        existing = {r["id"] for r in g.con.execute("SELECT id FROM items WHERE day=?", (str(d),))}
        if set(ids) != existing:
            abort(400, "La lista è cambiata nel frattempo: ricarica la pagina.")
        renumber(g.con, d, ids)
        db.bump(g.con)
        return jsonify(day_payload(d))

    @app.route("/api/voce/<iid>/titolo", methods=["POST"])
    @login_required(api=True)
    def api_titolo(iid):
        title = ((request.get_json(silent=True) or {}).get("title") or "").strip()
        if not title:
            abort(400, "Il titolo non può essere vuoto.")
        g.con.execute("UPDATE items SET title=? WHERE id=?", (title[:200], iid))
        db.bump(g.con)
        return jsonify({"ok": True})

    @app.route("/api/voce/<iid>/elimina", methods=["POST"])
    @login_required(api=True)
    def api_elimina(iid):
        r = g.con.execute("SELECT day FROM items WHERE id=?", (iid,)).fetchone()
        if not r:
            abort(400, "Video già eliminato.")
        g.con.execute("DELETE FROM items WHERE id=?", (iid,))
        ids = [x["id"] for x in g.con.execute("SELECT id FROM items WHERE day=? ORDER BY position", (r["day"],))]
        renumber(g.con, r["day"], ids)
        db.bump(g.con)
        return jsonify(day_payload(date.fromisoformat(r["day"])))

    @app.route("/api/voce/<iid>/verifica", methods=["POST"])
    @login_required(api=True)
    def api_verifica(iid):
        r = g.con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        if not r:
            abort(400, "Video non trovato.")
        row, err = media.check(g.con, r["url"], refresh=True)
        if err:
            return jsonify({"error": err}), 422
        return jsonify(day_payload(date.fromisoformat(r["day"])))

    @app.route("/api/giorno/<day>/svuota", methods=["POST"])
    @login_required(api=True)
    def api_svuota(day):
        d = parse_day(day)
        g.con.execute("DELETE FROM items WHERE day=?", (str(d),))
        db.bump(g.con)
        return jsonify(day_payload(d))

    @app.route("/api/giorno/<day>/copia", methods=["POST"])
    @login_required(api=True)
    def api_copia(day):
        d = parse_day(day)
        targets = (request.get_json(silent=True) or {}).get("days") or []
        src = g.con.execute("SELECT url, title FROM items WHERE day=? ORDER BY position", (str(d),)).fetchall()
        if not src:
            abort(400, "La giornata da copiare è vuota.")
        done = []
        g.con.execute("BEGIN")
        try:
            for t in targets:
                td = parse_day(t)
                if td == d or td < db.today():
                    continue
                g.con.execute("DELETE FROM items WHERE day=?", (str(td),))
                for pos, it in enumerate(src):
                    g.con.execute("INSERT INTO items(id, day, position, url, title) VALUES(?,?,?,?,?)",
                                  (uuid.uuid4().hex, str(td), pos, it["url"], it["title"]))
                done.append(td.isoformat())
            g.con.execute("COMMIT")
        except Exception:
            g.con.execute("ROLLBACK")
            raise
        db.bump(g.con)
        return jsonify({"ok": True, "copied": done, "labels": [day_label(x) for x in done]})

    @app.route("/api/video/cerca", methods=["POST"])
    @login_required(api=True)
    def api_cerca():
        q = ((request.get_json(silent=True) or {}).get("q") or "").strip()
        sql = "SELECT url, title, duration, ok FROM media WHERE probe IS NOT NULL"
        args = []
        if q:
            sql += " AND (title LIKE ? OR url LIKE ?)"
            args = [f"%{q}%", f"%{q}%"]
        sql += " ORDER BY COALESCE(used_at, checked_at) DESC LIMIT 12"
        return jsonify({"results": [dict(r) for r in g.con.execute(sql, args)]})

    # ---------- API: copie di sicurezza ----------

    @app.route("/api/copie/ora", methods=["POST"])
    @login_required(api=True)
    def api_copia_ora():
        try:
            name = backup.backup_now(g.con)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 422
        return jsonify({"ok": True, "name": name})

    @app.route("/api/copie/elenco")
    @login_required(api=True)
    def api_copie_elenco():
        try:
            return jsonify({"files": backup.list_backups(db.settings(g.con))})
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 422

    @app.route("/api/copie/ripristina", methods=["POST"])
    @login_required(api=True)
    def api_ripristina():
        name = (request.get_json(silent=True) or {}).get("name") or ""
        try:
            data = backup.download(db.settings(g.con), name)
            backup.import_data(g.con, data)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 422
        return jsonify({"ok": True})

    @app.route("/copie/scarica")
    @login_required()
    def copie_scarica():
        name = datetime.now(db.TZ).strftime("%Y.%m.%d") + " Backup regia.json"
        payload = json.dumps(backup.export_data(g.con), ensure_ascii=False, indent=1)
        return Response(payload, mimetype="application/json",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name.replace(' ', '%20')}"})

    @app.route("/copie/carica", methods=["POST"])
    @login_required()
    def copie_carica():
        f = request.files.get("file")
        try:
            data = json.load(f.stream)
            backup.import_data(g.con, data)
            flash("Copia di sicurezza ripristinata.", "ok")
        except Exception as e:  # noqa: BLE001
            flash(f"Ripristino non riuscito: {e}", "errore")
        return redirect(url_for("impostazioni") + "#copie")

    app.jinja_env.globals.update(day_label=day_label)
    app.jinja_env.filters["ora"] = lambda ts: datetime.fromtimestamp(ts, db.TZ).strftime("%H:%M:%S") if ts else ""
    app.jinja_env.filters["durata"] = fmt_duration
    app.jinja_env.filters["dataora"] = lambda iso: (datetime.fromisoformat(iso).strftime("%d/%m/%Y alle %H:%M") if iso else "")
    return app


def fmt_duration(sec):
    sec = int(round(sec or 0))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def bootstrap_admin():
    con = db.connect()
    try:
        if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            pw = os.environ.get("ADMIN_PASSWORD") or ""
            user = os.environ.get("ADMIN_USER") or "admin"
            if not pw:
                pw = secrets.token_urlsafe(9)
                print(f"*** Primo accesso: utente «{user}», password «{pw}». Cambiala dalla pagina Utenti. ***",
                      flush=True)
            con.execute("INSERT OR IGNORE INTO users(username, name, pw_hash, created_at) VALUES(?,?,?,?)",
                        (user, "Amministratore", generate_password_hash(pw), time.time()))
    finally:
        con.close()


app = create_app() if os.environ.get("REGIA_NO_APP") != "1" else None
