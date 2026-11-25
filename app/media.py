"""Lettura delle caratteristiche dei video (ffprobe) e controllo di conformità."""
import json
import os
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit, quote, unquote

from . import db


def normalize_url(url):
    """Accetta indirizzi incollati con spazi o lettere accentate."""
    url = (url or "").strip().strip('"').strip("'")
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return ""
    path = quote(unquote(parts.path), safe="/")
    query = quote(unquote(parts.query), safe="=&")
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def title_from_url(url):
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = os.path.splitext(name)[0].replace("_", " ").strip()
    return name or url


def _fraction(value):
    try:
        if "/" in value:
            a, b = value.split("/")
            return float(a) / float(b) if float(b) else 0.0
        return float(value)
    except Exception:
        return 0.0


def probe(url, timeout=45):
    cmd = ["ffprobe", "-v", "error", "-rw_timeout", "20000000",
           "-show_format", "-show_streams", "-of", "json", url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "Il video non risponde: l'indirizzo non si apre entro 45 secondi."
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        msg = err[-1] if err else "errore sconosciuto"
        if "404" in msg:
            return None, "Il video non esiste a questo indirizzo (errore 404)."
        if "403" in msg:
            return None, "Accesso al video negato dalla CDN (errore 403)."
        return None, f"Il video non si apre: {msg}"
    try:
        return json.loads(p.stdout), None
    except Exception:
        return None, "Risposta di ffprobe non leggibile."


def fmt_int(n):
    return f"{int(round(n)):,}".replace(",", ".")


def analyse(data, s):
    """Restituisce (durata, info, problemi) confrontando il file con il formato richiesto."""
    issues = []
    fmt = data.get("format", {}) if data else {}
    streams = data.get("streams", []) if data else []
    video = next((x for x in streams if x.get("codec_type") == "video"
                  and not x.get("disposition", {}).get("attached_pic")), None)
    audio = next((x for x in streams if x.get("codec_type") == "audio"), None)

    duration = 0.0
    try:
        duration = float(fmt.get("duration") or 0)
    except Exception:
        pass
    if duration <= 0 and video:
        duration = _fraction(video.get("duration", "0"))

    size = 0
    try:
        size = int(fmt.get("size") or 0)
    except Exception:
        pass
    bitrate = 0
    try:
        bitrate = int(fmt.get("bit_rate") or 0)
    except Exception:
        pass
    if not bitrate and size and duration:
        bitrate = int(size * 8 / duration)

    info = {"duration": duration, "kbps": round(bitrate / 1000)}

    max_kbps = int(s.get("max_kbps") or 2500)
    want_w, want_h = int(s.get("width") or 1920), int(s.get("height") or 1080)
    want_fps = float(s.get("fps") or 25)
    want_rate = int(s.get("audio_rate") or 48000)

    if duration <= 0:
        issues.append("Durata non leggibile.")

    if not video:
        issues.append("Il file non contiene video.")
    else:
        vcodec = video.get("codec_name", "?")
        w, h = int(video.get("width") or 0), int(video.get("height") or 0)
        fps = _fraction(video.get("avg_frame_rate") or "0") or _fraction(video.get("r_frame_rate") or "0")
        info.update({"vcodec": vcodec, "width": w, "height": h, "fps": round(fps, 3),
                     "profile": video.get("profile", "")})
        if vcodec != "h264":
            issues.append(f"Video in {vcodec.upper()}, serve H.264.")
        if (w, h) != (want_w, want_h):
            issues.append(f"Risoluzione {w}×{h}, serve {want_w}×{want_h}.")
        if abs(fps - want_fps) > 0.05:
            shown = f"{fps:.3f}".rstrip("0").rstrip(".").replace(".", ",")
            issues.append(f"{shown} fotogrammi al secondo, ne servono {want_fps:g}.")

    if not audio:
        issues.append("Il file non ha audio: serve una traccia audio stereo.")
    else:
        acodec = audio.get("codec_name", "?")
        ch = int(audio.get("channels") or 0)
        rate = int(audio.get("sample_rate") or 0)
        info.update({"acodec": acodec, "channels": ch, "rate": rate})
        if acodec != "aac":
            issues.append(f"Audio in {acodec.upper()}, serve AAC.")
        if ch != 2:
            issues.append("Audio mono, serve stereo." if ch == 1 else f"Audio a {ch} canali, serve stereo.")
        if rate and rate != want_rate:
            issues.append(f"Audio a {fmt_int(rate)} Hz, serve {fmt_int(want_rate)} Hz.")

    if bitrate and bitrate > max_kbps * 1000:
        issues.append(f"Bitrate {fmt_int(bitrate / 1000)} kbps, oltre il limite di {fmt_int(max_kbps)} kbps.")
    elif not bitrate:
        issues.append("Bitrate non leggibile.")

    return duration, info, issues


def check(con, url, refresh=False):
    """Verifica un indirizzo e salva il risultato. Restituisce la riga media o un errore."""
    url = normalize_url(url)
    if not url:
        return None, "Indirizzo non valido: deve iniziare con http:// o https://."
    row = con.execute("SELECT * FROM media WHERE url=?", (url,)).fetchone()
    if row and not refresh and row["probe"]:
        return row, None
    data, err = probe(url)
    if err:
        return None, err
    s = db.settings(con)
    duration, info, issues = analyse(data, s)
    title = row["title"] if row else title_from_url(url)
    con.execute(
        "INSERT INTO media(url, title, duration, probe, ok, issues, info, checked_at) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET duration=excluded.duration, "
        "probe=excluded.probe, ok=excluded.ok, issues=excluded.issues, info=excluded.info, "
        "checked_at=excluded.checked_at",
        (url, title, duration, json.dumps(data), 0 if issues else 1,
         json.dumps(issues, ensure_ascii=False), json.dumps(info), time.time()))
    return con.execute("SELECT * FROM media WHERE url=?", (url,)).fetchone(), None


def recheck_all(con):
    """Ricalcola la conformità di tutti i video già letti (dopo un cambio di formato richiesto)."""
    s = db.settings(con)
    for row in con.execute("SELECT url, probe FROM media WHERE probe IS NOT NULL").fetchall():
        try:
            data = json.loads(row["probe"])
        except Exception:
            continue
        duration, info, issues = analyse(data, s)
        con.execute("UPDATE media SET duration=?, ok=?, issues=?, info=? WHERE url=?",
                    (duration, 0 if issues else 1, json.dumps(issues, ensure_ascii=False),
                     json.dumps(info), row["url"]))
