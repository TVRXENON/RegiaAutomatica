"""Database SQLite condiviso tra pagina web e motore di messa in onda."""
import os
import sqlite3
import json
import time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "regia.db")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Rome"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    url TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    duration REAL,
    probe TEXT,
    ok INTEGER NOT NULL DEFAULT 0,
    issues TEXT NOT NULL DEFAULT '[]',
    info TEXT NOT NULL DEFAULT '{}',
    checked_at REAL,
    used_at REAL
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    position INTEGER NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_day ON items(day, position);
CREATE TABLE IF NOT EXISTS aired (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    item_id TEXT,
    kind TEXT NOT NULL,
    url TEXT,
    title TEXT NOT NULL,
    air_start REAL NOT NULL,
    air_end REAL NOT NULL,
    planned_end REAL NOT NULL,
    seek REAL NOT NULL DEFAULT 0,
    pos INTEGER NOT NULL DEFAULT -1,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS aired_time ON aired(air_start);
CREATE INDEX IF NOT EXISTS aired_day ON aired(day);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cmd TEXT NOT NULL,
    created REAL NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "station_name": "TVR Xenon",
    "rtmp_url": "",
    "rtmp_key": "",
    "filler_url": "",
    "max_kbps": "2500",
    "width": "1920",
    "height": "1080",
    "fps": "25",
    "audio_rate": "48000",
    "broadcast_enabled": "1",
    "realign_hour": "3",
    "storage_zone": "",
    "storage_password": "",
    "storage_host": "storage.bunnycdn.com",
    "backup_folder": "regia-backup",
    "data_version": "1",
    "backup_version": "0",
    "last_backup": "",
    "last_backup_error": "",
}


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init():
    con = connect()
    con.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
    con.close()


# ---------- impostazioni ----------

def settings(con):
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}


def setting(con, key, default=""):
    r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(con, key, value, touch=True):
    con.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    if touch:
        bump(con)


def bump(con):
    """Segnala che i dati sono cambiati (serve al salvataggio automatico)."""
    con.execute("UPDATE settings SET value = CAST(value AS INTEGER) + 1 WHERE key='data_version'")
    con.execute("INSERT INTO state(key, value) VALUES('last_change', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))


# ---------- stato del motore ----------

def get_state(con, key, default=None):
    r = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except Exception:
        return r["value"]


def put_state(con, key, value):
    con.execute("INSERT INTO state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def send_command(con, cmd):
    con.execute("INSERT INTO commands(cmd, created) VALUES(?, ?)", (cmd, time.time()))


# ---------- date e orari ----------

def today():
    return datetime.now(TZ).date()


def local_date(ts):
    return datetime.fromtimestamp(ts, TZ).date()


def midnight(d):
    """Mezzanotte locale (inizio giornata) come timestamp."""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()


def next_midnight(d):
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return midnight(d + timedelta(days=1))
