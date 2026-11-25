"""Motore di messa in onda.

Legge i video direttamente dalla CDN e li invia al server RTMP senza ricodificarli.
Ogni video viene letto da un ffmpeg "lettore" che lo riscrive in MPEG-TS con i tempi
spostati in avanti (output_ts_offset), così i video si accodano con tempi continui.
Un unico ffmpeg "trasmettitore" riceve il flusso continuo e lo manda in RTMP in tempo
reale: il collegamento resta aperto da un video all'altro.
"""
import collections
import logging
import os
import queue
import re
import subprocess
import threading
import time
from datetime import date, datetime, timedelta

from . import db, schedule, backup

log = logging.getLogger("motore")

CHUNK = 65536
QUEUE_CHUNKS = 160          # circa 10 MB: oltre 30 secondi di anticipo a 2,5 Mbit/s
GAP = 0.05                  # margine di tempo tra un video e il successivo
LEAD = 15                   # anticipo massimo (secondi) con cui un video viene accodato
PROGRESS_KEY = re.compile(r"^[a-z0-9_]+=")
# avvisi normali al passaggio da un video all'altro, da non mostrare come errori
BENIGN = re.compile(r"corrupt input packet|Continuity check|Packet corrupt|non monotonically|Non-monotonic", re.I)


class SessionError(Exception):
    pass


class Aborted(Exception):
    pass


def rtmp_target(s):
    url = (s.get("rtmp_url") or "").strip()
    key = (s.get("rtmp_key") or "").strip()
    if not url.startswith(("rtmp://", "rtmps://")):
        return ""
    return url.rstrip("/") + "/" + key if key else url


class Output:
    """Il trasmettitore: un solo ffmpeg che resta collegato al server RTMP."""

    def __init__(self, target):
        self.q = queue.Queue(maxsize=QUEUE_CHUNKS)
        self.errors = collections.deque(maxlen=12)
        self.out_time = 0.0
        self.progress_at = time.time()
        self.failed = None
        self.t0 = None
        self.started = time.time()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
               "-re", "-f", "mpegts", "-i", "pipe:0",
               "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
               "-f", "flv", "-flvflags", "no_duration_filesize",
               "-progress", "pipe:2", target]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._writer, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _writer(self):
        try:
            while True:
                chunk = self.q.get()
                if chunk is None:
                    break
                if self.t0 is None:
                    self.t0 = time.time()
                    self.progress_at = self.t0
                self.proc.stdin.write(chunk)
        except (BrokenPipeError, OSError, ValueError):
            self.failed = self.failed or "Il server RTMP ha chiuso il collegamento."
        finally:
            try:
                self.proc.stdin.close()
            except Exception:
                pass

    def _stderr(self):
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if line.startswith("out_time_us="):
                try:
                    v = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                if v > self.out_time + 0.001:
                    self.out_time = v
                    self.progress_at = time.time()
            elif PROGRESS_KEY.match(line) or BENIGN.search(line):
                continue
            else:
                self.errors.append(line)

    def alive(self):
        return self.failed is None and self.proc.poll() is None

    def problem(self):
        if self.failed:
            return self.failed
        if self.proc.poll() is not None:
            return "Il collegamento RTMP si è chiuso."
        if self.t0 and time.time() - self.progress_at > 45:
            return "La trasmissione si è bloccata: nessun dato inviato da 45 secondi."
        return None

    def last_error(self):
        return self.errors[-1] if self.errors else ""

    def close(self, wait):
        try:
            self.q.put(None, timeout=5)
        except queue.Full:
            pass
        try:
            self.proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self):
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        # sblocca il writer se è fermo in attesa
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass


class Engine:
    def __init__(self):
        db.init()
        self.con = db.connect()
        self.out = None
        self.offset = 0.0
        self.need_resume = True
        self.cur_day = None
        self.cur_item = None
        self.cur_pos = -1
        self.abort_reason = None
        self.backoff = 3
        self.last_state = 0
        self.last_settings = 0
        self.s = db.settings(self.con)
        self.status = ("avvio", "Avvio del motore di messa in onda.")
        self.filler_broken_until = 0
        self.last_cleanup = 0
        self.last_backup_check = 0
        self.backup_thread = None
        self.now_playing = None

    # ---------- ciclo principale ----------

    def run(self):
        log.info("Motore avviato")
        self.cut_records(time.time(), "Riavvio del programma")
        while True:
            try:
                self.step()
            except Aborted:
                reason = self.abort_reason
                self.teardown("Trasmissione fermata dalla pagina di gestione" if reason == "stop"
                              else "Riavvio del collegamento")
            except SessionError as e:
                self.fail(str(e))
            except Exception as e:  # noqa: BLE001
                log.exception("Errore imprevisto")
                self.fail(f"Errore interno del motore: {e}")

    def step(self):
        self.tick(force=True)
        if self.out is None:
            self.abort_reason = None
        s = self.s
        if s.get("broadcast_enabled") != "1":
            if self.out:
                self.teardown("Trasmissione fermata dalla pagina di gestione")
            self.set_status("fermo", "Trasmissione ferma. Si riavvia dalla pagina In onda.")
            self.sleep(1)
            return
        target = rtmp_target(s)
        if not target:
            if self.out:
                self.teardown("Indirizzo RTMP rimosso")
            self.set_status("non_configurato", "Manca l'indirizzo RTMP: inseriscilo nelle Impostazioni.")
            self.sleep(2)
            return

        if self.out is None:
            self.start_session(target)
        elif self.realign_due():
            self.realign(target)

        now = time.time()
        if self.need_resume:
            plan = self.resume_plan(now)
            self.need_resume = False
        else:
            plan = self.next_plan(self.air_time())
        if plan:
            self.play(plan)
            self.wait_lead()

    def wait_lead(self):
        """Non accoda il video successivo con troppo anticipo: così le modifiche
        al palinsesto valgono fino a pochi secondi prima della messa in onda."""
        while self.out and self.out.t0 and self.air_time() - time.time() > LEAD:
            self.tick()
            if self.abort_reason:
                raise Aborted()
            problem = self.out.problem()
            if problem:
                raise SessionError(problem)
            time.sleep(0.25)

    def start_session(self, target):
        log.info("Apro il collegamento RTMP")
        self.out = Output(target)
        self.offset = 0.0
        self.abort_reason = None
        self.set_status("in_onda", "")

    def realign_due(self):
        """Il collegamento viene riaperto una volta al giorno, tra un video e l'altro,
        per azzerare i tempi del flusso (che altrimenti dopo circa 26 ore si riavvolgono).
        Avviene alla fine del primo video dopo l'ora scelta (entro le 3 ore successive);
        in ogni caso il collegamento non resta aperto più di 24 ore e mezza."""
        if not self.out or not self.out.t0:
            return False
        age = time.time() - self.out.started
        hour = int(self.s.get("realign_hour") or 3)
        in_window = (datetime.now(db.TZ).hour - hour) % 24 < 3
        return (age > 2 * 3600 and in_window) or age > 24.5 * 3600

    def realign(self, target):
        log.info("Riallineamento giornaliero del collegamento")
        buffered = max(0.0, self.air_time() - time.time())
        self.out.close(wait=buffered + 30)
        self.out = None
        self.start_session(target)

    def air_time(self):
        """Ora in cui andrà in onda il prossimo video accodato."""
        if not self.out or self.out.t0 is None:
            return time.time() + self.offset
        return self.out.t0 + self.offset

    # ---------- scelta del prossimo video ----------

    def _items(self, day):
        return schedule.day_items(self.con, day)

    def _after_cursor(self, items):
        idx = next((i for i, it in enumerate(items) if it["id"] == self.cur_item), None)
        if idx is not None:
            return items[idx + 1:]
        return [it for it in items if it["position"] > self.cur_pos]

    def next_plan(self, air_at):
        day = db.local_date(air_at)
        if db.next_midnight(day) - air_at < 1.0:
            day = day + timedelta(days=1)
        if self.cur_day != day:
            self.cur_day, self.cur_item, self.cur_pos = day, None, -1
        for it in self._after_cursor(self._items(day)):
            if not it["playable"]:
                self.log_skip(day, it, air_at)
                continue
            return self.program_plan(day, it, 0.0)
        return self.filler_plan(day, db.next_midnight(day) - air_at)

    def resume_plan(self, now):
        """Dopo un avvio o un'interruzione riprende dal punto giusto secondo l'orario."""
        today = db.today()
        yesterday = today - timedelta(days=1)
        last = schedule.last_program_record(self.con)
        last_day = date.fromisoformat(last["day"]) if last else None

        # 1. il video interrotto è ancora nel suo orario: riprende da dove era arrivato
        if last and last_day in (today, yesterday) and last["air_start"] <= now < last["planned_end"] - 3:
            items = self._items(last_day)
            it = next((x for x in items if x["id"] == last["item_id"]), None)
            if it and it["playable"]:
                seek = last["seek"] + (now - last["air_start"])
                self.cur_day = last_day
                return self.program_plan(last_day, it, seek if seek >= 2 else 0.0)

        # 2. altrimenti si allinea agli orari del giorno
        start_mid, eod = db.midnight(today), db.next_midnight(today)
        items = self._items(today)
        self.cur_day, self.cur_item, self.cur_pos = today, None, -1
        if last and last_day == today:
            t = last["planned_end"]
            self.cur_item, self.cur_pos = last["item_id"], last["pos"]
            items = self._after_cursor(items)
        elif last and last_day == yesterday and last["planned_end"] > start_mid:
            t = last["planned_end"]
        else:
            t = start_mid
        if t > now:
            t = now
        for it in items:
            if not it["playable"]:
                continue
            if t >= eod:
                break
            dur = it["duration"]
            if t + dur > now:
                seek = now - t
                if dur - seek < 3:
                    t += dur
                    self.cur_item, self.cur_pos = it["id"], it["position"]
                    continue
                return self.program_plan(today, it, seek if seek >= 2 else 0.0)
            t += dur
            self.cur_item, self.cur_pos = it["id"], it["position"]
        return self.filler_plan(today, eod - now)

    def program_plan(self, day, it, seek):
        return {"kind": "programma", "day": day, "item_id": it["id"], "pos": it["position"],
                "url": it["url"], "title": it["title"], "seek": seek, "limit": None,
                "duration": max(0.0, it["duration"] - seek)}

    def filler_plan(self, day, remaining):
        url = (self.s.get("filler_url") or "").strip()
        row = None
        if url and time.time() > self.filler_broken_until:
            row = self.con.execute("SELECT * FROM media WHERE url=?", (url,)).fetchone()
        if row and row["ok"] and (row["duration"] or 0) > 0:
            dur = row["duration"]
            limit = remaining if remaining < dur else None
            return {"kind": "riempitivo", "day": day, "item_id": None, "pos": -1, "url": url,
                    "title": row["title"], "seek": 0.0, "limit": limit,
                    "duration": limit if limit else dur}
        # nessun riempitivo valido: schermo nero con silenzio, a blocchi di un minuto
        limit = max(1.0, min(60.0, remaining))
        return {"kind": "nero", "day": day, "item_id": None, "pos": -1, "url": None,
                "title": "Schermo nero (manca il video di riempimento)", "seek": 0.0,
                "limit": limit, "duration": limit}

    def log_skip(self, day, it, at):
        note = " ".join(it["issues"]) or "Video non conforme."
        self.con.execute(
            "INSERT INTO aired(day, item_id, kind, url, title, air_start, air_end, planned_end, seek, pos, status, note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(day), it["id"], "programma", it["url"], it["title"], at, at, at, 0, it["position"],
             "saltato", note))
        self.cur_item, self.cur_pos = it["id"], it["position"]
        log.warning("Saltato %s: %s", it["title"], note)

    # ---------- messa in onda ----------

    def play(self, plan):
        if plan["seek"] > 0 and plan["url"]:
            kf = self.keyframe_before(plan["url"], plan["seek"])
            plan["duration"] += plan["seek"] - kf
            plan["seek"] = kf
        start = self.air_time()
        planned_end = start + plan["duration"]
        cur = self.con.execute(
            "INSERT INTO aired(day, item_id, kind, url, title, air_start, air_end, planned_end, seek, pos, status, note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(plan["day"]), plan["item_id"], plan["kind"], plan["url"], plan["title"], start,
             planned_end, planned_end, plan["seek"], plan["pos"], "ok", ""))
        rec_id = cur.lastrowid
        if plan["kind"] == "programma":
            self.cur_day, self.cur_item, self.cur_pos = plan["day"], plan["item_id"], plan["pos"]
        if plan["url"]:
            self.con.execute("UPDATE media SET used_at=? WHERE url=?", (time.time(), plan["url"]))
        log.info("In coda %s (%s) alle %s", plan["title"], plan["kind"],
                 time.strftime("%H:%M:%S", time.localtime(start)))

        rc, fed, errs = self.feed(plan)

        if fed < 0.5 and rc != 0:
            msg = errs[-1] if errs else "il video non si apre"
            self.con.execute("UPDATE aired SET status='errore', air_end=air_start, planned_end=air_start, "
                             "note=? WHERE id=?", (f"Il video non si è aperto: {msg}", rec_id))
            log.error("Errore su %s: %s", plan["title"], msg)
            if plan["kind"] == "riempitivo":
                self.filler_broken_until = time.time() + 60
            self.sleep(1)
            return
        self.offset += fed + GAP + (0.15 if plan["seek"] > 0 else 0.0)
        end = self.air_time()
        if rc != 0:
            msg = errs[-1] if errs else ""
            self.con.execute("UPDATE aired SET air_end=?, status='interrotto', note=? WHERE id=?",
                             (end, f"Lettura interrotta: {msg}", rec_id))
        else:
            self.con.execute("UPDATE aired SET air_end=? WHERE id=?", (end, rec_id))
        if fed > 10:
            self.backoff = 3

    @staticmethod
    def keyframe_before(url, t):
        """Posizione del fotogramma chiave che precede t: la ripresa parte da lì,
        così i tempi del flusso restano esatti anche senza ricodifica."""
        cmd = ["ffprobe", "-v", "error", "-rw_timeout", "15000000", "-select_streams", "v:0",
               "-read_intervals", f"{max(0.0, t - 20):.3f}%{t + 0.5:.3f}",
               "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", url]
        try:
            outp = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
        except subprocess.TimeoutExpired:
            return 0.0
        best = 0.0
        for line in outp.splitlines():
            parts = line.split(",")
            try:
                pts = float(parts[0])
            except (ValueError, IndexError):
                continue
            if len(parts) > 1 and "K" in parts[1] and pts <= t + 0.001:
                best = max(best, pts)
        return best

    def feed_cmd(self, plan):
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error"]
        if plan["kind"] == "nero":
            w, h = self.s.get("width", "1920"), self.s.get("height", "1080")
            fps = self.s.get("fps", "25")
            rate = self.s.get("audio_rate", "48000")
            cmd += ["-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={fps}",
                    "-f", "lavfi", "-i", f"anullsrc=r={rate}:cl=stereo",
                    "-t", f"{plan['limit']:.3f}",
                    "-map", "0:v", "-map", "1:a",
                    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
                    "-pix_fmt", "yuv420p", "-b:v", "300k", "-g", str(int(float(fps) * 2)),
                    "-c:a", "aac", "-b:a", "96k"]
        else:
            cmd += ["-reconnect", "1", "-reconnect_on_network_error", "1",
                    "-reconnect_delay_max", "5", "-rw_timeout", "20000000"]
            if plan["seek"] > 0:
                cmd += ["-ss", f"{plan['seek']:.3f}"]
            cmd += ["-i", plan["url"]]
            if plan["limit"]:
                cmd += ["-t", f"{plan['limit']:.3f}"]
            cmd += ["-map", "0:v:0", "-map", "0:a:0", "-c", "copy"]
        cmd += ["-output_ts_offset", f"{self.offset:.3f}",
                "-f", "mpegts", "-progress", "pipe:2", "pipe:1"]
        return cmd

    def feed(self, plan):
        """Legge il video e lo accoda al trasmettitore. Restituisce (codice, secondi letti, errori)."""
        out = self.out
        proc = subprocess.Popen(self.feed_cmd(plan), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)
        progress = {"t": 0.0}
        errs = collections.deque(maxlen=5)

        def read_err():
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("out_time_us="):
                    try:
                        progress["t"] = max(progress["t"], int(line.split("=", 1)[1]) / 1e6)
                    except ValueError:
                        pass
                elif line and not PROGRESS_KEY.match(line):
                    errs.append(line)

        t_err = threading.Thread(target=read_err, daemon=True)
        t_err.start()
        fd = proc.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
                while True:
                    try:
                        out.q.put(chunk, timeout=1)
                        break
                    except queue.Full:
                        self.tick()
                        problem = out.problem()
                        if problem:
                            raise SessionError(problem)
                        if self.abort_reason:
                            raise Aborted()
                self.tick()
                if self.abort_reason:
                    raise Aborted()
                problem = out.problem()
                if problem:
                    raise SessionError(problem)
        except BaseException:
            try:
                proc.kill()
            except Exception:
                pass
            proc.wait()
            raise
        proc.wait()
        t_err.join(timeout=5)
        fed = progress["t"]  # secondi letti, esclusa la traslazione dei tempi
        return proc.returncode, fed, list(errs)

    # ---------- interruzioni ----------

    def cut_records(self, now, note):
        """Chiude i video in onda e cancella quelli accodati ma non ancora andati in onda."""
        self.con.execute("DELETE FROM aired WHERE air_start > ? AND status IN ('ok','interrotto')", (now + 0.5,))
        self.con.execute("UPDATE aired SET air_end=?, status='interrotto', note=? "
                         "WHERE air_start <= ? AND air_end > ? AND status='ok'", (now, note, now, now))

    def teardown(self, note):
        if self.out:
            self.out.kill()
            self.out = None
        self.cut_records(time.time(), note)
        self.offset = 0.0
        self.need_resume = True
        self.abort_reason = None

    def fail(self, msg):
        detail = ""
        if self.out and self.out.last_error():
            detail = self.out.last_error()
        log.error("%s %s", msg, detail)
        self.teardown("Collegamento interrotto")
        self.set_status("errore", msg + (f" Dettaglio: {detail}" if detail else ""))
        self.con.execute("INSERT INTO state(key, value) VALUES('last_error_at', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))
        self.sleep(self.backoff)
        self.backoff = min(self.backoff * 2, 30)

    # ---------- servizio ----------

    def sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.tick()
            if self.abort_reason == "restart" and not self.out:
                self.abort_reason = None
                return
            time.sleep(0.25)

    def set_status(self, code, message):
        self.status = (code, message)
        self.write_state()

    def write_state(self):
        now = time.time()
        out = self.out
        state = {
            "code": self.status[0], "message": self.status[1], "heartbeat": now,
            "session_started": out.started if out else None,
            "buffered": round(max(0.0, self.air_time() - now), 1) if out and out.t0 else 0,
            "rtmp_error": out.last_error() if out else "",
        }
        db.put_state(self.con, "engine", state)
        self.last_state = now

    def tick(self, force=False):
        now = time.time()
        if not force and now - self.last_settings < 1.0:
            return
        self.last_settings = now
        self.s = db.settings(self.con)
        for row in self.con.execute("SELECT id, cmd FROM commands ORDER BY id").fetchall():
            self.con.execute("DELETE FROM commands WHERE id=?", (row["id"],))
            if row["cmd"] == "restart":
                log.info("Riavvio richiesto dalla pagina di gestione")
                self.abort_reason = "restart"
        if self.s.get("broadcast_enabled") != "1" and self.out:
            self.abort_reason = self.abort_reason or "stop"
        if now - self.last_state >= 2:
            self.write_state()
        if now - self.last_backup_check > 60:
            self.last_backup_check = now
            if not (self.backup_thread and self.backup_thread.is_alive()):
                self.backup_thread = threading.Thread(target=backup.auto_backup, daemon=True)
                self.backup_thread.start()
        if now - self.last_cleanup > 3600:
            self.last_cleanup = now
            self.con.execute("DELETE FROM aired WHERE air_start < ?", (now - 10 * 86400,))
            self.con.execute("DELETE FROM items WHERE day < ?",
                             ((db.today() - timedelta(days=60)).isoformat(),))

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Engine().run()


if __name__ == "__main__":
    main()
