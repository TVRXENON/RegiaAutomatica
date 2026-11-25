"""Calcolo degli orari del palinsesto (previsti e reali)."""
import json
import time
from datetime import date, timedelta

from . import db


def day_items(con, day):
    rows = con.execute(
        "SELECT i.id, i.day, i.position, i.url, i.title, m.duration, m.ok, m.issues, m.info "
        "FROM items i LEFT JOIN media m ON m.url = i.url WHERE i.day = ? ORDER BY i.position",
        (str(day),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["duration"] = d["duration"] or 0
        d["issues"] = json.loads(d["issues"] or "[]") if d["issues"] else ["Video non ancora verificato."]
        d["info"] = json.loads(d["info"] or "{}") if d["info"] else {}
        d["playable"] = bool(d["ok"]) and d["duration"] > 0
        out.append(d)
    return out


def records(con, day=None, kind=None, since=None, until=None):
    q, a = "SELECT * FROM aired WHERE 1=1", []
    if day is not None:
        q += " AND day = ?"; a.append(str(day))
    if kind:
        q += " AND kind = ?"; a.append(kind)
    if since is not None:
        q += " AND air_end > ?"; a.append(since)
    if until is not None:
        q += " AND air_start < ?"; a.append(until)
    q += " ORDER BY air_start, id"
    return [dict(r) for r in con.execute(q, a).fetchall()]


def last_program_record(con, day=None):
    q, a = "SELECT * FROM aired WHERE kind='programma' AND status IN ('ok','interrotto')", []
    if day is not None:
        q += " AND day = ?"; a.append(str(day))
    r = con.execute(q + " ORDER BY air_start DESC, id DESC LIMIT 1", a).fetchone()
    return dict(r) if r else None


def nominal_spill(con, day):
    """Fine del video che sfora la mezzanotte, calcolata sugli orari teorici del giorno."""
    t, eod = db.midnight(day), db.next_midnight(day)
    for it in day_items(con, day):
        if not it["playable"]:
            continue
        if t >= eod:
            break
        t += it["duration"]
    return t if t > eod else None


def timeline(con, day, now=None, _depth=0):
    """Elenco dei video del giorno con orario di inizio e fine e stato."""
    now = now or time.time()
    if isinstance(day, str):
        day = date.fromisoformat(day)
    today = db.today()
    start_of, eod = db.midnight(day), db.next_midnight(day)
    items = day_items(con, day)

    recs = {}
    for r in records(con, day=day, kind="programma"):
        if r["item_id"]:
            recs[r["item_id"]] = r

    # partenza: mezzanotte, oppure fine del video che sfora dal giorno prima
    anchor = start_of
    prev = last_program_record(con, day - timedelta(days=1))
    if prev and prev["planned_end"] > start_of and prev["air_start"] < start_of:
        anchor = max(anchor, prev["air_end"] if prev["status"] == "ok" else prev["planned_end"])
    elif day > today and _depth < 2:
        prev_tl = timeline(con, day - timedelta(days=1), now, _depth + 1)
        if prev_tl["spill"]:
            anchor = prev_tl["spill"]

    last_idx = -1
    for i, it in enumerate(items):
        if it["id"] in recs:
            last_idx = i

    # orari teorici, usati per i video che non sono andati in onda
    nt = anchor
    for it in items:
        it["nstart"] = it["nend"] = None
        if it["playable"]:
            it["nstart"], it["nend"] = nt, nt + it["duration"]
            nt += it["duration"]

    # fine di ciò che è in onda o in coda adesso (anche il riempitivo)
    live_end = None
    if day == today:
        r = con.execute("SELECT MAX(air_end) FROM aired WHERE status='ok' AND air_end > ? AND air_start < ?",
                        (now, eod)).fetchone()[0]
        live_end = r

    t = anchor
    for i, it in enumerate(items):
        r = recs.get(it["id"])
        it["start"] = it["end"] = None
        if r:
            it["start"], it["end"] = r["air_start"], r["air_end"]
            if r["status"] == "ok":
                if r["air_start"] <= now < r["air_end"]:
                    it["state"] = "in_onda"
                elif r["air_start"] > now:
                    it["state"] = "in_coda"
                else:
                    it["state"] = "trasmesso"
            else:
                it["state"] = r["status"]
                it["note"] = r["note"]
            t = max(t, r["air_end"])
        elif not it["playable"]:
            it["state"] = "non_conforme"
        elif i < last_idx:
            it["state"] = "non_trasmesso"
            it["start"], it["end"] = it["nstart"], it["nend"]
        else:
            if day == today:
                if live_end:
                    t = max(t, live_end)
                elif t + it["duration"] <= now:
                    # la trasmissione era ferma: il motore riparte allineato all'orario
                    it["state"] = "non_trasmesso"
                    it["start"], it["end"] = t, t + it["duration"]
                    t += it["duration"]
                    continue
            it["start"], it["end"] = t, t + it["duration"]
            t += it["duration"]
            it["state"] = "fuori_orario" if it["start"] >= eod else "previsto"

    spill = None
    for it in items:
        if it["start"] is not None and it["start"] < eod and it["end"] and it["end"] > eod:
            spill = it["end"]
    total = sum(it["duration"] for it in items if it["playable"])
    return {"day": day.isoformat(), "anchor": anchor, "start_of": start_of, "end_of": eod,
            "items": items, "spill": spill, "total": total,
            "late_start": anchor - start_of if anchor > start_of else 0}
