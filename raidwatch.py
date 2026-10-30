#!/usr/bin/env python3
"""
RAID Watch: keeps an eye on your RAID arrays and raises the alarm when one needs service.

Two parts, one file:
  raidwatch.py --daemon     background checker (runs as root via systemd: raidwatch.service)
  raidwatch.py              the little status window with the alarm (starts when you log in)
  raidwatch.py --once       check once and print the status (for testing / SSH)
  raidwatch.py --demo       the window with a pretend failed drive (changes nothing)

What it watches
  CRITICAL (red, alarm sound, desktop pop-up, Pi's green light flashes fast, phone push if set up)
    - an array is degraded or a drive in it failed
    - an array is stopped/inactive, or an array listed in mdadm.conf didn't start
    - a drive's SMART health check says FAILING
    - an array's filesystem is 98%+ full
  WARNING (amber, one pop-up, phone push if set up)
    - SSD wear past 80%, bad/pending sectors appear or grow, drive hotter than 60 °C
    - filesystem 90%+ full, a data check found mismatches, a hot spare got used up
  INFO (shown in the window, no alarm)
    - rebuild / resync / check in progress with progress and time left
  It also tells you when everything is healthy again.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

APP = "RAID Watch"
VERSION = "1.2"
DEMO = "--demo" in sys.argv
MDSTAT = os.environ.get("RAIDWATCH_MDSTAT", "/proc/mdstat")
RUN_DIR = os.environ.get("RAIDWATCH_RUN", "/run/raidwatch")          # status.json + ack.json (shared with the window)
STATE_DIR = os.environ.get("RAIDWATCH_STATE", "/var/lib/raidwatch")  # remembers what it already told you
CONF = os.environ.get("RAIDWATCH_CONF", "/etc/raidwatch.json")
MDADM_CONF = os.environ.get("RAIDWATCH_MDADM_CONF") or next((p for p in ("/etc/mdadm/mdadm.conf", "/etc/mdadm.conf") if os.path.exists(p)),
                  "/etc/mdadm/mdadm.conf")
STATUS = os.path.join(RUN_DIR, "status.json")
ACK = os.path.join(RUN_DIR, "ack.json")
TEST_FLAG = os.path.join(RUN_DIR, "test-alarm")
DEFAULTS = {
    "interval_s": 15,            # how often arrays are checked
    "smart_hours": 6,            # how often drives' SMART data is read
    "led_alarm": True,           # flash the Pi's green ACT light fast while something is critical
    "sound": True,               # the window plays an alarm sound for critical problems
    "remind_hours": 4,           # after you press Silence, alarm again if it's still broken this long later
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",            # set to get phone notifications with the free ntfy app (needs internet)
    "full_warn": 90, "full_crit": 98, "temp_warn": 60, "wear_warn": 80,
    # over-temperature alarm (optional): checked every temp_interval_s instead of with SMART
    "temp_alarm": False, "temp_alarm_drive": 65, "temp_alarm_cpu": 80, "temp_interval_s": 120,
    # password-protected web page (optional)
    "web": False, "web_port": 8484, "web_password": "",
}
LEVELS = {"ok": 0, "info": 1, "warn": 2, "crit": 3}


# ================================================================== helpers
def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def run(args, timeout=60):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, mode=0o644):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_conf():
    c = dict(DEFAULTS)
    c.update({k: v for k, v in load_json(CONF, {}).items() if k in DEFAULTS})
    return c


def disk_of(dev):
    return re.sub(r"p\d+$", "", dev) if re.match(r"(nvme|mmcblk)", dev) else re.sub(r"\d+$", "", dev)


def parse_mdstat(text):
    """Same reading of /proc/mdstat as RAID Forge."""
    arrays, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^(md\w+)\s*:\s*(\w+)\s*(raid\d+|linear)?\s*(.*)$", line)
        if m:
            devs = []
            for tok in m.group(4).split():
                dm = re.match(r"(\w+)\[\d+\](\((\w)\))?", tok)
                if dm:
                    devs.append((dm.group(1), {"F": "failed", "S": "spare"}.get(dm.group(3), "active")))
            cur = dict(name=m.group(1), state=m.group(2), level=m.group(3) or "?", devices=devs,
                       health="", want=0, have=0, op="", pct=None, finish="")
            arrays.append(cur)
            continue
        if not cur:
            continue
        h = re.search(r"\[(\d+)/(\d+)\]\s*\[([U_]+)\]", line)
        if h:
            cur["want"], cur["have"], cur["health"] = int(h.group(1)), int(h.group(2)), h.group(3)
        p = re.search(r"(resync|recovery|reshape|check|repair)\s*=\s*([\d.]+)%", line)
        if p:
            cur["op"], cur["pct"] = p.group(1), float(p.group(2))
            f = re.search(r"finish=([\d.]+)min", line)
            cur["finish"] = f.group(1) if f else ""
        if "resync=DELAYED" in line or "resync=PENDING" in line:
            cur["op"] = cur["op"] or "resync pending"
    return arrays


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1000 or unit == "PB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000


def nice_minutes(m):
    try:
        m = float(m)
    except (TypeError, ValueError):
        return ""
    return f"{m:.0f} min" if m < 90 else f"{m / 60:.1f} h"


# ================================================================== the checks (pure: inputs in, alerts out)
def check_arrays(arrays, configured, sysread, mounts, conf):
    """arrays: parse_mdstat output. configured: [(uuid, name)] from mdadm.conf. sysread(path)->str.
    mounts: {mdname: (mountpoint, used_pct)}. Returns (alerts, infos, array_rows)."""
    alerts, infos, rows = [], [], []
    for a in arrays:
        n = a["name"]
        label = a.get("alias") or n
        failed = [d for d, s in a["devices"] if s == "failed"]
        spares = [d for d, s in a["devices"] if s == "spare"]
        degraded = "_" in a["health"] or (a["want"] and a["have"] < a["want"])
        if a["state"] != "active":
            alerts.append(("crit", f"inactive:{n}", f"{label} is stopped ({a['state']}). Its data can't be reached. "
                                                    "Open RAID Forge > My Arrays > Find & start existing arrays."))
        if failed:
            alerts.append(("crit", f"failed:{n}", f"{label}: drive {', '.join('/dev/' + d for d in failed)} failed. "
                                                  "Replace it soon (RAID Forge > My Arrays > Replace a drive)."))
        if degraded:
            missing = a["want"] - a["have"] if a["want"] else 1
            # already rebuilding onto a new drive = being fixed: amber, not a repeating alarm
            alerts.append(("warn" if a["op"] == "recovery" else "crit", f"degraded:{n}",
                           f"{label} is DEGRADED: running on {a['have']} of {a['want']} drives"
                           f" ({missing} missing). It has no protection left"
                           + (" until the rebuild finishes." if a["op"] == "recovery" else ". Replace the drive.")))
        if a["op"]:
            what = {"recovery": "Rebuilding", "resync": "Syncing", "check": "Checking", "repair": "Repairing",
                    "reshape": "Reshaping"}.get(a["op"], a["op"].capitalize())
            pct = f" {a['pct']:.1f}%" if a["pct"] is not None else ""
            left = f", about {nice_minutes(a['finish'])} left" if a.get("finish") else ""
            infos.append(f"{label}: {what}{pct}{left}")
        # spares: a spare that existed before and is gone now = it was used up replacing a drive
        if a.get("had_spares") and not spares and not degraded:
            alerts.append(("warn", f"spare:{n}", f"{label} used its hot spare. Add a new spare to stay protected."))
        # data check result
        mc = sysread(f"/sys/block/{n}/md/mismatch_cnt").strip()
        last = sysread(f"/sys/block/{n}/md/last_sync_action").strip()
        if mc.isdigit() and int(mc) > 0 and last == "check" and not a["op"] and a["level"] not in ("raid0",):
            alerts.append(("warn", f"mismatch:{n}",
                           f"{label}: the last data check found {mc} mismatched blocks. Run 'Scrub' again; "
                           "if it keeps happening, check the drives' health."))
        m = tuple(mounts.get(n, ()))
        mp, used, free, total = (m + ("", None, None, None)[len(m):])[:4]
        if used is not None:
            if used >= conf["full_crit"]:
                alerts.append(("crit", f"full:{n}", f"{label} ({mp}) is {used:.0f}% full. Free up space now."))
            elif used >= conf["full_warn"]:
                alerts.append(("warn", f"full:{n}", f"{label} ({mp}) is {used:.0f}% full."))
        rows.append(dict(name=n, label=label, level=a["level"], state="DEGRADED" if degraded else a["state"],
                         health=a["health"], op=a["op"], pct=a["pct"], finish=a.get("finish", ""),
                         mount=mp, used=used, free=free, total=total, failed=failed, spares=spares))
    running = {a.get("uuid") for a in arrays if a.get("uuid")}
    for uuid, name in configured:
        if uuid not in running:
            alerts.append(("crit", f"missing:{uuid}",
                           f"Array {name or uuid[:8]} is set up on this Pi but didn't start. A drive may be "
                           "unplugged or dead. Open RAID Forge > My Arrays > Find & start existing arrays."))
    return alerts, infos, rows


def check_smart(disks, smart, previous, conf, now=None):
    """disks: [{name, rota}], smart: {name: smartctl json}. previous: {name: {'bad': n}}.
    Returns (alerts, new_previous)."""
    alerts, prev = [], dict(previous)
    now = now or time.time()
    for d in disks:
        j = smart.get(d["name"])
        if not j:
            continue
        n = d["name"]
        if j.get("smart_status", {}).get("passed") is False:
            alerts.append(("crit", f"smart:{n}", f"/dev/{n} says it is FAILING (SMART). Back up and replace it now."))
        temp = j.get("temperature", {}).get("current")
        if temp and temp >= conf["temp_warn"] and not conf.get("temp_alarm"):
            alerts.append(("warn", f"temp:{n}", f"/dev/{n} is running hot: {temp} °C. Check its fan/airflow."))
        bad = 0
        for attr in j.get("ata_smart_attributes", {}).get("table", []):
            if attr.get("id") in (5, 197, 198):
                bad += int(attr.get("raw", {}).get("value", 0) or 0)
        nv = j.get("nvme_smart_health_information_log", {})
        bad += int(nv.get("media_errors", 0) or 0)
        p = dict(prev.get(n, {}))
        if bad and bad > p.get("bad", 0):
            p["grew_at"], p["more"] = now, bool(p.get("bad"))
        p["bad"] = max(bad, p.get("bad", 0))
        if p.get("grew_at") and now - p["grew_at"] < 24 * 3600:     # keep it showing for a day, then settle
            alerts.append(("warn", f"bad:{n}", f"/dev/{n} has {p['bad']} bad/unreadable sectors"
                                               + (" (more than before)." if p.get("more") else ".")
                                               + " Keep an eye on it; replace it if the number keeps growing."))
        prev[n] = p
        w = None
        if not d.get("rota"):
            if nv.get("percentage_used") is not None:
                w = int(nv["percentage_used"])
            else:
                for attr in j.get("ata_smart_attributes", {}).get("table", []):
                    if attr.get("id") in (177, 231, 233, 202) and attr.get("value") and 0 < attr["value"] <= 100:
                        w = 100 - int(attr["value"])
                        break
        if w is not None and w >= conf["wear_warn"]:
            alerts.append(("warn", f"wear:{n}", f"/dev/{n} has used {w}% of its rated writes. Plan a replacement."))
    return alerts, prev


def check_temps(temps, cpu, conf, hot_before):
    """Over-temperature alarm. temps: {disk: °C}, cpu: °C or None. hot_before: {id: level} from the last round.
    Alarms at the limit, warns 5 °C below it, and only clears 3 °C under where it tripped (no flapping).
    Returns (alerts, hot_now)."""
    alerts, hot = [], {}
    items = [(f"temp:{d}", f"/dev/{d}", c, conf["temp_alarm_drive"], "Check its fan/airflow and the case vents.")
             for d, c in sorted(temps.items()) if c is not None]
    if cpu is not None:
        items.append(("temp:cpu", "The Pi's processor", cpu, conf["temp_alarm_cpu"],
                      "Check the Pi's fan/heatsink. It slows itself down at 85 °C."))
    for aid, what, c, limit, tip in items:
        prev = hot_before.get(aid)
        crit_at = limit - (3 if prev == "crit" else 0)
        warn_at = limit - 5 - (3 if prev in ("warn", "crit") else 0)
        if c >= crit_at:
            hot[aid] = "crit"
            alerts.append(("crit", aid, f"OVER TEMPERATURE: {what} is at {c:.0f} °C (alarm at {limit} °C). {tip}"))
        elif c >= warn_at:
            hot[aid] = "warn"
            alerts.append(("warn", aid, f"{what} is getting hot: {c:.0f} °C (alarm at {limit} °C). {tip}"))
    return alerts, hot


def worst(alerts, infos):
    if any(a[0] == "crit" for a in alerts):
        return "crit"
    if alerts:
        return "warn"
    return "info" if infos else "ok"


# ================================================================== daemon (root)
class Daemon:
    def __init__(self):
        self.conf = load_conf()
        self.stop = threading.Event()
        os.makedirs(RUN_DIR, exist_ok=True)
        os.chmod(RUN_DIR, 0o1777)                    # the window (your user) writes ack.json here
        os.makedirs(STATE_DIR, exist_ok=True)
        self.state = load_json(os.path.join(STATE_DIR, "state.json"),
                               {"seen": {}, "smart_prev": {}, "had_spares": {}, "events": []})
        self.smart = {}
        self.smart_alerts = []
        self.smart_at = 0
        self.led = None                              # (path, original trigger) while flashing
        self.temps, self.cpu, self.temps_at, self.hot = {}, None, 0, {}
        self.lock = threading.Lock()                 # the web page can ask for a fresh check
        self.web = None
        run(["modprobe", "drivetemp"], timeout=10)   # lets SATA/USB drives report temperature without smartctl
        self.log(f"{APP} {VERSION} started")

    def log(self, msg):
        print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)

    # ---- inputs
    def configured_arrays(self):
        out = []
        for line in read(MDADM_CONF).splitlines():
            if line.strip().startswith("ARRAY"):
                u = re.search(r"UUID=(\S+)", line)
                nm = re.search(r"name=(?:\S+:)?(\S+)", line)
                if u:
                    out.append((u.group(1), nm.group(1) if nm else ""))
        return out

    def arrays(self):
        arrays = parse_mdstat(read(MDSTAT))
        for a in arrays:
            rc, out = run(["mdadm", "--detail", "--export", f"/dev/{a['name']}"], timeout=10)
            kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
            a["uuid"] = kv.get("MD_UUID", "")
            if kv.get("MD_DEVNAME"):
                a["alias"] = "/dev/md/" + kv["MD_DEVNAME"]
            had = self.state["had_spares"].get(a["name"], 0)
            now = sum(1 for _, s in a["devices"] if s == "spare")
            a["had_spares"] = had
            if now:
                self.state["had_spares"][a["name"]] = now
            elif had and not ("_" in a["health"]):
                # remember until the user adds a new spare; the alert keeps showing
                pass
        return arrays

    def mounts(self, arrays):
        out = {}
        for a in arrays:
            rc, mp = run(["findmnt", "-no", "TARGET", "-S", f"/dev/{a['name']}"], timeout=5)
            mp = mp.strip().splitlines()[0] if mp.strip() else ""
            if mp:
                st = os.statvfs(mp)
                total = st.f_blocks * st.f_frsize
                used = (st.f_blocks - st.f_bfree) * st.f_frsize
                out[a["name"]] = (mp, 100.0 * used / total if total else None, st.f_bavail * st.f_frsize, total)
        return out

    def read_smart(self, arrays):
        if not shutil.which("smartctl"):
            return
        names = sorted({disk_of(d) for a in arrays for d, s in a["devices"]})
        rc, out = run(["lsblk", "-J", "-o", "NAME,TYPE,ROTA,TRAN"], timeout=10)
        try:
            info = {d["name"]: d for d in json.loads(out)["blockdevices"] if d.get("type") == "disk"}
        except (ValueError, KeyError):
            info = {}
        disks, smart = [], {}
        for n in names:
            d = info.get(n, {"name": n})
            args = ["smartctl", "-H", "-A", "-j", f"/dev/{n}"]
            if d.get("tran") == "usb":
                args[1:1] = ["-d", "sat"]
            rc, o = run(args, timeout=60)
            try:
                smart[n] = json.loads(o)
            except ValueError:
                continue
            disks.append({"name": n, "rota": bool(d.get("rota"))})
        self.smart_alerts, self.state["smart_prev"] = check_smart(disks, smart, self.state["smart_prev"], self.conf)
        self.smart_at = time.time()

    def read_temps(self, arrays):
        """Drive temperatures (hwmon first, smartctl if a drive has none) + the Pi's CPU temperature."""
        names = {disk_of(d) for a in arrays for d, s in a["devices"]}
        temps = {}
        for hw in sorted(os.listdir("/sys/class/hwmon")) if os.path.isdir("/sys/class/hwmon") else []:
            base = f"/sys/class/hwmon/{hw}"
            kind = read(base + "/name").strip()
            dev = os.path.realpath(base + "/device")
            disk = None
            if kind == "nvme":
                m = re.search(r"/nvme/(nvme\d+)", dev) or re.search(r"(nvme\d+)$", dev)
                ctrl = m.group(1) if m else ""
                disk = next((n for n in names if n.startswith(ctrl + "n")), None) if ctrl else None
            elif kind == "drivetemp":
                blk = os.path.join(dev, "block")
                disk = next(iter(os.listdir(blk)), None) if os.path.isdir(blk) else None
            v = read(base + "/temp1_input").strip()
            if disk in names and v.lstrip("-").isdigit():
                temps[disk] = int(v) / 1000
        if shutil.which("smartctl"):
            for n in sorted(names - set(temps)):
                rc, o = run(["smartctl", "-A", "-j", f"/dev/{n}"], timeout=30)
                try:
                    c = json.loads(o).get("temperature", {}).get("current")
                except ValueError:
                    c = None
                if c is None:
                    rc, o = run(["smartctl", "-d", "sat", "-A", "-j", f"/dev/{n}"], timeout=30)
                    try:
                        c = json.loads(o).get("temperature", {}).get("current")
                    except ValueError:
                        c = None
                if c is not None:
                    temps[n] = float(c)
        v = read("/sys/class/thermal/thermal_zone0/temp").strip()
        self.cpu = int(v) / 1000 if v.lstrip("-").isdigit() else None
        self.temps, self.temps_at = temps, time.time()

    # ---- outputs
    def led_alarm(self, on):
        if not self.conf["led_alarm"]:
            on = False
        path = next((p for p in ("/sys/class/leds/ACT", "/sys/class/leds/led0") if os.path.isdir(p)), None)
        if not path:
            return
        try:
            if on and not self.led:
                trig = re.search(r"\[(\S+)\]", read(path + "/trigger"))
                self.led = (path, trig.group(1) if trig else "mmc0")
                with open(path + "/trigger", "w") as f:
                    f.write("timer")
                for k, v in (("delay_on", "100"), ("delay_off", "100")):
                    with open(f"{path}/{k}", "w") as f:
                        f.write(v)
            elif not on and self.led:
                with open(self.led[0] + "/trigger", "w") as f:
                    f.write(self.led[1])
                self.led = None
        except OSError:
            pass

    def push(self, level, title, text):
        topic = (self.conf.get("ntfy_topic") or "").strip()
        if not topic:
            return
        import urllib.request
        url = self.conf["ntfy_server"].rstrip("/") + "/" + topic
        prio = {"crit": "5", "warn": "3", "ok": "2"}.get(level, "3")
        tags = {"crit": "rotating_light", "warn": "warning", "ok": "white_check_mark"}.get(level, "")
        host = os.uname().nodename

        def go():
            try:
                req = urllib.request.Request(url, data=text.encode(), method="POST",
                                             headers={"Title": f"{host}: {title}", "Priority": prio, "Tags": tags})
                urllib.request.urlopen(req, timeout=15).read()
            except Exception as e:  # noqa: BLE001 - never let a network hiccup stop the watching
                self.log(f"phone push failed: {e}")
        threading.Thread(target=go, daemon=True).start()

    def manage_web(self):
        want = self.conf["web"] and bool(self.conf["web_password"])
        if self.conf["web"] and not self.conf["web_password"]:
            self.log("web page is on but has no password set, so it stays off")
        if self.web and (not want or self.web.port != self.conf["web_port"]):
            self.web.close()
            self.web = None
            self.log("web page stopped")
        if want and not self.web:
            try:
                self.web = WebUI(self, self.conf["web_port"])
                self.log(f"web page on port {self.conf['web_port']} (password protected)")
            except OSError as e:
                self.log(f"web page couldn't start on port {self.conf['web_port']}: {e}")

    def refresh_now(self):
        threading.Thread(target=self.tick, daemon=True).start()

    # ---- one round
    def tick(self):
        with self.lock:
            return self._tick()

    def _tick(self):
        self.conf = load_conf()
        self.manage_web()
        arrays = self.arrays()
        if time.time() - self.smart_at > self.conf["smart_hours"] * 3600:
            self.read_smart(arrays)
        alerts, infos, rows = check_arrays(arrays, self.configured_arrays(), read, self.mounts(arrays), self.conf)
        alerts += self.smart_alerts
        if self.conf["temp_alarm"]:
            if time.time() - self.temps_at > self.conf["temp_interval_s"]:
                self.read_temps(arrays)
            t_alerts, self.hot = check_temps(self.temps, self.cpu, self.conf, self.hot)
            alerts += t_alerts
        else:
            self.hot = {}
        try:
            if time.time() - os.path.getmtime(TEST_FLAG) < 60:
                alerts.append(("crit", "test", "TEST ALARM: this is only a test. Everything is fine."))
            else:
                os.remove(TEST_FLAG)
        except OSError:
            pass
        now = time.time()
        level = worst(alerts, infos)
        active = {aid for _, aid, _ in alerts}

        # acknowledgements from the window: drop stale ones, and expire after remind_hours (alarm again)
        ack = load_json(ACK, {})
        ack = {k: v for k, v in ack.items() if k in active and now - v < self.conf["remind_hours"] * 3600}

        # new alerts -> events + push; cleared alerts -> recovery
        seen = self.state["seen"]
        new, fixed = [], []
        for lvl, aid, text in alerts:
            if aid not in seen:
                seen[aid] = {"level": lvl, "since": now, "text": text}
                self.event(lvl, text)
                new.append((lvl, text))
            elif LEVELS[lvl] > LEVELS[seen[aid]["level"]]:      # got worse: alarm again, even if silenced
                seen[aid].update(level=lvl, text=text)
                ack.pop(aid, None)
                self.event(lvl, text)
                new.append((lvl, text))
            else:
                seen[aid].update(level=lvl, text=text)
        for aid in [k for k in seen if k not in active]:
            old = seen.pop(aid)
            msg = "Resolved: " + old["text"].split(". ")[0] + "."
            self.event("ok", msg)
            if aid != "test":
                fixed.append(msg)
        # one phone notification per check, however many things changed
        if new:
            top = "crit" if any(l == "crit" for l, _ in new) else "warn"
            self.push(top, "RAID needs service" if top == "crit" else "RAID warning",
                      "\n".join(t for _, t in sorted(new, key=lambda x: -LEVELS[x[0]])))
        if fixed:
            self.push("ok", "RAID back to normal" if not active else "RAID problem resolved", "\n".join(fixed))
        try:
            write_json(ACK, ack, 0o666)
        except OSError:
            pass
        unacked_crit = [aid for lvl, aid, _ in alerts if lvl == "crit" and aid not in ack]
        self.led_alarm(bool(unacked_crit))
        status = {
            "time": now, "level": level, "host": os.uname().nodename, "version": VERSION,
            "arrays": rows, "infos": infos,
            "alerts": [{"level": l, "id": i, "text": t, "since": seen[i]["since"], "acked": i in ack}
                       for l, i, t in sorted(alerts, key=lambda x: -LEVELS[x[0]])],
            "events": self.state["events"][-30:], "smart_checked": self.smart_at,
            "sound": self.conf["sound"], "led_alarm": self.conf["led_alarm"],
            "temp_alarm": self.conf["temp_alarm"], "temps": self.temps if self.conf["temp_alarm"] else {},
            "cpu_temp": self.cpu if self.conf["temp_alarm"] else None,
            "temp_limits": [self.conf["temp_alarm_drive"], self.conf["temp_alarm_cpu"]],
        }
        write_json(STATUS, status)
        write_json(os.path.join(STATE_DIR, "state.json"), self.state)
        return status

    def event(self, level, text):
        self.state["events"].append({"time": time.time(), "level": level, "text": text})
        self.state["events"] = self.state["events"][-100:]
        self.log(f"[{level}] {text}")

    def loop(self):
        def bye(*_):
            self.stop.set()
        signal.signal(signal.SIGTERM, bye)
        signal.signal(signal.SIGINT, bye)
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - keep watching no matter what
                self.log(f"check failed: {e}")
            self.stop.wait(self.conf["interval_s"])
        self.led_alarm(False)
        if self.web:
            self.web.close()
        self.log("stopped")


# ================================================================== password-protected web page (optional)
def make_hash(password, iterations=200_000):
    import hashlib
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${h.hex()}"


def check_hash(password, stored):
    import hashlib
    import hmac
    try:
        algo, it, salt, h = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, AttributeError):
        return False


def save_conf_key(key, value):
    raw = load_json(CONF, {})
    raw[key] = value
    write_json(CONF, raw, 0o600)


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>RAID Watch</title><style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f1419;color:#e6edf3;
font:16px/1.5 system-ui,"DejaVu Sans",sans-serif;padding:16px;box-sizing:border-box}
form{background:#161d26;border:1px solid #2a3646;border-radius:16px;padding:26px;width:100%;max-width:340px}
h1{margin:0 0 4px;font-size:22px} p{color:#8b98a9;margin:4px 0 16px;font-size:14px}
input{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #2a3646;background:#0f1419;
color:#e6edf3;font:inherit} button{margin-top:12px;width:100%;padding:12px;border:0;border-radius:10px;
background:#c51a4a;color:#fff;font:inherit;font-weight:700;cursor:pointer} .err{color:#f85149;font-size:14px;min-height:20px}
</style></head><body><form method="post" action="/login"><h1>RAID Watch</h1><p>%HOST%</p>
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
<div class="err">%ERR%</div><button>Sign in</button></form></body></html>"""

DASH_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>RAID Watch</title><style>
:root{--bg:#0f1419;--panel:#161d26;--card:#1d2632;--line:#2a3646;--fg:#e6edf3;--mut:#8b98a9;
--ok:#3fb950;--info:#58a6ff;--warn:#d29922;--crit:#f85149}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,"DejaVu Sans",sans-serif}
main{max-width:720px;margin:0 auto;padding:16px}
header{display:flex;align-items:center;gap:14px;margin:6px 0 14px}
#lamp{width:44px;height:44px;border-radius:50%;background:var(--line);flex:none}
h1{margin:0;font-size:22px} #sub{color:var(--mut);font-size:13px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px;margin:10px 0}
.card h2{margin:0 0 8px;font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}
.row{display:flex;gap:10px;align-items:flex-start;padding:6px 0} .dot{width:10px;height:10px;border-radius:50%;
margin-top:6px;flex:none} .row.acked{opacity:.6} .mut{color:var(--mut)}
button{appearance:none;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:10px;
padding:10px 14px;font:inherit;cursor:pointer} button.red{background:var(--crit);border-color:var(--crit);color:#fff;font-weight:700}
.btns{display:flex;gap:8px;flex-wrap:wrap} .sw{display:flex;justify-content:space-between;align-items:center;
padding:8px 0;border-top:1px solid var(--line)} .sw:first-of-type{border-top:0}
.tog{flex:none;width:52px;height:30px;border-radius:15px;background:var(--line);position:relative;border:0;padding:0}
.tog::after{content:"";position:absolute;top:3px;left:3px;width:24px;height:24px;border-radius:50%;background:#fff;transition:.15s}
.tog.on{background:var(--ok)} .tog.on::after{left:25px}
.temps{display:flex;gap:8px;flex-wrap:wrap} .t{background:var(--card);border-radius:10px;padding:6px 10px;font-size:14px}
body.alarm{animation:fl 1s steps(1) infinite} @keyframes fl{50%{background:#3a1216}}
footer{display:flex;justify-content:space-between;color:var(--mut);font-size:13px;margin-top:14px}
footer form button{padding:6px 10px;font-size:13px}
</style></head><body><main>
<header><div id="lamp"></div><div><h1 id="head">Loading…</h1><div id="sub"></div></div></header>
<div class="card"><h2>Status</h2><div id="list"></div><div class="btns" style="margin-top:10px">
<button class="red" id="sil" hidden>Silence alarm</button><button id="test">Test alarm</button></div></div>
<div class="card" id="tcard" hidden><h2>Temperatures</h2><div class="temps" id="temps"></div></div>
<div class="card"><h2>Arrays</h2><div id="arrays"></div></div>
<div class="card"><h2>Settings</h2>
<div class="sw"><span>Alarm sound on the Pi</span><button class="tog" data-k="sound"></button></div>
<div class="sw"><span>Flash the Pi's green light</span><button class="tog" data-k="led_alarm"></button></div>
<div class="sw"><span>Over-temperature alarm <span class="mut" id="lim"></span></span><button class="tog" data-k="temp_alarm"></button></div>
</div>
<div class="card"><h2>Recent</h2><div id="events"></div></div>
<footer><span id="ver"></span><form method="post" action="/logout"><input type="hidden" name="csrf" value="%CSRF%"><button>Sign out</button></form></footer>
</main><script>
const CSRF="%CSRF%", C={ok:"var(--ok)",info:"var(--info)",warn:"var(--warn)",crit:"var(--crit)"};
const W={ok:"All RAID arrays healthy",info:"Healthy, working on something",warn:"Needs a look",crit:"RAID NEEDS SERVICE"};
const $=id=>document.getElementById(id);
function hb(n){const u=["B","KB","MB","GB","TB","PB"];let i=0;while(n>=1000&&i<5){n/=1000;i++}return n.toFixed(i<2?0:1)+" "+u[i]}
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function row(level,text,acked){const r=el("div","row"+(acked?" acked":""));const d=el("span","dot");d.style.background=C[level];
 r.append(d,el("span","",text+(acked?"  (silenced)":"")));return r}
async function api(path,body){const r=await fetch(path,{method:body?"POST":"GET",headers:{"X-CSRF":CSRF,
 "Content-Type":"application/json"},body:body?JSON.stringify(body):undefined,credentials:"same-origin"});
 if(r.status===401){location.reload();return null} return r.json()}
async function load(){const s=await api("/api/status");if(!s)return;
 const lv=s.level, un=(s.alerts||[]).some(a=>a.level==="crit"&&!a.acked);
 $("head").textContent=W[lv]||lv;$("head").style.color=lv==="ok"?"":C[lv];$("lamp").style.background=C[lv];
 $("sub").textContent=s.host+" · checked "+new Date(s.time*1000).toLocaleTimeString();
 document.body.classList.toggle("alarm",un);$("sil").hidden=!(s.alerts||[]).some(a=>!a.acked);
 const L=$("list");L.replaceChildren();(s.alerts||[]).forEach(a=>L.append(row(a.level,a.text,a.acked)));
 (s.infos||[]).forEach(i=>L.append(row("info",i)));if(!L.children.length)L.append(row("ok","Everything is healthy."));
 const A=$("arrays");A.replaceChildren();(s.arrays||[]).forEach(r=>A.append(row(r.state==="DEGRADED"?"crit":"ok",
  r.label+"  "+r.level.toUpperCase()+" ["+r.health+"]  "+(r.mount||"not mounted")+(r.free!=null?", "+hb(r.free)+" free of "+hb(r.total)+" ("+(100-r.used).toFixed(0)+"%)":r.used!=null?", "+r.used.toFixed(0)+"% full":""))));
 if(!A.children.length)A.append(el("div","mut","No RAID arrays yet."));
 const T=$("temps");T.replaceChildren();const ts=Object.entries(s.temps||{});
 if(s.cpu_temp!=null)ts.unshift(["CPU",s.cpu_temp]);ts.forEach(([k,v])=>T.append(el("span","t",k+" "+Math.round(v)+" °C")));
 $("tcard").hidden=!ts.length;$("lim").textContent=s.temp_limits?"(drives "+s.temp_limits[0]+" °C, CPU "+s.temp_limits[1]+" °C)":"";
 document.querySelectorAll(".tog").forEach(b=>b.classList.toggle("on",!!s[b.dataset.k]));
 const E=$("events");E.replaceChildren();(s.events||[]).slice(-6).reverse().forEach(e=>E.append(row(e.level,
  new Date(e.time*1000).toLocaleString([], {weekday:"short",hour:"2-digit",minute:"2-digit"})+"  "+e.text)));
 $("ver").textContent="RAID Watch "+s.version}
$("sil").onclick=async()=>{await api("/api/silence",{});load()};
$("test").onclick=async()=>{$("test").textContent="Test sent…";await api("/api/test",{});setTimeout(()=>{$("test").textContent="Test alarm";load()},1500)};
document.querySelectorAll(".tog").forEach(b=>b.onclick=async()=>{await api("/api/toggle",{key:b.dataset.k});load()});
load();setInterval(()=>{if(!document.hidden)load()},5000);
</script></body></html>"""


class WebUI:
    """Small password-protected status page for phones/PCs on the home network."""
    TOGGLES = ("sound", "led_alarm", "temp_alarm")

    def __init__(self, daemon, port):
        import http.server
        import secrets
        self.d, self.port = daemon, port
        self.sessions, self.fails = {}, {}
        web = self

        class H(http.server.BaseHTTPRequestHandler):
            server_version = "RAIDWatch"
            sys_version = ""

            def log_message(self, *a):
                pass

            def send(self, code, body, ctype="text/html; charset=utf-8", headers=()):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                 "connect-src 'self'; form-action 'self'; frame-ancestors 'none'")
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def session(self):
                m = re.search(r"(?:^|;\s*)rw_session=([A-Za-z0-9_-]+)", self.headers.get("Cookie", ""))
                s = web.sessions.get(m.group(1)) if m else None
                if s and s["exp"] > time.time():
                    return m.group(1), s
                return None, None

            def body(self):
                n = min(int(self.headers.get("Content-Length") or 0), 4096)
                return self.rfile.read(n).decode("utf-8", "replace")

            def login_page(self, err="", code=200):
                html = LOGIN_HTML.replace("%HOST%", os.uname().nodename).replace("%ERR%", err)
                self.send(code, html)

            def do_GET(self):
                tok, s = self.session()
                if self.path == "/":
                    return self.send(200, DASH_HTML.replace("%CSRF%", s["csrf"])) if s else self.login_page()
                if self.path == "/api/status":
                    if not s:
                        return self.send(401, "{}", "application/json")
                    return self.send(200, json.dumps(load_json(STATUS, {})), "application/json")
                self.send(404, "Not found", "text/plain")

            def do_POST(self):
                ip = self.client_address[0]
                if self.path == "/login":
                    f = web.fails.get(ip, {"n": 0, "until": 0})
                    if f["until"] > time.time():
                        return self.login_page(f"Too many wrong passwords. Try again in "
                                               f"{int(f['until'] - time.time()) // 60 + 1} min.", 429)
                    from urllib.parse import parse_qs
                    pw = parse_qs(self.body()).get("password", [""])[0]
                    stored = load_conf().get("web_password", "")
                    if stored and check_hash(pw, stored):
                        web.fails.pop(ip, None)
                        tok = secrets.token_urlsafe(32)
                        web.sessions[tok] = {"exp": time.time() + 12 * 3600, "csrf": secrets.token_urlsafe(24)}
                        web.d.log(f"web: signed in from {ip}")
                        return self.send(303, "", headers=(("Location", "/"), (
                            "Set-Cookie", f"rw_session={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200")))
                    f["n"] += 1
                    if f["n"] >= 5:
                        f = {"n": 0, "until": time.time() + 300}
                        web.d.log(f"web: 5 wrong passwords from {ip}, locked for 5 minutes")
                    web.fails[ip] = f
                    time.sleep(1)                                   # slow down guessing
                    return self.login_page("Wrong password.", 401)
                tok, s = self.session()
                if self.path == "/logout":
                    from urllib.parse import parse_qs
                    if s and parse_qs(self.body()).get("csrf", [""])[0] == s["csrf"]:
                        web.sessions.pop(tok, None)
                    return self.send(303, "", headers=(("Location", "/"),
                                                       ("Set-Cookie", "rw_session=; Path=/; Max-Age=0")))
                if not s:
                    return self.send(401, "{}", "application/json")
                if self.headers.get("X-CSRF") != s["csrf"]:
                    return self.send(403, '{"error":"bad token"}', "application/json")
                try:
                    data = json.loads(self.body() or "{}")
                except ValueError:
                    data = {}
                if self.path == "/api/silence":
                    st = load_json(STATUS, {})
                    ack = load_json(ACK, {})
                    ack.update({a["id"]: time.time() for a in st.get("alerts", [])})
                    write_json(ACK, ack, 0o666)
                    web.d.log(f"web: alarm silenced from {ip}")
                elif self.path == "/api/test":
                    with open(TEST_FLAG, "w") as f:
                        f.write("web\n")
                    web.d.log(f"web: test alarm from {ip}")
                elif self.path == "/api/toggle" and data.get("key") in WebUI.TOGGLES:
                    k = data["key"]
                    save_conf_key(k, not load_conf()[k])
                    web.d.log(f"web: {k} turned {'on' if load_conf()[k] else 'off'} from {ip}")
                else:
                    return self.send(404, "{}", "application/json")
                web.d.refresh_now()
                self.send(200, '{"ok":true}', "application/json")

        self.httpd = http.server.ThreadingHTTPServer(("", port), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ================================================================== the window (your desktop)
def alarm_wav(path):
    """A short two-tone alarm, made once."""
    if os.path.exists(path):
        return path
    import math
    import struct
    import wave
    rate, frames = 22050, bytearray()
    for tone in (880, 660, 880, 660):
        for i in range(int(rate * 0.18)):
            env = min(1.0, i / 200, (rate * 0.18 - i) / 200)
            frames += struct.pack("<h", int(14000 * env * math.sin(2 * math.pi * tone * i / rate)))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def play(path):
    for cmd in (["pw-play", path], ["paplay", path], ["aplay", "-q", path]):
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except OSError:
                pass
    return False


def notify(level, title, body):
    if shutil.which("notify-send"):
        urg = {"crit": "critical", "warn": "normal"}.get(level, "low")
        subprocess.Popen(["notify-send", "-u", urg, "-a", APP, "-i", "drive-harddisk", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def demo_status():
    now = time.time()
    return {"time": now, "level": "crit", "host": "pi5", "version": VERSION, "sound": True,
            "arrays": [{"name": "md127", "label": "/dev/md/vault", "level": "raid1", "state": "DEGRADED",
                        "health": "U_", "op": "", "pct": None, "finish": "", "mount": "/mnt/vault", "used": 61.2, "free": 1_529_000_000_000, "total": 3_940_000_000_000,
                        "failed": ["sdb"], "spares": []},
                       {"name": "md126", "label": "/dev/md/media", "level": "raid5", "state": "active",
                        "health": "UUU", "op": "check", "pct": 42.0, "finish": "38", "mount": "/mnt/media",
                        "used": 91.4, "free": 516_000_000_000, "total": 6_000_000_000_000, "failed": [], "spares": []}],
            "infos": ["/dev/md/media: Checking 42.0%, about 38 min left"],
            "alerts": [{"level": "crit", "id": "failed:md127", "acked": False, "since": now - 300,
                        "text": "/dev/md/vault: drive /dev/sdb failed. Replace it soon (RAID Forge > My Arrays > "
                                "Replace a drive)."},
                       {"level": "crit", "id": "degraded:md127", "acked": False, "since": now - 300,
                        "text": "/dev/md/vault is DEGRADED: running on 1 of 2 drives (1 missing). It has no "
                                "protection left. Replace the drive."},
                       {"level": "warn", "id": "full:md126", "acked": False, "since": now - 3600,
                        "text": "/dev/md/media (/mnt/media) is 91% full."}],
            "events": [{"time": now - 86400, "level": "ok", "text": "RAID Watch started"},
                       {"time": now - 3600, "level": "warn", "text": "/dev/md/media (/mnt/media) is 91% full."},
                       {"time": now - 300, "level": "crit", "text": "/dev/md/vault: drive /dev/sdb failed."}],
            "smart_checked": now - 1800, "temp_alarm": True, "temps": {"sda": 44.0, "sdb": 46.0},
            "cpu_temp": 57.3, "temp_limits": [65, 80]}


def window():
    import tkinter as tk
    from tkinter import ttk
    BG, PANEL, CARD, LINE, FG, MUTED = "#0f1419", "#161d26", "#1d2632", "#2a3646", "#e6edf3", "#8b98a9"
    COL = {"ok": "#3fb950", "info": "#58a6ff", "warn": "#d29922", "crit": "#f85149"}
    WORD = {"ok": "All RAID arrays healthy", "info": "Healthy, working on something",
            "warn": "Needs a look", "crit": "RAID NEEDS SERVICE"}
    here = os.path.dirname(os.path.abspath(__file__))
    wav = alarm_wav(os.path.expanduser("~/.cache/raidwatch/alarm.wav"))
    root = tk.Tk()
    root.title(APP + (" (demo)" if DEMO else ""))
    root.geometry("480x430")
    root.minsize(380, 260)
    root.configure(bg=BG)
    try:
        root.iconphoto(True, tk.PhotoImage(file=os.path.join(here, "raidwatch.png")))
    except Exception:  # noqa: BLE001 - icon is optional
        pass
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure("TButton", background=CARD, foreground=FG, borderwidth=1, padding=(10, 5))
    st.map("TButton", background=[("active", LINE)])
    st.configure("Alarm.TButton", background=COL["crit"], foreground="white", font=("DejaVu Sans", 10, "bold"))

    top = tk.Frame(root, bg=BG)
    top.pack(fill="x", padx=14, pady=(12, 6))
    lamp_c = tk.Canvas(top, width=46, height=46, bg=BG, highlightthickness=0)
    lamp_c.pack(side="left")
    lamp = lamp_c.create_oval(4, 4, 42, 42, fill=COL["ok"], outline="")
    tt = tk.Frame(top, bg=BG)
    tt.pack(side="left", padx=10, fill="x", expand=True)
    head = tk.Label(tt, text="Starting…", bg=BG, fg=FG, font=("DejaVu Sans", 14, "bold"), anchor="w")
    head.pack(fill="x")
    sub = tk.Label(tt, text="", bg=BG, fg=MUTED, anchor="w", font=("DejaVu Sans", 9))
    sub.pack(fill="x")

    body = tk.Text(root, bg=PANEL, fg=FG, bd=0, highlightthickness=0, wrap="word", height=8,
                   font=("DejaVu Sans", 10), padx=10, pady=8)
    body.pack(fill="both", expand=True, padx=14)
    for k, c in COL.items():
        body.tag_config(k, foreground=c)
    body.tag_config("muted", foreground=MUTED)
    body.tag_config("bold", font=("DejaVu Sans", 10, "bold"))

    bf = tk.Frame(root, bg=BG)
    bf.pack(fill="x", padx=14, pady=10)
    state = {"level": None, "flash": False, "unacked": [], "last_sound": 0, "status": None, "notified": set()}

    def silence():
        ids = [a["id"] for a in (state["status"] or {}).get("alerts", [])]
        if DEMO:
            for a in state["status"]["alerts"]:
                a["acked"] = True
        else:
            ack = load_json(ACK, {})
            ack.update({i: time.time() for i in ids})
            try:
                write_json(ACK, ack, 0o666)
            except OSError:
                pass
        state["unacked"] = []
        refresh(force=True)

    sil = ttk.Button(bf, text="Silence alarm", style="Alarm.TButton", command=silence)

    def open_forge():
        for p in (os.path.join(here, "raidforge.py"), "/opt/raidforge/raidforge.py"):
            if os.path.exists(p):
                subprocess.Popen([sys.executable, p] + (["--demo"] if DEMO else []))
                return
    ttk.Button(bf, text="Open RAID Forge", command=open_forge).pack(side="right")
    ttk.Button(bf, text="Hide", command=root.iconify).pack(side="right", padx=6)

    def draw(s):
        body.configure(state="normal")
        body.delete("1.0", "end")
        for a in s.get("alerts", []):
            body.insert("end", ("● " if not a["acked"] else "○ "), a["level"])
            body.insert("end", a["text"] + ("  (silenced)" if a["acked"] else "") + "\n\n")
        for i in s.get("infos", []):
            body.insert("end", "● ", "info")
            body.insert("end", i + "\n\n")
        if not s.get("alerts"):
            for r in s.get("arrays", []):
                used = (f", {human_bytes(r['free'])} free of {human_bytes(r['total'])}" if r.get("free") is not None
                        else f", {r['used']:.0f}% full" if r.get("used") is not None else "")
                body.insert("end", "● ", "ok")
                body.insert("end", f"{r['label']}  ", "bold")
                body.insert("end", f"{r['level'].upper()} [{r['health']}]  {r['mount'] or 'not mounted'}{used}\n")
            if not s.get("arrays"):
                body.insert("end", "No RAID arrays on this Pi yet. Build one in RAID Forge.\n", "muted")
        spaced = [r for r in s.get("arrays", []) if r.get("free") is not None]
        if s.get("alerts") and spaced:
            body.insert("end", "\nFree space  ", "muted")
            body.insert("end", "  ·  ".join(f"{r['label'].split('/')[-1]} {human_bytes(r['free'])} "
                                            f"({100 - r['used']:.0f}%)" for r in spaced) + "\n")
        temps = list((s.get("temps") or {}).items())
        if s.get("cpu_temp") is not None:
            temps.insert(0, ("CPU", s["cpu_temp"]))
        if temps:
            body.insert("end", "\nTemperatures  ", "muted")
            body.insert("end", "  ·  ".join(f"{k} {v:.0f} °C" for k, v in temps) + "\n")
        ev = s.get("events", [])[-4:]
        if ev:
            body.insert("end", "\nRecent\n", "muted")
            for e in reversed(ev):
                body.insert("end", time.strftime("%a %H:%M  ", time.localtime(e["time"])), "muted")
                body.insert("end", e["text"][:110] + "\n", e["level"])
        body.configure(state="disabled")

    def refresh(force=False):
        s = demo_state if DEMO else load_json(STATUS, None)
        if s is None or (not DEMO and time.time() - s.get("time", 0) > 120):
            lv = "warn"
            head.configure(text="RAID Watch isn't running", fg=COL["warn"])
            sub.configure(text="Turn it on in RAID Forge > My Arrays > RAID Watch")
            lamp_c.itemconfigure(lamp, fill=COL["warn"])
            state.update(level=lv, unacked=[], status={"alerts": []})
            sil.pack_forget()
            return
        state["status"] = s
        lv = s["level"]
        unacked = [a for a in s.get("alerts", []) if a["level"] == "crit" and not a["acked"]]
        active_ids = {a["id"] for a in s.get("alerts", [])}
        for a in s.get("alerts", []):
            if a["id"] not in state["notified"] and not a["acked"]:
                notify(a["level"], "RAID needs service" if a["level"] == "crit" else "RAID warning", a["text"])
        state["notified"] = active_ids
        if state["level"] in ("crit", "warn") and lv in ("ok", "info") and not force:
            notify("ok", "RAID back to normal", "All arrays are healthy again.")
        state["level"], state["unacked"] = lv, unacked
        head.configure(text=WORD[lv], fg=COL[lv] if lv != "ok" else FG)
        n = len(s.get("arrays", []))
        chk = s.get("smart_checked") or 0
        sub.configure(text=f"{s.get('host', '')} · {n} arra{'y' if n == 1 else 'ys'} · checked "
                           f"{time.strftime('%H:%M:%S', time.localtime(s['time']))}"
                           + (f" · drives checked {time.strftime('%H:%M', time.localtime(chk))}" if chk else ""))
        if unacked:
            sil.pack(side="left")
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
        else:
            sil.pack_forget()
            root.attributes("-topmost", False)
            lamp_c.itemconfigure(lamp, fill=COL[lv])
        draw(s)

    def flash():
        if state["unacked"]:
            state["flash"] = not state["flash"]
            lamp_c.itemconfigure(lamp, fill=COL["crit"] if state["flash"] else LINE)
            root.configure(bg="#3a1216" if state["flash"] else BG)
            for w in (top, tt, bf, head, sub, lamp_c):
                w.configure(bg=root.cget("bg"))
            s = state["status"] or {}
            if s.get("sound", True) and time.time() - state["last_sound"] > 5:
                state["last_sound"] = time.time()
                play(wav)
        elif root.cget("bg") != BG:
            root.configure(bg=BG)
            for w in (top, tt, bf, head, sub, lamp_c):
                w.configure(bg=BG)
        root.after(500, flash)

    def poll():
        refresh()
        root.after(2000, poll)

    demo_state = demo_status() if DEMO else None
    poll()
    flash()
    if not DEMO and (load_json(STATUS, {}) or {}).get("level") in ("ok", "info"):
        root.after(1500, root.iconify)              # stay out of the way when all is well
    root.mainloop()


# ================================================================== entry
if __name__ == "__main__":
    if "--version" in sys.argv:
        print(f"{APP} {VERSION}")
    elif "--daemon" in sys.argv:
        if os.geteuid() != 0:
            sys.exit("raidwatch --daemon needs root (it runs as the raidwatch service)")
        Daemon().loop()
    elif "--once" in sys.argv:
        d = Daemon()
        s = d.tick()
        print(f"{s['level'].upper()}  ({len(s['arrays'])} arrays)")
        for a in s["alerts"]:
            print(f"  [{a['level']}] {a['text']}")
        for i in s["infos"]:
            print(f"  [info] {i}")
        d.led_alarm(False)
    else:
        window()
