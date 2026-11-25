"""Copia di sicurezza di palinsesti, impostazioni e utenti su Bunny Storage."""
import json
import logging
import time
from datetime import datetime
from urllib.parse import quote

import requests

from . import db

log = logging.getLogger("backup")

SKIP_KEYS = {"data_version", "backup_version", "last_backup", "last_backup_error",
             "storage_password", "storage_zone", "storage_host", "backup_folder"}

REGIONS = [
    ("storage.bunnycdn.com", "Falkenstein (Germania)"),
    ("uk.storage.bunnycdn.com", "Londra"),
    ("se.storage.bunnycdn.com", "Stoccolma"),
    ("ny.storage.bunnycdn.com", "New York"),
    ("la.storage.bunnycdn.com", "Los Angeles"),
    ("sg.storage.bunnycdn.com", "Singapore"),
    ("syd.storage.bunnycdn.com", "Sydney"),
    ("br.storage.bunnycdn.com", "San Paolo"),
    ("jh.storage.bunnycdn.com", "Johannesburg"),
]


def export_data(con):
    s = db.settings(con)
    return {
        "programma": "Regia automatica",
        "versione": 1,
        "creato": datetime.now(db.TZ).isoformat(timespec="seconds"),
        "impostazioni": {k: v for k, v in s.items() if k not in SKIP_KEYS},
        "utenti": [dict(r) for r in con.execute("SELECT username, name, pw_hash, created_at FROM users")],
        "palinsesti": [dict(r) for r in con.execute("SELECT id, day, position, url, title FROM items ORDER BY day, position")],
        "video": [dict(r) for r in con.execute("SELECT url, title, probe FROM media WHERE probe IS NOT NULL")],
    }


def import_data(con, data):
    """Ripristina i dati da una copia. Le credenziali dello Storage restano quelle attuali."""
    from . import media
    if not isinstance(data, dict) or "palinsesti" not in data:
        raise ValueError("Il file non è una copia di sicurezza della regia automatica.")
    con.execute("BEGIN")
    try:
        for k, v in (data.get("impostazioni") or {}).items():
            if k in db.DEFAULT_SETTINGS and k not in SKIP_KEYS:
                con.execute("UPDATE settings SET value=? WHERE key=?", (str(v), k))
        users = data.get("utenti") or []
        if users:
            con.execute("DELETE FROM users")
            for u in users:
                con.execute("INSERT INTO users(username, name, pw_hash, created_at) VALUES(?,?,?,?)",
                            (u["username"], u["name"], u["pw_hash"], u.get("created_at") or time.time()))
        con.execute("DELETE FROM items")
        for it in data["palinsesti"]:
            con.execute("INSERT INTO items(id, day, position, url, title) VALUES(?,?,?,?,?)",
                        (it["id"], it["day"], it["position"], it["url"], it["title"]))
        for m in data.get("video") or []:
            con.execute("INSERT INTO media(url, title, probe) VALUES(?,?,?) "
                        "ON CONFLICT(url) DO UPDATE SET probe=excluded.probe, title=excluded.title",
                        (m["url"], m["title"], m["probe"]))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    media.recheck_all(con)
    db.bump(con)
    db.send_command(con, "restart")


# ---------- Bunny Storage ----------

def _base(s):
    host = (s.get("storage_host") or "storage.bunnycdn.com").strip()
    zone = (s.get("storage_zone") or "").strip()
    folder = (s.get("backup_folder") or "regia-backup").strip().strip("/")
    return f"https://{host}/{quote(zone)}/{quote(folder)}/"


def configured(s):
    return bool((s.get("storage_zone") or "").strip() and (s.get("storage_password") or "").strip())


def upload(s, name, payload):
    r = requests.put(_base(s) + quote(name), data=payload, timeout=60,
                     headers={"AccessKey": s["storage_password"], "Content-Type": "application/octet-stream"})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Bunny Storage ha risposto {r.status_code}: {r.text[:200]}")


def list_backups(s):
    r = requests.get(_base(s), timeout=30, headers={"AccessKey": s["storage_password"], "Accept": "application/json"})
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise RuntimeError(f"Bunny Storage ha risposto {r.status_code}: {r.text[:200]}")
    files = [f for f in r.json() if not f.get("IsDirectory") and f.get("ObjectName", "").endswith(".json")]
    files.sort(key=lambda f: f.get("ObjectName", ""), reverse=True)
    return [{"name": f["ObjectName"], "size": f.get("Length", 0), "changed": f.get("LastChanged", "")} for f in files]


def download(s, name):
    r = requests.get(_base(s) + quote(name), timeout=60, headers={"AccessKey": s["storage_password"]})
    if r.status_code != 200:
        raise RuntimeError(f"Bunny Storage ha risposto {r.status_code}.")
    return r.json()


def backup_now(con):
    s = db.settings(con)
    if not configured(s):
        raise RuntimeError("Bunny Storage non è configurato.")
    version = s.get("data_version", "0")
    name = datetime.now(db.TZ).strftime("%Y.%m.%d") + " Backup regia.json"
    payload = json.dumps(export_data(con), ensure_ascii=False, indent=1).encode("utf-8")
    upload(s, name, payload)
    db.set_setting(con, "backup_version", version, touch=False)
    db.set_setting(con, "last_backup", datetime.now(db.TZ).isoformat(timespec="seconds"), touch=False)
    db.set_setting(con, "last_backup_error", "", touch=False)
    log.info("Copia di sicurezza salvata: %s", name)
    return name


def auto_backup():
    """Chiamato dal motore ogni minuto: salva se i dati sono cambiati, e comunque una volta al giorno."""
    con = db.connect()
    try:
        s = db.settings(con)
        if not configured(s):
            return
        changed = s.get("data_version") != s.get("backup_version")
        last_change = float(db.get_state(con, "last_change", 0) or 0)
        last = s.get("last_backup") or ""
        today = datetime.now(db.TZ).date().isoformat()
        last_attempt = float(db.get_state(con, "backup_attempt", 0) or 0)
        if s.get("last_backup_error") and time.time() - last_attempt < 900:
            return  # dopo un errore riprova ogni 15 minuti, non ogni minuto
        if (changed and time.time() - last_change > 120) or not last.startswith(today):
            db.put_state(con, "backup_attempt", time.time())
            try:
                backup_now(con)
            except Exception as e:  # noqa: BLE001
                db.set_setting(con, "last_backup_error", f"{datetime.now(db.TZ):%d/%m %H:%M} {e}", touch=False)
                log.error("Copia di sicurezza non riuscita: %s", e)
    finally:
        con.close()
