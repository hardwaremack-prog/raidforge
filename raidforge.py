#!/usr/bin/env python3
"""
RAID Forge — point-and-click software RAID for Raspberry Pi 5 / Pi 500 / Pi 400.

Boot the Pi from its SD card, plug drives into a HAT (SATA, NVMe or USB),
and build a Linux mdadm RAID 0, 1, 5, 6 or 10 array with a filesystem,
mount point and auto-mount at boot. Also manages, scrubs, repairs and
health-checks arrays.

Run:  python3 raidforge.py          (real mode — uses sudo/pkexec for disk work)
      python3 raidforge.py --demo   (safe demo with fake drives, touches nothing)
"""
import getpass
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

APP = "RAID Forge"
VERSION = "1.6"
DEMO = "--demo" in sys.argv or os.environ.get("RAIDFORGE_DEMO") == "1"
USER = os.environ.get("SUDO_USER") or getpass.getuser()

CONFIG_TXT = next((p for p in ("/boot/firmware/config.txt", "/boot/config.txt")
                   if os.path.exists(p)), "/boot/firmware/config.txt")
MDADM_CONF = next((p for p in ("/etc/mdadm/mdadm.conf", "/etc/mdadm.conf")
                   if os.path.exists(p)), "/etc/mdadm/mdadm.conf")
BOOT_DIR = os.path.dirname(CONFIG_TXT)                 # the SD card's boot partition
CMDLINE = os.path.join(BOOT_DIR, "cmdline.txt")
CMDLINE_SD = CMDLINE + ".raidforge-sd"                 # original, kept for "boot from SD again"
NEWROOT = "/mnt/raidforge-newroot"
RSYNC_EXCLUDES = ["/dev/*", "/proc/*", "/sys/*", "/run/*", "/tmp/*", "/mnt/*", "/media/*",
                  "/lost+found", "/var/swap", "/swapfile", "/var/cache/apt/archives/*.deb",
                  NEWROOT]
# RAID levels + the ways drives connect (USB, NVMe, SATA HAT). Built-in ones are skipped harmlessly.
INITRAMFS_MODULES = ["md_mod", "raid0", "raid1", "raid10", "raid456",
                     "usb_storage", "uas", "sd_mod", "nvme", "ahci"]


# ---------------------------------------------------------------- migration file edits (pure, testable)
def edit_fstab(text, fs_uuid, fstype, old_uuids=()):
    """New root's fstab: '/' comes from the array; drop the array's old data-mount line(s)."""
    drop = {f"UUID={u}" for u in (fs_uuid, *old_uuids) if u}
    out, had_root = [], False
    for line in text.splitlines():
        f = line.split()
        if f and not f[0].startswith("#") and len(f) >= 2:
            if f[1] == "/":
                out.append(f"UUID={fs_uuid}  /  {fstype}  defaults,noatime  0  1  # RAID Forge: system on RAID")
                had_root = True
                continue
            if f[0] in drop:
                continue                       # was mounted as a data folder; now it's the system
        out.append(line)
    if not had_root:
        out.insert(0, f"UUID={fs_uuid}  /  {fstype}  defaults,noatime  0  1  # RAID Forge: system on RAID")
    return "\n".join(out) + "\n"


def edit_cmdline(text, fs_uuid, fstype):
    """cmdline.txt is one line: point root= at the array and make sure the kernel waits for it."""
    parts = [p for p in text.split() if not p.startswith(("root=", "rootfstype=", "rootwait"))]
    return " ".join([f"root=UUID={fs_uuid}", f"rootfstype={fstype}", "rootwait"] + parts) + "\n"


def wear_from_smart(j):
    """Percent of rated life used, from smartctl -j output (NVMe or SATA SSD). None if unknown."""
    nv = j.get("nvme_smart_health_information_log", {})
    if nv.get("percentage_used") is not None:
        return int(nv["percentage_used"])
    ds = j.get("ata_device_statistics", {})
    for page in ds.get("pages", []) or []:
        for t in page.get("table", []) or []:
            if "Endurance" in (t.get("name") or "") and t.get("value") is not None:
                return int(t["value"])
    # vendor attributes: normalized value counts DOWN from 100 as the flash wears
    for attr in j.get("ata_smart_attributes", {}).get("table", []):
        if attr.get("id") in (177, 231, 233, 202) and attr.get("value") is not None and 0 < attr["value"] <= 100:
            return 100 - int(attr["value"])
    return None


def disk_of(dev):
    """md member name -> whole disk (nvme0n1p1 -> nvme0n1, sda1 -> sda)."""
    return re.sub(r"p\d+$", "", dev) if re.match(r"(nvme|mmcblk)", dev) else re.sub(r"\d+$", "", dev)


def lockstep_warnings(arrays, wear):
    """Mirrored SSDs that have worn to within 5% of each other (and past 30%) may wear out together."""
    out = []
    for a in arrays:
        if a["level"] not in ("raid1", "raid10"):
            continue
        vals = sorted((wear[disk_of(d)], disk_of(d)) for d, s in a["devices"]
                      if s == "active" and wear.get(disk_of(d)) is not None)
        for (w1, d1), (w2, d2) in zip(vals, vals[1:]):
            if w2 >= 30 and w2 - w1 <= 5:
                out.append(f"{a['name']}: {d1} ({w1}%) and {d2} ({w2}%) are wearing in step. Consider replacing "
                           f"one early (My Arrays > Replace a drive) so they don't wear out together.")
    return out


# Runs as admin for one "on" burst: keeps reading 1 MB at random spots (bypassing caches) until the time is up,
# so the drive's activity light stays lit for the whole burst. Read-only.
BLINK_READER = r'''
import os, sys, time, random, mmap
dev, secs, size = sys.argv[1], float(sys.argv[2]), int(sys.argv[3] or 0)
B = 1 << 20
try:
    fd = os.open(dev, os.O_RDONLY | getattr(os, "O_DIRECT", 0))
except OSError:
    fd = os.open(dev, os.O_RDONLY)
if size <= 0:
    size = os.lseek(fd, 0, os.SEEK_END)
blocks = max(size // B - 1, 1)
buf = mmap.mmap(-1, B)
end = time.monotonic() + secs
while time.monotonic() < end:
    try:
        os.preadv(fd, [buf], random.randrange(blocks) * B)
    except OSError:
        time.sleep(0.01)
os.close(fd)
'''


def describe_location(syspath, read_text):
    """Turn /sys/block/<disk>'s real path into words: 'USB 3 (blue port) · bus 4, port 1 · hub port 2'."""
    parts = []
    usb = [s for s in syspath.split("/") if re.fullmatch(r"\d+-[\d.]+", s)]
    if usb:
        seg = usb[-1]
        bus, port = seg.split("-", 1)
        speed = read_text(f"/sys/bus/usb/devices/{seg}/speed", "").strip()
        kind = ("USB 3 (blue port)" if speed.isdigit() and int(speed) >= 5000 else
                "USB 2 (black port)" if speed in ("480", "12", "1.5") else "USB")
        top, *hub = port.split(".")
        parts += [kind, f"bus {bus}, port {top}"]
        if hub:
            parts.append(f"hub port {hub[-1]}")
        lun = re.search(r"/\d+:\d+:\d+:(\d+)/block/", syspath)
        if lun and int(lun.group(1)) > 0:
            parts.append(f"dock slot {int(lun.group(1)) + 1}")
        return " · ".join(parts)
    m = re.search(r"/ata(\d+)/", syspath)
    if m:
        return f"SATA port {m.group(1)}"
    m = re.search(r"/([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.\d)/nvme/", syspath)
    if m:
        return f"NVMe, PCIe address {m.group(1)[5:]}"
    if "mmc" in syspath:
        return "SD card slot"
    return ""


TRIM_OVERRIDE = "/etc/systemd/system/fstrim.timer.d/raidforge.conf"
WATCH_UNIT = "/etc/systemd/system/raidwatch.service"
WATCH_AUTOSTART = "/etc/xdg/autostart/raidwatch.desktop"
WATCH_MENU = "/usr/share/applications/raidwatch.desktop"
WATCH_CONF = "/etc/raidwatch.json"
WATCH_STATUS = "/run/raidwatch/status.json"
HERE = os.path.dirname(os.path.abspath(__file__))
TRIM_DAILY = ("# Added by RAID Forge: TRIM every day instead of every week\n"
              "[Timer]\nOnCalendar=\nOnCalendar=daily\nAccuracySec=1h\nRandomizedDelaySec=30min\nPersistent=true\n")
ZRAM_CONF = "# Added by RAID Forge (low-wear): swap lives in compressed RAM, not on the drives\nALGO=zstd\nPERCENT=50\nPRIORITY=100\n"


def ensure_initramfs(config_text):
    """The kernel needs an initramfs (with mdadm) to find a RAID root. Pi OS turns it on with auto_initramfs=1."""
    if re.search(r"^\s*(auto_initramfs=1|initramfs\s)", config_text, re.M):
        return config_text
    return config_text.rstrip("\n") + "\n[all]\n# Added by RAID Forge (system on RAID)\nauto_initramfs=1\n"

# ---------------------------------------------------------------- colours
BG, PANEL, CARD, LINE = "#0f1419", "#161d26", "#1d2632", "#2a3646"
FG, MUTED, ACCENT = "#e6edf3", "#8b98a9", "#c51a4a"      # Pi raspberry red
GOOD, WARN, BAD, INFO = "#3fb950", "#d29922", "#f85149", "#58a6ff"

# ---------------------------------------------------------------- RAID levels
LEVELS = {
    "0": dict(title="RAID 0", sub="Stripe — speed", min=2, spares=False,
              data=lambda n: n, survive=lambda n: "0 drives (any failure loses everything)",
              desc="Splits data across every drive. Fastest and uses all the space, "
                   "but one dead drive destroys the whole array. Scratch space only."),
    "1": dict(title="RAID 1", sub="Mirror — safety", min=2, spares=True,
              data=lambda n: 1, survive=lambda n: f"{n-1} drive{'s' if n-1 != 1 else ''}",
              desc="Every drive holds a full copy. Capacity of one drive, survives "
                   "all but one drive failing. Simple and very safe."),
    "5": dict(title="RAID 5", sub="Stripe + parity", min=3, spares=True,
              data=lambda n: n - 1, survive=lambda n: "1 drive",
              desc="Stripes data with one drive's worth of parity. Good balance of "
                   "space, speed and safety. Survives one failure."),
    "6": dict(title="RAID 6", sub="Double parity", min=4, spares=True,
              data=lambda n: n - 2, survive=lambda n: "2 drives",
              desc="Like RAID 5 with two parity blocks. Survives any two drives "
                   "failing — best for big drives with long rebuilds."),
    "10": dict(title="RAID 10", sub="Mirrored stripes", min=4, spares=True,
               data=lambda n: n // 2, survive=lambda n: "1 per mirror pair (up to %d)" % (n // 2),
               desc="Stripes across mirrored pairs. Fast writes and fast rebuilds, "
                    "half the raw space. Use an even number of drives."),
}

HATS = [
    ("Auto-detect", "Shows whatever drives the Pi can see."),
    ("Radxa Penta SATA HAT", "Up to 5 SATA drives over PCIe. Needs PCIe enabled and its own 12 V power."),
    ("Dual NVMe HAT (Pimoroni Duo, Geekworm X1004 …)", "2 NVMe drives behind a PCIe switch. Use PCIe Gen 2."),
    ("SunFounder Pironman 5-MAX (2 × NVMe)", "Both slots share one Gen 2 switch (~450 MB/s total). Use PCIe Gen 2 and "
                                             "RAID 1; Gen 3 and RAID 0 won't make it faster. Needs the 27 W supply."),
    ("Quad NVMe HAT (PCIe switch)", "Up to 4 NVMe drives behind a PCIe switch. Use PCIe Gen 2."),
    ("USB 3 drives / USB HAT / dock", "Works on Pi 5, Pi 500 and Pi 400 (blue USB 3 ports). Use a powered hub or dock for 2.5\"/3.5\" drives."),
]


def free_text(space):
    """'1.6 TB free of 3.9 TB (60%) ▕██████░░░░▏' style text for the arrays table."""
    if not space or not space[1]:
        return "— (not mounted)"
    free, total = space
    pct = 100 * free / total
    used = round((100 - pct) / 10)
    return f"{human(free)} free ({pct:.0f}%)  " + "█" * used + "░" * (10 - used)


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1000 or unit == "PB":
            return f"{n:.1f} {unit}" if unit not in ("B", "KB") else f"{n:.0f} {unit}"
        n /= 1000


# ---------------------------------------------------------------- demo data
DEMO_DISKS = [
    dict(name="mmcblk0", path="/dev/mmcblk0", size=64_000_000_000, model="SD card", serial="SD01",
         tran="mmc", rota=False, busy="boot disk"),
    dict(name="sda", path="/dev/sda", size=2_000_398_934_016, model="WDC WD20EFZX", serial="WD-A1", tran="sata", rota=True),
    dict(name="sdb", path="/dev/sdb", size=2_000_398_934_016, model="WDC WD20EFZX", serial="WD-A2", tran="sata", rota=True),
    dict(name="sdc", path="/dev/sdc", size=2_000_398_934_016, model="WDC WD20EFZX", serial="WD-A3", tran="sata", rota=True),
    dict(name="sdd", path="/dev/sdd", size=2_000_398_934_016, model="WDC WD20EFZX", serial="WD-A4", tran="sata", rota=True),
    dict(name="sde", path="/dev/sde", size=2_000_398_934_016, model="ST2000VN004", serial="ST-B1", tran="sata", rota=True),
]
DEMO_MDSTAT = """Personalities : [raid6] [raid5] [raid4]
md127 : active raid5 sdc[2] sdb[1] sda[0]
      3906764800 blocks super 1.2 level 5, 512k chunk, algorithm 2 [3/3] [UUU]
      [=====>...............]  resync = 27.4% (535302144/1953382400) finish=118.6min speed=199231K/sec
      bitmap: 12/15 pages [48KB], 65536KB chunk

unused devices: <none>
"""


# ================================================================= system layer
class System:
    """Everything that touches the OS lives here."""

    def __init__(self, log):
        self.log = log
        self._priv = None

    # ---- privilege
    def priv_prefix(self):
        if self._priv is not None:
            return self._priv
        if os.geteuid() == 0:
            self._priv = []
        elif shutil.which("sudo") and subprocess.run(["sudo", "-n", "true"],
                                                     capture_output=True).returncode == 0:
            self._priv = ["sudo", "-n"]
        elif shutil.which("pkexec"):
            self._priv = ["pkexec"]
        else:
            self._priv = ["sudo"]
        return self._priv

    def run(self, args, root=False, input=None, quiet=False, timeout=None):
        """Run a command. Returns (rc, combined output)."""
        shown = " ".join(args)
        if DEMO and root:
            if not quiet:
                self.log(f"$ {shown}", "cmd")
                self.log("  (demo — not actually run)", "muted")
            return 0, ""
        cmd = (self.priv_prefix() + args) if root else args
        if not quiet:
            self.log(f"$ {shown}", "cmd")
        try:
            p = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            if not quiet:
                self.log(f"  command not found: {args[0]}", "bad")
            return 127, ""
        except subprocess.TimeoutExpired:
            if not quiet:
                self.log("  timed out", "bad")
            return 124, ""
        out = (p.stdout or "") + (p.stderr or "")
        if not quiet and out.strip():
            for line in out.strip().splitlines()[-25:]:
                self.log("  " + line, "bad" if p.returncode else "muted")
        return p.returncode, out

    def write_root_file(self, path, text, append=False):
        args = ["tee"] + (["-a"] if append else []) + [path]
        if DEMO:
            self.log(f"$ {'append to' if append else 'write'} {path}:", "cmd")
            for l in text.strip().splitlines():
                self.log("  " + l, "muted")
            return 0
        rc, _ = self.run(args, root=True, input=text, quiet=True)
        self.log(f"{'Appended to' if append else 'Wrote'} {path}" if rc == 0 else f"Could not write {path}",
                 "good" if rc == 0 else "bad")
        return rc

    # ---- facts
    def board(self):
        if DEMO:
            return "Raspberry Pi 5 Model B Rev 1.0 (demo)"
        try:
            with open("/proc/device-tree/model") as f:
                return f.read().strip("\x00\n ")
        except OSError:
            return "Unknown (not a Raspberry Pi?)"

    def root_source(self):
        if DEMO:
            return "/dev/mmcblk0p2"
        rc, out = self.run(["findmnt", "-no", "SOURCE", "/"], quiet=True)
        return out.strip()

    def boot_disk(self):
        src = self.root_source()
        if not src.startswith("/dev/"):
            return None
        rc, out = self.run(["lsblk", "-no", "PKNAME", src], quiet=True)
        pk = out.strip().splitlines()[0] if out.strip() else ""
        return pk or os.path.basename(src)

    def has(self, tool):
        return DEMO or shutil.which(tool) is not None or os.path.exists(f"/usr/sbin/{tool}") \
            or os.path.exists(f"/sbin/{tool}")

    def run_stream(self, args, root=False, on_text=None):
        """Run a long command, feeding each output line/progress update to on_text. Returns rc."""
        self.log("$ " + " ".join(args), "cmd")
        cmd = (self.priv_prefix() + args) if root else args
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except FileNotFoundError:
            self.log(f"  command not found: {args[0]}", "bad")
            return 127
        buf = b""
        while True:
            ch = p.stdout.read(256)
            if not ch:
                break
            buf += ch
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                if part.strip() and on_text:
                    on_text(part.decode("utf-8", "replace"))
        if buf.strip() and on_text:
            on_text(buf.decode("utf-8", "replace"))
        return p.wait()

    def used_bytes(self, path="/"):
        st = os.statvfs(path)
        return (st.f_blocks - st.f_bfree) * st.f_frsize

    def read_text(self, path, default=""):
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return default

    def pcie_config(self):
        """Returns (enabled, gen) from config.txt."""
        if DEMO:
            return True, "3"
        try:
            text = open(CONFIG_TXT).read()
        except OSError:
            return False, None
        enabled = bool(re.search(r"^\s*dtparam=(pciex1|nvme)\b", text, re.M))
        m = re.search(r"^\s*dtparam=pciex1_gen=(\d)", text, re.M)
        return enabled, (m.group(1) if m else None)

    def pcie_devices(self):
        if DEMO:
            return ["01:00.0 SATA controller: JMicron JMB58x AHCI SATA controller"]
        if not shutil.which("lspci"):
            return []
        rc, out = self.run(["lspci"], quiet=True)
        return [l for l in out.splitlines() if "bridge" not in l.lower()]

    def disks(self):
        if DEMO:
            return [dict(d) for d in DEMO_DISKS]
        rc, out = self.run(["lsblk", "-J", "-b", "-o",
                            "NAME,PATH,SIZE,MODEL,SERIAL,TRAN,TYPE,ROTA,FSTYPE,MOUNTPOINT"], quiet=True)
        try:
            tree = json.loads(out)["blockdevices"]
        except (ValueError, KeyError):
            return []
        boot = self.boot_disk()
        res = []
        for d in tree:
            if d.get("type") != "disk" or re.match(r"(loop|zram|ram|md)", d["name"]):
                continue
            busy = []
            if d["name"] == boot:
                busy.append("boot disk")

            def walk(node):
                if node.get("mountpoint"):
                    busy.append(f"mounted {node['mountpoint']}")
                if node.get("fstype") == "linux_raid_member":
                    busy.append("RAID member")
                if node.get("type", "").startswith("raid"):
                    busy.append(f"in {node['name']}")
                for c in node.get("children", []) or []:
                    walk(c)
            walk(d)
            res.append(dict(name=d["name"], path=d.get("path") or f"/dev/{d['name']}",
                            size=int(d.get("size") or 0), model=(d.get("model") or "").strip(),
                            serial=(d.get("serial") or "").strip(), tran=d.get("tran") or "",
                            rota=bool(d.get("rota")), busy=", ".join(dict.fromkeys(busy))))
        return res

    def mdstat(self):
        if DEMO:
            return DEMO_MDSTAT
        try:
            return open("/proc/mdstat").read()
        except OSError:
            return ""

    @staticmethod
    def parse_mdstat(text):
        arrays, cur = [], None
        for line in text.splitlines():
            m = re.match(r"^(md\w+)\s*:\s*(\w+)\s*(raid\d+|linear)?\s*(.*)$", line)
            if m:
                devs = []
                for tok in m.group(4).split():
                    dm = re.match(r"(\w+)\[\d+\](\((\w)\))?", tok)
                    if dm:
                        devs.append((dm.group(1), {"F": "failed", "S": "spare"}.get(dm.group(3), "active")))
                cur = dict(name=m.group(1), state=m.group(2), level=m.group(3) or "?",
                           devices=devs, size=0, health="", op="", pct=None, finish="", speed="")
                arrays.append(cur)
                continue
            if not cur:
                continue
            b = re.search(r"(\d+) blocks", line)
            if b and not cur["size"]:
                cur["size"] = int(b.group(1)) * 1024
            h = re.search(r"\[(\d+)/(\d+)\]\s*\[([U_]+)\]", line)
            if h:
                cur["health"] = h.group(3)
                cur["want"], cur["have"] = int(h.group(1)), int(h.group(2))
            p = re.search(r"(resync|recovery|reshape|check|repair)\s*=\s*([\d.]+)%", line)
            if p:
                cur["op"], cur["pct"] = p.group(1), float(p.group(2))
                f = re.search(r"finish=([\d.]+min)", line)
                s = re.search(r"speed=(\d+)K/sec", line)
                cur["finish"] = f.group(1) if f else ""
                cur["speed"] = human(int(s.group(1)) * 1024) + "/s" if s else ""
            if "resync=DELAYED" in line or "resync=PENDING" in line:
                cur["op"] = "resync pending"
        return arrays

    def md_alias(self, mdname):
        if DEMO:
            return "/dev/md/vault"
        try:
            for n in os.listdir("/dev/md"):
                if os.path.basename(os.path.realpath(f"/dev/md/{n}")) == mdname:
                    return f"/dev/md/{n}"
        except OSError:
            pass
        return f"/dev/{mdname}"

    def space(self, mount):
        """(free_bytes, total_bytes) of a mounted filesystem, or None."""
        if DEMO and mount:
            return (1_558_000_000_000, 3_940_000_000_000)
        if not mount:
            return None
        try:
            st = os.statvfs(mount)
        except OSError:
            return None
        return (st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize)

    def mountpoint_of(self, dev):
        if DEMO:
            return "/mnt/vault"
        rc, out = self.run(["findmnt", "-no", "TARGET", "-S", dev], quiet=True)
        return out.strip().splitlines()[0] if out.strip() else ""

    # ---- finding a physical drive
    def location(self, name):
        """Where a disk is plugged in, in plain words (USB port + speed, SATA port, PCIe slot)."""
        if DEMO:
            return {"sda": "USB 3 (blue port) · bus 2, port 1 · dock slot 1",
                    "sdb": "USB 3 (blue port) · bus 2, port 1 · dock slot 2",
                    "sdc": "USB 3 (blue port) · bus 4, port 1 · dock slot 1",
                    "sdd": "USB 3 (blue port) · bus 4, port 1 · dock slot 2",
                    "sde": "USB 2 (black port) · bus 1, port 2"}.get(name, "")
        return describe_location(os.path.realpath(f"/sys/block/{name}"), self.read_text)

    def can_blink(self):
        return DEMO or os.geteuid() == 0 or self.priv_prefix()[:1] == ["sudo"]

    def blink(self, disks, stop, on_s=1.0, off_s=1.0, on_phase=None):
        """Read from each disk in an on/off rhythm so its activity light blinks. Read-only, safe on live arrays.
        disks: [{"path":..., "size":...}]. stop: threading.Event. on_phase(True/False) is called each change."""
        while not stop.is_set():
            if on_phase:
                on_phase(True)
            procs = []
            if not DEMO:
                for d in disks:
                    cmd = self.priv_prefix() + [sys.executable, "-c", BLINK_READER, d["path"], str(on_s),
                                                str(int(d.get("size") or 0))]
                    try:
                        procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                    except OSError:
                        pass
            stop.wait(on_s)
            for p in procs:
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
            if on_phase:
                on_phase(False)
            stop.wait(off_s)


# ================================================================= GUI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP}{'  —  DEMO MODE' if DEMO else ''}")
        self.geometry("1200x860")
        self.minsize(1000, 760)
        self.configure(bg=BG)
        self.q = queue.Queue()
        self.sys = System(self.log)
        self.busy = False
        self.disk_vars = {}
        self.spare_vars = {}
        self.disk_info = {}
        self.arrays = []

        self._style()
        self._header()
        self._log_panel()
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self.tab_build = ttk.Frame(nb, style="Bg.TFrame")
        self.tab_arrays = ttk.Frame(nb, style="Bg.TFrame")
        self.tab_health = ttk.Frame(nb, style="Bg.TFrame")
        nb.add(self.tab_build, text="  ① Set up RAID  ")
        nb.add(self.tab_arrays, text="  ② My Arrays  ")
        nb.add(self.tab_health, text="  ③ Drive Health  ")
        self._build_tab()
        self._arrays_tab()
        self._health_tab()

        self.after(100, self._pump)
        self.after(200, self.refresh_all)
        self.after(3000, self._auto_refresh)
        if DEMO:
            self.log("Demo mode: fake drives, nothing on this computer is changed.", "warn")

    # ------------------------------------------------------------ styling
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        base = ("DejaVu Sans", 10)
        self.option_add("*Font", base)
        s.configure(".", background=BG, foreground=FG, fieldbackground=CARD, bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE, troughcolor=PANEL, font=base)
        s.configure("Bg.TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("Card.TFrame", background=CARD)
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 6, 0, 0))
        s.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(14, 8), borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", FG)])
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Panel.TLabel", background=PANEL)
        s.configure("Card.TLabel", background=CARD)
        s.configure("Muted.Panel.TLabel", background=PANEL, foreground=MUTED)
        s.configure("Muted.Card.TLabel", background=CARD, foreground=MUTED)
        s.configure("H.Panel.TLabel", background=PANEL, font=("DejaVu Sans", 11, "bold"))
        s.configure("Big.Card.TLabel", background=CARD, font=("DejaVu Sans", 18, "bold"))
        s.configure("TButton", background=CARD, foreground=FG, padding=(12, 6), borderwidth=1)
        s.map("TButton", background=[("active", LINE), ("disabled", PANEL)],
              foreground=[("disabled", MUTED)])
        s.configure("Accent.TButton", background=ACCENT, foreground="white",
                    font=("DejaVu Sans", 11, "bold"), padding=(18, 9))
        s.map("Accent.TButton", background=[("active", "#e0245e"), ("disabled", LINE)])
        s.configure("Danger.TButton", foreground=BAD)
        s.configure("TCheckbutton", background=PANEL, foreground=FG)
        s.map("TCheckbutton", background=[("active", PANEL)])
        s.configure("Card.TCheckbutton", background=CARD)
        s.map("Card.TCheckbutton", background=[("active", CARD)])
        s.configure("TRadiobutton", background=PANEL, foreground=FG)
        s.map("TRadiobutton", background=[("active", PANEL)])
        s.configure("TEntry", fieldbackground=CARD, foreground=FG, insertcolor=FG)
        s.configure("TCombobox", fieldbackground=CARD, foreground=FG, background=CARD, arrowcolor=FG)
        s.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", CARD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG, rowheight=28,
                    borderwidth=0)
        s.configure("Treeview.Heading", background=PANEL, foreground=MUTED, borderwidth=0)
        s.map("Treeview", background=[("selected", "#2d3f57")])
        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL, thickness=10)

    def _header(self):
        h = tk.Frame(self, bg=BG)
        h.pack(fill="x", padx=16, pady=(12, 8))
        c = tk.Canvas(h, width=40, height=40, bg=BG, highlightthickness=0)
        c.pack(side="left")
        for i, col in enumerate((ACCENT, "#e0245e", "#9b1239", ACCENT)):
            c.create_rectangle(4 + (i % 2) * 17, 4 + (i // 2) * 17, 19 + (i % 2) * 17, 19 + (i // 2) * 17,
                               fill=col, outline="")
        t = tk.Frame(h, bg=BG)
        t.pack(side="left", padx=10)
        tk.Label(t, text=APP, bg=BG, fg=FG, font=("DejaVu Sans", 17, "bold")).pack(anchor="w")
        tk.Label(t, text="Software RAID for Raspberry Pi 5 / 500 / 400 — boot from SD, store on the HAT or USB",
                 bg=BG, fg=MUTED).pack(anchor="w")
        if DEMO:
            tk.Label(h, text=" DEMO ", bg=WARN, fg="black", font=("DejaVu Sans", 10, "bold")).pack(side="right")

    # ------------------------------------------------------------ log
    def _log_panel(self):
        f = tk.Frame(self, bg=PANEL)
        f.pack(side="bottom", fill="x", padx=14, pady=(0, 12))
        top = tk.Frame(f, bg=PANEL)
        top.pack(fill="x", padx=10, pady=(6, 0))
        tk.Label(top, text="Activity", bg=PANEL, fg=MUTED, font=("DejaVu Sans", 9, "bold")).pack(side="left")
        self.status = tk.Label(top, text="Ready", bg=PANEL, fg=MUTED)
        self.status.pack(side="right")
        self.logbox = tk.Text(f, height=5, bg=PANEL, fg=FG, bd=0, highlightthickness=0,
                              font=("DejaVu Sans Mono", 9), wrap="word", padx=10, pady=6)
        self.logbox.pack(fill="x")
        for tag, col in (("cmd", INFO), ("muted", MUTED), ("good", GOOD), ("warn", WARN),
                         ("bad", BAD), ("info", FG)):
            self.logbox.tag_config(tag, foreground=col)
        self.logbox.configure(state="disabled")

    def log(self, msg, tag="info"):
        self.q.put(("log", msg, tag))

    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", item[1] + "\n", item[2])
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif item[0] == "call":
                    item[1]()
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def ui(self, fn):
        self.q.put(("call", fn))

    def work(self, label, fn):
        """Run fn in a background thread; one job at a time."""
        if self.busy:
            messagebox.showinfo(APP, "Another job is still running — wait for it to finish.")
            return
        self.busy = True
        self.status.configure(text=f"Working: {label}…", fg=WARN)
        self.build_btn.state(["disabled"])

        def runner():
            try:
                fn()
            except Exception as e:  # keep the GUI alive
                self.log(f"Error: {e}", "bad")
            finally:
                def done():
                    self.busy = False
                    self.status.configure(text="Ready", fg=MUTED)
                    self.refresh_all()
                self.ui(done)
        threading.Thread(target=runner, daemon=True).start()

    # ============================================================ TAB 1: build
    def _build_tab(self):
        root = self.tab_build
        root.columnconfigure(0, weight=5)
        root.columnconfigure(1, weight=6)
        root.rowconfigure(1, weight=1)

        # --- system check strip
        chk = ttk.Frame(root, style="Panel.TFrame", padding=10)
        chk.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(8, 6))
        self.chk_labels = {}
        for i, key in enumerate(("board", "boot", "tools", "pcie")):
            col = ttk.Frame(chk, style="Panel.TFrame")
            col.grid(row=0, column=i, sticky="nw", padx=(0, 22))
            chk.columnconfigure(i, weight=1)
            ttl = {"board": "Board", "boot": "Booted from", "tools": "RAID tools", "pcie": "PCIe / HAT"}[key]
            ttk.Label(col, text=ttl.upper(), style="Muted.Panel.TLabel",
                      font=("DejaVu Sans", 8, "bold")).pack(anchor="w")
            lab = tk.Label(col, text="…", bg=PANEL, fg=FG, justify="left", anchor="w", wraplength=240)
            lab.pack(anchor="w")
            self.chk_labels[key] = lab
        btns = ttk.Frame(chk, style="Panel.TFrame")
        btns.grid(row=0, column=4, sticky="e")
        self.btn_tools = ttk.Button(btns, text="Install RAID tools", command=self.install_tools)
        self.btn_tools.pack(fill="x", pady=2)
        self.btn_pcie = ttk.Button(btns, text="Enable PCIe for HAT…", command=self.enable_pcie)
        self.btn_pcie.pack(fill="x", pady=2)

        # --- left: drives
        left = ttk.Frame(root, style="Panel.TFrame", padding=12)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        top = ttk.Frame(left, style="Panel.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="1. Pick the drives", style="H.Panel.TLabel").pack(side="left")
        ttk.Button(top, text="↻ Rescan", command=self.refresh_all).pack(side="right")
        hf = ttk.Frame(left, style="Panel.TFrame")
        hf.pack(fill="x", pady=(8, 4))
        ttk.Label(hf, text="HAT:", style="Panel.TLabel").pack(side="left")
        self.hat = tk.StringVar(value=HATS[0][0])
        cb = ttk.Combobox(hf, textvariable=self.hat, values=[h[0] for h in HATS], state="readonly", width=40)
        cb.pack(side="left", padx=6, fill="x", expand=True)
        cb.bind("<<ComboboxSelected>>", lambda e: self._hat_note())
        self.hat_note = ttk.Label(left, text=HATS[0][1], style="Muted.Panel.TLabel", wraplength=420)
        self.hat_note.pack(anchor="w", pady=(0, 6))

        holder = tk.Frame(left, bg=PANEL)
        holder.pack(fill="both", expand=True)
        cv = tk.Canvas(holder, bg=PANEL, highlightthickness=0, height=200)
        sb = ttk.Scrollbar(holder, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        self.disk_frame = ttk.Frame(cv, style="Panel.TFrame")
        win = cv.create_window((0, 0), window=self.disk_frame, anchor="nw")
        self.disk_frame.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfigure(win, width=e.width))
        for seq, d in (("<Button-4>", -1), ("<Button-5>", 1)):
            cv.bind_all(seq, lambda e, d=d: cv.yview_scroll(d, "units")
                        if str(e.widget).startswith(str(cv)) else None)

        # --- right: level + options
        right = ttk.Frame(root, style="Panel.TFrame", padding=12)
        right.grid(row=1, column=1, sticky="nsew")
        bf = ttk.Frame(right, style="Panel.TFrame")       # packed first so it's never clipped
        bf.pack(fill="x", side="bottom", pady=(8, 0))
        self.build_btn = ttk.Button(bf, text="Build RAID array", style="Accent.TButton", command=self.build)
        self.build_btn.pack(side="right")
        self.build_hint = ttk.Label(bf, text="", style="Muted.Panel.TLabel")
        self.build_hint.pack(side="right", padx=10)
        ttk.Label(right, text="2. Choose a RAID level", style="H.Panel.TLabel").pack(anchor="w")
        lv = ttk.Frame(right, style="Panel.TFrame")
        lv.pack(fill="x", pady=(8, 6))
        self.level = tk.StringVar(value="5")
        self.level_cards = {}
        for i, key in enumerate(LEVELS):
            L = LEVELS[key]
            card = tk.Frame(lv, bg=CARD, highlightthickness=2, highlightbackground=CARD, cursor="hand2")
            card.grid(row=0, column=i, sticky="nsew", padx=3)
            lv.columnconfigure(i, weight=1)
            a = tk.Label(card, text=L["title"], bg=CARD, fg=FG, font=("DejaVu Sans", 12, "bold"))
            b = tk.Label(card, text=L["sub"], bg=CARD, fg=MUTED, font=("DejaVu Sans", 8))
            c = tk.Label(card, text=f"min {L['min']} drives", bg=CARD, fg=MUTED, font=("DejaVu Sans", 8))
            for w in (a, b, c):
                w.pack(padx=6, pady=(4 if w is a else 0))
            for w in (card, a, b, c):
                w.bind("<Button-1>", lambda e, k=key: self._pick_level(k))
            self.level_cards[key] = (card, a, b, c)

        self.level_desc = tk.Label(right, text="", bg=PANEL, fg=MUTED, wraplength=560, justify="left")
        self.level_desc.pack(anchor="w", pady=(2, 8))

        # summary card
        sm = ttk.Frame(right, style="Card.TFrame", padding=12)
        sm.pack(fill="x")
        for i in range(3):
            sm.columnconfigure(i, weight=1)
        self.sum_cap = self._stat(sm, 0, "Usable space")
        self.sum_tol = self._stat(sm, 1, "Can lose")
        self.sum_eff = self._stat(sm, 2, "Efficiency")
        self.sum_bar = tk.Canvas(sm, height=14, bg=CARD, highlightthickness=0)
        self.sum_bar.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.sum_msg = tk.Label(sm, text="", bg=CARD, fg=WARN, wraplength=540, justify="left")
        self.sum_msg.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        ttk.Label(right, text="3. Name it and pick where it mounts", style="H.Panel.TLabel").pack(anchor="w", pady=(10, 4))
        opt = ttk.Frame(right, style="Panel.TFrame")
        opt.pack(fill="x")
        self.name = tk.StringVar(value="vault")
        self.mount = tk.StringVar(value="/mnt/vault")
        self.fs = tk.StringVar(value="ext4")
        self.chunk = tk.StringVar(value="512")
        self.persist = tk.BooleanVar(value=True)
        self.name.trace_add("write", lambda *a: self._name_changed())
        rows = [("Array name", ttk.Entry(opt, textvariable=self.name, width=18)),
                ("Mount at", ttk.Entry(opt, textvariable=self.mount, width=24)),
                ("Filesystem", ttk.Combobox(opt, textvariable=self.fs, values=["ext4", "xfs"],
                                             state="readonly", width=8)),
                ("Chunk (KB)", ttk.Combobox(opt, textvariable=self.chunk,
                                             values=["64", "128", "256", "512", "1024"],
                                             state="readonly", width=8))]
        for i, (lab, w) in enumerate(rows):
            r, c = divmod(i, 2)
            ttk.Label(opt, text=lab, style="Muted.Panel.TLabel").grid(row=r, column=c * 2, sticky="w", padx=(0, 6), pady=3)
            w.grid(row=r, column=c * 2 + 1, sticky="w", padx=(0, 18), pady=3)
        ttk.Checkbutton(opt, text="Mount automatically at every boot (fstab + mdadm.conf)",
                        variable=self.persist).grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self._pick_level("5")

    def _stat(self, parent, col, title):
        ttk.Label(parent, text=title.upper(), style="Muted.Card.TLabel",
                  font=("DejaVu Sans", 8, "bold")).grid(row=0, column=col, sticky="w")
        lab = ttk.Label(parent, text="—", style="Big.Card.TLabel")
        lab.grid(row=1, column=col, sticky="w")
        return lab

    def _hat_note(self):
        self.hat_note.configure(text=dict(HATS)[self.hat.get()])

    def _name_changed(self):
        n = re.sub(r"[^A-Za-z0-9_-]", "", self.name.get())
        if self.mount.get().startswith("/mnt/"):
            self.mount.set(f"/mnt/{n}" if n else "/mnt/")

    def _pick_level(self, key):
        self.level.set(key)
        for k, (card, a, b, c) in self.level_cards.items():
            on = k == key
            card.configure(highlightbackground=ACCENT if on else CARD)
        self.level_desc.configure(text=LEVELS[key]["desc"])
        self.update_summary()

    # --- disks list
    def render_disks(self, disks):
        for w in self.disk_frame.winfo_children():
            w.destroy()
        old_sel = {k for k, v in self.disk_vars.items() if v.get()}
        old_sp = {k for k, v in self.spare_vars.items() if v.get()}
        self.disk_vars, self.spare_vars, self.disk_info = {}, {}, {}
        hdr = ttk.Frame(self.disk_frame, style="Panel.TFrame")
        hdr.pack(fill="x")
        ttk.Label(hdr, text="Use  Spare", style="Muted.Panel.TLabel", font=("DejaVu Sans", 8, "bold")).pack(side="left")
        usable = [d for d in disks if not d.get("busy")]
        if not usable:
            msg = ("No free drives found.\n\nCheck the HAT has power, its ribbon cable is seated, "
                   "and PCIe is enabled (button above), then reboot and Rescan.")
            tk.Label(self.disk_frame, text=msg, bg=PANEL, fg=MUTED, justify="left", wraplength=420).pack(anchor="w", pady=12)
        for d in disks:
            self.disk_info[d["path"]] = d
            row = tk.Frame(self.disk_frame, bg=CARD)
            row.pack(fill="x", pady=3)
            v, sv = tk.BooleanVar(value=d["path"] in old_sel), tk.BooleanVar(value=d["path"] in old_sp)
            busy = bool(d.get("busy"))
            cb = ttk.Checkbutton(row, variable=v, style="Card.TCheckbutton",
                                 command=lambda p=d["path"]: self._toggle(p, "use"))
            sp = ttk.Checkbutton(row, variable=sv, style="Card.TCheckbutton",
                                 command=lambda p=d["path"]: self._toggle(p, "spare"))
            cb.pack(side="left", padx=(8, 0), pady=8)
            sp.pack(side="left", padx=(6, 6))
            if busy:
                cb.state(["disabled"]); sp.state(["disabled"]); v.set(False); sv.set(False)
            kind = {"nvme": "NVMe", "sata": "SATA", "usb": "USB", "mmc": "SD"}.get(d["tran"], d["tran"].upper() or "?")
            if d["tran"] == "sata":
                kind += " HDD" if d["rota"] else " SSD"
            badge = tk.Label(row, text=kind, bg=LINE, fg=FG, font=("DejaVu Sans", 8, "bold"), padx=6)
            badge.pack(side="left", padx=4)
            info = tk.Frame(row, bg=CARD)
            info.pack(side="left", fill="x", expand=True, padx=6, pady=4)
            tk.Label(info, text=f"{d['path']}   {human(d['size'])}", bg=CARD, fg=MUTED if busy else FG,
                     font=("DejaVu Sans", 10, "bold")).pack(anchor="w")
            sub = d["model"] or "unknown model"
            if d.get("serial"):
                sub += f"  ·  {d['serial']}"
            tk.Label(info, text=sub, bg=CARD, fg=MUTED, font=("DejaVu Sans", 8)).pack(anchor="w")
            if d["tran"] != "mmc":
                ttk.Button(row, text="Blink", width=6,
                           command=lambda n=d["name"]: self.identify([n])).pack(side="right", padx=(0, 8))
            if busy:
                tk.Label(row, text=d["busy"], bg=CARD, fg=WARN, font=("DejaVu Sans", 8)).pack(side="right", padx=8)
            self.disk_vars[d["path"]], self.spare_vars[d["path"]] = v, sv
        self.update_summary()

    def _toggle(self, path, which):
        if which == "use" and self.disk_vars[path].get():
            self.spare_vars[path].set(False)
        if which == "spare" and self.spare_vars[path].get():
            self.disk_vars[path].set(False)
        self.update_summary()

    def selected(self):
        m = [p for p, v in self.disk_vars.items() if v.get()]
        s = [p for p, v in self.spare_vars.items() if v.get()]
        return m, s

    def update_summary(self):
        if not hasattr(self, "sum_cap"):
            return
        key = self.level.get()
        L = LEVELS[key]
        members, spares = self.selected()
        n = len(members)
        problems = []
        if n < L["min"]:
            problems.append(f"{L['title']} needs at least {L['min']} drives — {n} selected.")
        if key == "10" and n % 2:
            problems.append("RAID 10 needs an even number of drives.")
        if spares and not L["spares"]:
            problems.append("RAID 0 can't use hot spares — untick the spare.")
        sizes = [self.disk_info[p]["size"] for p in members + spares if p in self.disk_info]
        smallest = min(sizes) if sizes else 0
        if sizes and max(sizes) > smallest * 1.02:
            problems.append(f"Drives differ in size — every drive will only use {human(smallest)}.")
        if n >= 1:
            data = max(L["data"](n), 0) if n >= L["min"] else 0
            cap = data * smallest
            self.sum_cap.configure(text=human(cap) if cap else "—")
            self.sum_tol.configure(text=L["survive"](n) if n >= L["min"] else "—")
            self.sum_eff.configure(text=f"{100 * data / n:.0f}%" if n >= L["min"] else "—")
        else:
            data = 0
            for lab in (self.sum_cap, self.sum_tol, self.sum_eff):
                lab.configure(text="—")
        # drive bar: data vs redundancy blocks
        c = self.sum_bar
        c.delete("all")
        c.update_idletasks()
        w = max(c.winfo_width(), 300)
        tot = n + len(spares)
        if tot:
            seg = w / tot
            for i in range(tot):
                col = GOOD if i < data else (WARN if i < n else INFO)
                c.create_rectangle(i * seg + 1, 0, (i + 1) * seg - 2, 14, fill=col, outline="")
        legend = "green = data   amber = mirror/parity   blue = hot spare" if tot else ""
        hard = [p for p in problems if "differ" not in p]
        self.sum_msg.configure(text="\n".join(problems) or legend, fg=WARN if problems else MUTED)
        ok = not hard and n >= L["min"]
        if ok and not self.busy:
            self.build_btn.state(["!disabled"])
            self.build_hint.configure(text=f"{n} drive{'s' if n != 1 else ''}"
                                           f"{f' + {len(spares)} spare' if spares else ''} will be erased")
        else:
            self.build_btn.state(["disabled"])
            self.build_hint.configure(text="")

    # --- system check
    def render_checks(self, facts):
        b, src, tools, pc = facts["board"], facts["root"], facts["tools"], facts["pcie"]
        self.chk_labels["board"].configure(text=b, fg=FG)
        on_sd = "mmcblk" in src
        if "/md" in src:
            txt, col = f"SD card (boot) ✓\nsystem on RAID {src}", GOOD
        else:
            txt, col = f"{'SD card ✓' if on_sd else 'Not the SD card'}\n{src}", GOOD if on_sd else WARN
        self.chk_labels["boot"].configure(text=txt, fg=col)
        self.chk_labels["tools"].configure(text="mdadm ready ✓" if tools else "mdadm not installed",
                                           fg=GOOD if tools else BAD)
        self.btn_tools.state(["disabled"] if tools else ["!disabled"])
        is500plus = "500+" in b or "500 Plus" in b
        has_pcie = is500plus or bool(re.search(r"Pi 5\b|Compute Module 5", b))
        if not has_pcie:
            short = re.sub(r"^Raspberry\s+|\s+Rev.*$", "", b) or "This board"
            txt, col = f"{short} has no PCIe connector —\nuse USB 3 drives/docks", WARN
            self.btn_pcie.state(["disabled"])
        else:
            en, gen = pc["config"]
            devs = pc["devices"]
            if devs:
                txt = f"{'On' if en else 'Auto'} (Gen {gen or '2'}) ✓\n{devs[0].split(': ', 1)[-1][:46]}"
                col = GOOD
            elif en:
                txt, col = "Enabled, but nothing found —\ncheck HAT power/cable, reboot", WARN
            else:
                txt, col = "Not enabled yet\n(needed for SATA/NVMe HATs)", WARN
            if is500plus:
                txt += "\nPi 500+: internal M.2 slot"
            self.btn_pcie.state(["!disabled"])
        self.chk_labels["pcie"].configure(text=txt, fg=col)

    def install_tools(self):
        def job():
            self.log("Installing mdadm, smartmontools, xfsprogs…", "info")
            self.sys.run(["apt-get", "update"], root=True, timeout=600)
            rc, _ = self.sys.run(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                                  "--no-install-recommends", "mdadm", "smartmontools", "xfsprogs", "pciutils"],
                                 root=True, timeout=1200)
            self.log("Tools installed ✓" if rc == 0 else "Install failed — see above.", "good" if rc == 0 else "bad")
        self.work("installing tools", job)

    def enable_pcie(self):
        win = tk.Toplevel(self, bg=PANEL)
        win.title("Enable PCIe")
        win.transient(self)
        win.grab_set()
        tk.Label(win, text="Turn on the Pi 5 PCIe connector for your HAT", bg=PANEL, fg=FG,
                 font=("DejaVu Sans", 12, "bold")).pack(anchor="w", padx=16, pady=(14, 4))
        tk.Label(win, text=f"Adds dtparam lines to {CONFIG_TXT}. A reboot is needed afterwards.\n"
                           "Gen 2 is the official speed. Gen 3 is faster but not certified —\n"
                           "HATs with a PCIe switch (dual/quad NVMe) are usually Gen 2 only.",
                 bg=PANEL, fg=MUTED, justify="left").pack(anchor="w", padx=16)
        gen = tk.StringVar(value="2")
        for g, t in (("2", "Gen 2 — safe (≈ 450 MB/s)"), ("3", "Gen 3 — faster (≈ 900 MB/s), unofficial")):
            ttk.Radiobutton(win, text=t, value=g, variable=gen).pack(anchor="w", padx=24, pady=2)

        def apply():
            win.destroy()

            def job():
                try:
                    text = open(CONFIG_TXT).read() if not DEMO else "[all]\n"
                except OSError as e:
                    self.log(f"Can't read {CONFIG_TXT}: {e}", "bad")
                    return
                lines = [l for l in text.splitlines()
                         if not re.match(r"^\s*dtparam=(pciex1|pciex1_gen|nvme)\b", l)]
                if not any(l.strip() == "[all]" for l in lines):
                    lines.append("[all]")
                lines += ["# Added by RAID Forge", "dtparam=pciex1", f"dtparam=pciex1_gen={gen.get()}"]
                self.sys.run(["cp", CONFIG_TXT, CONFIG_TXT + ".raidforge.bak"], root=True)
                if self.sys.write_root_file(CONFIG_TXT, "\n".join(lines) + "\n") == 0:
                    self.log(f"PCIe Gen {gen.get()} enabled. Backup saved as config.txt.raidforge.bak.", "good")
                    self.ui(self._offer_reboot)
            self.work("enabling PCIe", job)
        b = tk.Frame(win, bg=PANEL)
        b.pack(fill="x", padx=16, pady=14)
        ttk.Button(b, text="Enable", style="Accent.TButton", command=apply).pack(side="right")
        ttk.Button(b, text="Cancel", command=win.destroy).pack(side="right", padx=8)

    def _offer_reboot(self):
        if messagebox.askyesno(APP, "Reboot now so the Pi can see the HAT's drives?"):
            self.sys.run(["systemctl", "reboot"], root=True)

    # --- build
    def build(self):
        key = self.level.get()
        members, spares = self.selected()
        name = re.sub(r"[^A-Za-z0-9_-]", "", self.name.get())
        mnt = self.mount.get().strip()
        if not name:
            return messagebox.showerror(APP, "Give the array a name (letters, numbers, - or _).")
        if not mnt.startswith("/") or mnt in ("/", "/boot", "/boot/firmware", "/home", "/usr", "/etc"):
            return messagebox.showerror(APP, "Pick a mount folder such as /mnt/vault.")
        if any(a["name"] == name for a in self.arrays) or (not DEMO and os.path.exists(f"/dev/md/{name}")):
            return messagebox.showerror(APP, f"An array called '{name}' already exists.")
        if not self.sys.has("mdadm"):
            return messagebox.showerror(APP, "Install the RAID tools first (button at the top).")

        win = tk.Toplevel(self, bg=PANEL)
        win.title("Erase drives?")
        win.transient(self)
        win.grab_set()
        tk.Label(win, text="⚠  Everything on these drives will be erased", bg=PANEL, fg=BAD,
                 font=("DejaVu Sans", 13, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        for p in members + spares:
            d = self.disk_info[p]
            tk.Label(win, text=f"   {p}   {human(d['size'])}   {d['model']}{'   (spare)' if p in spares else ''}",
                     bg=PANEL, fg=FG, font=("DejaVu Sans Mono", 10)).pack(anchor="w", padx=18)
        tk.Label(win, text=f"\nBuilding {LEVELS[key]['title']} '{name}' → {self.fs.get()} at {mnt}\n"
                           "Type ERASE to continue:", bg=PANEL, fg=MUTED, justify="left").pack(anchor="w", padx=18)
        word = tk.StringVar()
        ent = ttk.Entry(win, textvariable=word, width=14)
        ent.pack(anchor="w", padx=18, pady=4)
        ent.focus_set()
        bf = tk.Frame(win, bg=PANEL)
        bf.pack(fill="x", padx=18, pady=14)
        go = ttk.Button(bf, text="Erase & build", style="Accent.TButton")
        go.pack(side="right")
        go.state(["disabled"])
        ttk.Button(bf, text="Cancel", command=win.destroy).pack(side="right", padx=8)
        word.trace_add("write", lambda *a: go.state(["!disabled"] if word.get().strip() == "ERASE" else ["disabled"]))

        params = dict(level=key, members=members, spares=spares, name=name, mount=mnt,
                      fs=self.fs.get(), chunk=int(self.chunk.get()), persist=self.persist.get())

        def start():
            win.destroy()
            self.work(f"building {LEVELS[key]['title']}", lambda: self._do_build(params))
        go.configure(command=start)

    def _do_build(self, p):
        S = self.sys
        lvl, members, spares, name = p["level"], p["members"], p["spares"], p["name"]
        md = f"/dev/md/{name}"
        n = len(members)
        self.log(f"── Building {LEVELS[lvl]['title']} '{name}' from {n} drive(s) ──", "info")

        self.log("Step 1/6  Wiping old signatures", "info")
        for d in members + spares:
            S.run(["mdadm", "--zero-superblock", "--force", d], root=True, quiet=True)
            S.run(["wipefs", "-a", d], root=True)

        self.log("Step 2/6  Creating the array", "info")
        cmd = ["mdadm", "--create", md, "--metadata=1.2", f"--name={name}", f"--level={lvl}",
               f"--raid-devices={n}"]
        if lvl != "1":
            cmd.append(f"--chunk={p['chunk']}")
        if lvl == "10":
            cmd.append("--layout=n2")
        if lvl != "0":
            cmd.append("--bitmap=internal")
        if spares:
            cmd.append(f"--spare-devices={len(spares)}")
        cmd += ["--run"] + members + spares
        rc, _ = S.run(cmd, root=True, input="y\n")
        if rc != 0:
            self.log("Array creation failed — nothing else was changed.", "bad")
            return
        S.run(["udevadm", "settle"], root=True, quiet=True)

        self.log(f"Step 3/6  Formatting as {p['fs']}", "info")
        data = LEVELS[lvl]["data"](n)
        if p["fs"] == "ext4":
            stride = max(p["chunk"] // 4, 1)
            rc, _ = S.run(["mkfs.ext4", "-F", "-L", name[:16], "-m", "1",
                           "-E", f"stride={stride},stripe_width={stride * max(data, 1)},lazy_itable_init=1,lazy_journal_init=1",
                           md], root=True, timeout=3600)
        else:
            rc, _ = S.run(["mkfs.xfs", "-f", "-L", name[:12], md], root=True, timeout=3600)
        if rc != 0:
            self.log("Formatting failed. The array exists — see 'My Arrays'.", "bad")
            return

        self.log(f"Step 4/6  Mounting at {p['mount']}", "info")
        S.run(["mkdir", "-p", p["mount"]], root=True)
        rc, _ = S.run(["mount", md, p["mount"]], root=True)
        S.run(["chown", f"{USER}:{USER}", p["mount"]], root=True)

        if p["persist"]:
            self.log("Step 5/6  Saving config so it comes back after reboot", "info")
            rc, out = S.run(["mdadm", "--detail", "--brief", md], root=True, quiet=True)
            line = next((l for l in out.splitlines() if l.startswith("ARRAY")), "")
            if DEMO:
                line = f"ARRAY {md} metadata=1.2 name={name} UUID=1a2b3c4d:5e6f7a8b:9c0d1e2f:3a4b5c6d"
            um = re.search(r"UUID=(\S+)", line)
            try:
                conf = open(MDADM_CONF).read()
            except OSError:
                conf = ""
            if um and um.group(1) not in conf:
                S.run(["mkdir", "-p", os.path.dirname(MDADM_CONF)], root=True, quiet=True)
                S.write_root_file(MDADM_CONF, line + "\n", append=True)
            rc, out = S.run(["blkid", "-s", "UUID", "-o", "value", md], root=True, quiet=True)
            fsuuid = out.strip() or ("demo-fs-uuid" if DEMO else "")
            if fsuuid:
                opts = "defaults,noatime,nofail,x-systemd.device-timeout=30s"
                fst = f"UUID={fsuuid}  {p['mount']}  {p['fs']}  {opts}  0  {2 if p['fs'] == 'ext4' else 0}  # RAID Forge {name}\n"
                S.write_root_file("/etc/fstab", fst, append=True)
            if DEMO or shutil.which("update-initramfs"):
                self.log("Updating initramfs (can take a minute)…", "muted")
                S.run(["update-initramfs", "-u"], root=True, timeout=900)
            S.run(["systemctl", "daemon-reload"], root=True, quiet=True)
        else:
            self.log("Step 5/6  Skipped (not saved for boot)", "muted")

        self.log("Step 6/6  Done", "info")
        msg = (f"✓ '{name}' is ready at {p['mount']}."
               + ("" if lvl == "0" else " It's syncing in the background — usable now, "
                                        "but redundancy is complete once 'My Arrays' shows 100%."))
        self.log(msg, "good")
        self.ui(lambda: messagebox.showinfo(APP, msg))

    # ============================================================ TAB 2: arrays
    def _arrays_tab(self):
        root = self.tab_arrays
        top = ttk.Frame(root, style="Bg.TFrame")
        top.pack(fill="x", pady=(10, 6))
        ttk.Button(top, text="↻ Refresh", command=self.refresh_all).pack(side="left")
        ttk.Button(top, text="Find & start existing arrays", command=self.assemble).pack(side="left", padx=6)
        cols = ("dev", "level", "size", "free", "health", "state", "mount", "progress")
        self.atree = ttk.Treeview(root, columns=cols, show="headings", height=6, selectmode="browse")
        for c, t, w in zip(cols, ("Array", "Level", "Size", "Free space", "Drives", "State", "Mounted at", "Activity"),
                           (170, 60, 85, 190, 100, 110, 130, 230)):
            self.atree.heading(c, text=t)
            self.atree.column(c, width=w, anchor="w")
        self.atree.pack(fill="x")
        self.atree.tag_configure("bad", foreground=BAD)
        self.atree.tag_configure("warn", foreground=WARN)
        self.atree.tag_configure("good", foreground=GOOD)
        self.atree.bind("<<TreeviewSelect>>", lambda e: self.show_detail())

        self.aprog = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.aprog.pack(fill="x", pady=(6, 0))
        self.aprog_lbl = ttk.Label(root, text="", foreground=MUTED)
        self.aprog_lbl.pack(anchor="w")

        acts = ttk.Frame(root, style="Bg.TFrame")
        acts.pack(fill="x", pady=8)
        for t, fn in (("Details", self.show_detail), ("Scrub (check data)", self.scrub),
                      ("Speed test", self.speed_test), ("Replace a drive…", self.replace_drive),
                      ("Add hot spare…", self.add_spare), ("Identify drives…", self.identify_member),
                      ("Stop", self.stop_array)):
            ttk.Button(acts, text=t, command=fn).pack(side="left", padx=(0, 6))
        ttk.Button(acts, text="Delete array…", style="Danger.TButton", command=self.delete_array).pack(side="right")

        mig = ttk.Frame(root, style="Panel.TFrame", padding=10)
        mig.pack(fill="x", pady=(0, 8))
        ttk.Label(mig, text="Run the whole system from RAID", style="H.Panel.TLabel").pack(side="left")
        ttk.Label(mig, text="  copies everything off the SD card; the SD card only starts the Pi",
                  style="Muted.Panel.TLabel").pack(side="left")
        ttk.Button(mig, text="Boot from SD again…", command=self.undo_migration).pack(side="right")
        ttk.Button(mig, text="Move system to RAID…", style="Accent.TButton",
                   command=self.migrate_wizard).pack(side="right", padx=6)

        wp = ttk.Frame(root, style="Panel.TFrame", padding=10)
        wp.pack(fill="x", pady=(0, 8))
        ttk.Label(wp, text="RAID Watch", style="H.Panel.TLabel").pack(side="left")
        self.watch_lbl = tk.Label(wp, text="  checking…", bg=PANEL, fg=MUTED)
        self.watch_lbl.pack(side="left")
        ttk.Button(wp, text="Test alarm", command=self.watch_test).pack(side="right")
        ttk.Button(wp, text="Settings…", command=self.watch_settings).pack(side="right", padx=6)
        self.watch_btn = ttk.Button(wp, text="Turn on", style="Accent.TButton", command=self.watch_toggle)
        self.watch_btn.pack(side="right")
        self.after(800, self.watch_refresh)

        self.detail = tk.Text(root, bg=PANEL, fg=FG, bd=0, highlightthickness=0,
                              font=("DejaVu Sans Mono", 9), padx=10, pady=8)
        self.detail.pack(fill="both", expand=True)

    def render_arrays(self, arrays):
        sel = self.current_array_name()
        self.arrays = arrays
        self.atree.delete(*self.atree.get_children())
        best = None
        for a in arrays:
            members = [d for d, s in a["devices"] if s != "spare"]
            sp = sum(1 for d, s in a["devices"] if s == "spare")
            drives = a["health"] or f"{len(members)}"
            if sp:
                drives += f" +{sp} spare"
            degraded = "_" in a["health"] or any(s == "failed" for _, s in a["devices"])
            state = "DEGRADED" if degraded else a["state"]
            act = ""
            if a["op"]:
                act = f"{a['op']} {a['pct']:.1f}%" if a["pct"] is not None else a["op"]
                if a["finish"]:
                    act += f"  ~{a['finish']}  {a['speed']}"
                best = best or a
            sp = a.get("space")
            low = sp and sp[1] and sp[0] / sp[1] * 100
            tag = "bad" if degraded or a["state"] != "active" or (sp and low < 2) else \
                ("warn" if a["op"] or (sp and low < 10) else "good")
            self.atree.insert("", "end", iid=a["name"], tags=(tag,),
                              values=(f"{a['alias']}  ({a['name']})", a["level"].upper(),
                                      human(a["size"]) if a["size"] else "—", free_text(a.get("space")), drives, state,
                                      a["mount"] or "not mounted", act or "idle ✓"))
        if not arrays:
            self.atree.insert("", "end", iid="_none", values=("No arrays yet — build one on tab ①", "", "", "", "", "", "", ""))
        if sel and self.atree.exists(sel):
            self.atree.selection_set(sel)
        elif arrays and not self.atree.selection():
            self.atree.selection_set(arrays[0]["name"])
        if best and best["pct"] is not None:
            self.aprog["value"] = best["pct"]
            self.aprog_lbl.configure(text=f"{best['alias']}: {best['op']} {best['pct']:.1f}%  "
                                          f"{'— about ' + best['finish'] + ' left' if best['finish'] else ''}")
        else:
            self.aprog["value"] = 0
            self.aprog_lbl.configure(text="")

    def current_array_name(self):
        s = self.atree.selection() if hasattr(self, "atree") else ()
        return s[0] if s and s[0] != "_none" else None

    def current_array(self):
        n = self.current_array_name()
        a = next((a for a in self.arrays if a["name"] == n), None)
        if not a:
            messagebox.showinfo(APP, "Select an array first.")
        return a

    def _set_detail(self, text):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", text)
        self.detail.configure(state="disabled")

    def show_detail(self):
        n = self.current_array_name()
        if not n:
            return

        def job():
            if DEMO:
                out = (f"/dev/{n}:\n           Version : 1.2\n        Raid Level : raid5\n"
                       "        Array Size : 3906764800 (3.64 TiB)\n      Raid Devices : 3\n"
                       "             State : clean, resyncing\n    Active Devices : 3\n"
                       "        Chunk Size : 512K\n\n    Number   Major   Minor   RaidDevice State\n"
                       "       0       8        0        0      active sync   /dev/sda\n"
                       "       1       8       16        1      active sync   /dev/sdb\n"
                       "       2       8       32        2      active sync   /dev/sdc\n")
            else:
                rc, out = self.sys.run(["mdadm", "--detail", f"/dev/{n}"], root=True, quiet=True)
            self.ui(lambda: self._set_detail(out))
        threading.Thread(target=job, daemon=True).start()

    def assemble(self):
        self.work("assembling", lambda: self.sys.run(["mdadm", "--assemble", "--scan"], root=True))

    def scrub(self):
        a = self.current_array()
        if not a:
            return
        if a["level"] == "raid0":
            return messagebox.showinfo(APP, "RAID 0 has no redundancy to check.")
        if messagebox.askyesno(APP, f"Read every block of {a['alias']} and verify mirrors/parity?\n"
                                    "The array stays usable but slower while it runs."):
            self.work("scrubbing", lambda: self.sys.write_root_file(
                f"/sys/block/{a['name']}/md/sync_action", "check\n"))

    def speed_test(self):
        a = self.current_array()
        if not a:
            return
        if not a["mount"]:
            return messagebox.showinfo(APP, "Mount the array first (it isn't mounted).")
        f = os.path.join(a["mount"], ".raidforge-speedtest")

        def job():
            self.log(f"Speed test on {a['alias']} (writes then reads 1 GB)…", "info")
            if DEMO:
                self.log("  write: 412 MB/s   read: 538 MB/s (demo numbers)", "good")
                return
            rc, out = self.sys.run(["dd", "if=/dev/zero", f"of={f}", "bs=1M", "count=1024",
                                    "oflag=direct", "conv=fsync"], root=True, quiet=True, timeout=600)
            w = re.findall(r"([\d.,]+ [kMG]B/s)", out)
            rc, out = self.sys.run(["dd", f"if={f}", "of=/dev/null", "bs=1M", "iflag=direct"],
                                   root=True, quiet=True, timeout=600)
            r = re.findall(r"([\d.,]+ [kMG]B/s)", out)
            self.sys.run(["rm", "-f", f], root=True, quiet=True)
            self.log(f"  write: {w[-1] if w else '?'}   read: {r[-1] if r else '?'}", "good")
        self.work("speed test", job)

    def _pick_from(self, title, prompt, options):
        """Small chooser dialog; returns selected value or None."""
        if not options:
            messagebox.showinfo(APP, "Nothing available to choose.")
            return None
        win = tk.Toplevel(self, bg=PANEL)
        win.title(title)
        win.transient(self)
        win.grab_set()
        tk.Label(win, text=prompt, bg=PANEL, fg=FG, justify="left").pack(anchor="w", padx=16, pady=(14, 6))
        v = tk.StringVar(value=options[0][0])
        for val, label in options:
            ttk.Radiobutton(win, text=label, value=val, variable=v).pack(anchor="w", padx=22, pady=2)
        res = {}
        bf = tk.Frame(win, bg=PANEL)
        bf.pack(fill="x", padx=16, pady=12)
        ttk.Button(bf, text="OK", style="Accent.TButton",
                   command=lambda: (res.setdefault("v", v.get()), win.destroy())).pack(side="right")
        ttk.Button(bf, text="Cancel", command=win.destroy).pack(side="right", padx=8)
        self.wait_window(win)
        return res.get("v")

    def _free_disks(self):
        return [(d["path"], f"{d['path']}  {human(d['size'])}  {d['model']}")
                for d in self.sys.disks() if not d.get("busy")]

    def replace_drive(self):
        a = self.current_array()
        if not a:
            return
        if a["level"] == "raid0":
            return messagebox.showinfo(APP, "RAID 0 can't survive a drive change — rebuild it instead.")
        outgoing = [(f"/dev/{d}", f"/dev/{d}  ({s})") for d, s in a["devices"]]
        old = self._pick_from("Replace drive", "Which drive is leaving the array?\n"
                                               "(Pick the failed one — it'll be marked failed and removed.)", outgoing)
        if not old:
            return
        if messagebox.askyesno(APP, f"Want help finding {old} first?\n\nRAID Forge can blink the other drives in the "
                                    "array. The one whose light stays dark is the one to pull."):
            others = [disk_of(d) for d, s in a["devices"] if f"/dev/{d}" != old and s != "failed"]
            w = self.identify(others, f"Find {old}", note=(
                f"The other drives blink together in time with the dot. {old} is the one that stays dark. "
                "Match its port and serial too. Press Stop when you've found it."), target=[os.path.basename(old)])
            if isinstance(w, tk.Toplevel):
                self.wait_window(w)
        new = self._pick_from("Replace drive", "Which new drive goes in?\n(It will be erased.)", self._free_disks())
        if not new:
            return
        md = f"/dev/{a['name']}"

        def job():
            self.sys.run(["mdadm", "--manage", md, "--fail", old], root=True)
            self.sys.run(["mdadm", "--manage", md, "--remove", old], root=True)
            self.sys.run(["wipefs", "-a", new], root=True)
            rc, _ = self.sys.run(["mdadm", "--manage", md, "--add", new], root=True)
            self.log(f"✓ {new} added — rebuilding. Watch progress above." if rc == 0 else "Add failed.",
                     "good" if rc == 0 else "bad")
        if messagebox.askyesno(APP, f"Remove {old} and rebuild onto {new}?\n{new} will be erased."):
            self.work("replacing drive", job)

    def add_spare(self):
        a = self.current_array()
        if not a:
            return
        if a["level"] == "raid0":
            return messagebox.showinfo(APP, "RAID 0 can't use hot spares.")
        new = self._pick_from("Add hot spare", "Pick a drive to stand by as a hot spare.\n(It will be erased.)",
                              self._free_disks())
        if new and messagebox.askyesno(APP, f"Erase {new} and add it as a spare?"):
            def job():
                self.sys.run(["wipefs", "-a", new], root=True)
                self.sys.run(["mdadm", "--manage", f"/dev/{a['name']}", "--add-spare", new], root=True)
            self.work("adding spare", job)

    def _is_system(self, a):
        if a and a.get("mount") == "/":
            messagebox.showerror(APP, f"{a['alias']} holds the running system, so it can't be stopped or "
                                      "deleted from here.\nUse 'Boot from SD again…' and reboot first.")
            return True
        return False

    def stop_array(self):
        a = self.current_array()
        if self._is_system(a):
            return
        if a and messagebox.askyesno(APP, f"Unmount and stop {a['alias']}?\nData is kept — "
                                          "use 'Find & start existing arrays' to bring it back."):
            def job():
                if a["mount"]:
                    self.sys.run(["umount", a["mount"]], root=True)
                self.sys.run(["mdadm", "--stop", f"/dev/{a['name']}"], root=True)
            self.work("stopping", job)

    def delete_array(self):
        a = self.current_array()
        if not a or self._is_system(a):
            return
        short = a["alias"].split("/")[-1]
        typed = simpledialog.askstring(APP, f"This destroys {a['alias']} and ALL its data,\n"
                                            f"and removes it from fstab and mdadm.conf.\n\n"
                                            f"Type the array name ({short}) to confirm:", parent=self)
        if typed != short:
            return
        md = f"/dev/{a['name']}"

        def job():
            S = self.sys
            rc, out = S.run(["mdadm", "--detail", "--export", md], root=True, quiet=True)
            mu = re.search(r"MD_UUID=(\S+)", out)
            rc, out = S.run(["blkid", "-s", "UUID", "-o", "value", md], root=True, quiet=True)
            fsu = out.strip()
            if a["mount"]:
                S.run(["umount", a["mount"]], root=True)
            S.run(["mdadm", "--stop", md], root=True)
            for d, _ in a["devices"]:
                S.run(["mdadm", "--zero-superblock", f"/dev/{d}"], root=True)
                S.run(["wipefs", "-a", f"/dev/{d}"], root=True)
            for path, key in (("/etc/fstab", fsu), (MDADM_CONF, mu.group(1) if mu else "")):
                if not key or DEMO:
                    continue
                try:
                    txt = open(path).read()
                except OSError:
                    continue
                if key in txt:
                    new = "".join(l for l in txt.splitlines(True) if key not in l)
                    S.write_root_file(path, new)
            if shutil.which("update-initramfs"):
                S.run(["update-initramfs", "-u"], root=True, timeout=900)
            self.log(f"✓ {a['alias']} deleted; its drives are free again.", "good")
        self.work("deleting array", job)

    # ============================================================ RAID WATCH (monitor + alarm)
    def _watch_state(self):
        if DEMO:
            return getattr(self, "_demo_watch", False), {"level": "ok", "alerts": [], "infos": []}
        rc, out = self.sys.run(["systemctl", "is-active", "raidwatch"], quiet=True)
        on = out.strip() == "active"
        try:
            with open(WATCH_STATUS) as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = None
        return on, st

    def watch_refresh(self):
        def job():
            on, st = self._watch_state()

            def show():
                if on and st:
                    lv = st.get("level", "ok")
                    n = len(st.get("alerts", []))
                    txt = {"ok": "  ● on: all arrays healthy", "info": "  ● on: healthy, " + (st.get("infos") or [""])[0],
                           "warn": f"  ● on: {n} warning{'s' if n != 1 else ''}",
                           "crit": "  ● on: RAID NEEDS SERVICE"}.get(lv, "  ● on")
                    col = {"ok": GOOD, "info": INFO, "warn": WARN, "crit": BAD}.get(lv, GOOD)
                elif on:
                    txt, col = "  ● on: starting…", GOOD
                else:
                    txt, col = "  ○ off: nothing is watching your arrays. Turn it on for alarms", WARN
                self.watch_lbl.configure(text=txt, fg=col)
                self.watch_btn.configure(text="Turn off" if on else "Turn on",
                                         style="TButton" if on else "Accent.TButton")
            self.ui(show)
        threading.Thread(target=job, daemon=True).start()
        self.after(10000, self.watch_refresh)

    def watch_toggle(self):
        on, _ = self._watch_state()
        S = self.sys
        script = os.path.join(HERE, "raidwatch.py")
        if not on and not os.path.exists(script):
            return messagebox.showerror(APP, f"raidwatch.py isn't next to RAID Forge ({HERE}). Reinstall RAID Forge.")

        def job():
            if DEMO:
                self._demo_watch = not on
            if on:
                S.run(["systemctl", "disable", "--now", "raidwatch"], root=True)
                S.run(["rm", "-f", WATCH_AUTOSTART], root=True, quiet=True)
                self.log("RAID Watch turned off. Nothing is watching the arrays now.", "warn")
            else:
                need = [p for p, tool in (("smartmontools", "smartctl"), ("libnotify-bin", "notify-send"))
                        if not S.has(tool)]
                if need and not DEMO:
                    S.run(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                           "--no-install-recommends"] + need, root=True, timeout=900)
                py = sys.executable or "/usr/bin/python3"
                S.write_root_file(WATCH_UNIT, f"""[Unit]
Description=RAID Watch: alarms when a RAID array needs service
After=local-fs.target mdmonitor.service

[Service]
ExecStart={py} {script} --daemon
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
""")
                desk = f"""[Desktop Entry]
Type=Application
Name=RAID Watch
Comment=Shows your RAID's health and sounds the alarm if it needs service
Exec={py} {script}
Icon={os.path.join(HERE, "raidwatch.png")}
Terminal=false
Categories=System;Monitor;
"""
                S.run(["mkdir", "-p", os.path.dirname(WATCH_AUTOSTART)], root=True, quiet=True)
                S.write_root_file(WATCH_AUTOSTART, desk)
                S.write_root_file(WATCH_MENU, desk)
                S.run(["systemctl", "daemon-reload"], root=True, quiet=True)
                rc, _ = S.run(["systemctl", "enable", "--now", "raidwatch"], root=True)
                self.log("✓ RAID Watch is on. It starts with the Pi; its window opens when you log in."
                         if rc == 0 else "Couldn't start RAID Watch.", "good" if rc == 0 else "bad")
                if rc == 0:
                    self.ui(self.watch_open)
            self.ui(self.watch_refresh)
        self.work("RAID Watch", job)

    def watch_open(self):
        """Open the RAID Watch window as you (not as admin)."""
        cmd = [sys.executable, os.path.join(HERE, "raidwatch.py")] + (["--demo"] if DEMO else [])
        if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
            env = [f"{k}={os.environ[k]}" for k in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY",
                                                    "DBUS_SESSION_BUS_ADDRESS") if os.environ.get(k)]
            cmd = ["runuser", "-u", os.environ["SUDO_USER"], "--", "env", *env] + cmd
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def watch_test(self):
        on, _ = self._watch_state()
        if not on:
            return messagebox.showinfo(APP, "Turn RAID Watch on first.")

        def job():
            self.sys.run(["mkdir", "-p", "/run/raidwatch"], root=True, quiet=True)
            self.sys.run(["touch", "/run/raidwatch/test-alarm"], root=True)
            self.log("Test alarm sent: within ~15 s the RAID Watch window alarms, the green light flashes fast, "
                     "and your phone buzzes if phone alerts are set. It clears itself after a minute.", "good")
        self.work("test alarm", job)

    def watch_settings(self):
        cur = {}
        if not DEMO:                                  # the settings file is admin-only (it holds the web password hash)
            rc, out = self.sys.run(["cat", WATCH_CONF], root=True, quiet=True)
            try:
                cur = json.loads(out) if rc == 0 else {}
            except ValueError:
                cur = {}
        win = tk.Toplevel(self, bg=PANEL)
        win.title("RAID Watch settings")
        win.transient(self)
        win.grab_set()
        tk.Label(win, text="RAID Watch settings", bg=PANEL, fg=FG, font=("DejaVu Sans", 13, "bold")
                 ).pack(anchor="w", padx=18, pady=(14, 4))
        body = tk.Frame(win, bg=PANEL)
        body.pack(fill="x", padx=18)
        sound = tk.BooleanVar(value=cur.get("sound", True))
        led = tk.BooleanVar(value=cur.get("led_alarm", True))
        remind = tk.StringVar(value=str(cur.get("remind_hours", 4)))
        smart = tk.StringVar(value=str(cur.get("smart_hours", 6)))
        topic = tk.StringVar(value=cur.get("ntfy_topic", ""))
        server = tk.StringVar(value=cur.get("ntfy_server", "https://ntfy.sh"))
        ttk.Checkbutton(body, text="Alarm sound for critical problems (plays through the Pi's speakers/HDMI)",
                        variable=sound).pack(anchor="w", pady=2)
        ttk.Checkbutton(body, text="Flash the Pi's green light fast while something is critical",
                        variable=led).pack(anchor="w", pady=2)
        r = tk.Frame(body, bg=PANEL)
        r.pack(anchor="w", pady=4)
        tk.Label(r, text="After Silence, alarm again if still broken after", bg=PANEL, fg=FG).pack(side="left")
        ttk.Combobox(r, textvariable=remind, values=["1", "4", "12", "24"], width=4, state="readonly").pack(side="left", padx=4)
        tk.Label(r, text="hours", bg=PANEL, fg=FG).pack(side="left")
        r = tk.Frame(body, bg=PANEL)
        r.pack(anchor="w", pady=4)
        tk.Label(r, text="Check drive health (SMART) every", bg=PANEL, fg=FG).pack(side="left")
        ttk.Combobox(r, textvariable=smart, values=["1", "6", "12", "24"], width=4, state="readonly").pack(side="left", padx=4)
        tk.Label(r, text="hours", bg=PANEL, fg=FG).pack(side="left")
        tk.Label(body, text="Phone alerts (optional, needs internet)", bg=PANEL, fg=FG,
                 font=("DejaVu Sans", 10, "bold")).pack(anchor="w", pady=(12, 0))
        tk.Label(body, bg=PANEL, fg=MUTED, justify="left", wraplength=560, text=(
            "Install the free ntfy app on your phone, tap +, and subscribe to the topic below. RAID Watch then "
            "sends a push when a RAID needs service and when it's fixed. Anyone who knows the topic name can "
            "read it, so keep the random one.")).pack(anchor="w")
        r = tk.Frame(body, bg=PANEL)
        r.pack(anchor="w", pady=4, fill="x")
        tk.Label(r, text="Topic", bg=PANEL, fg=FG, width=7, anchor="w").pack(side="left")
        ttk.Entry(r, textvariable=topic, width=34).pack(side="left")

        def gen():
            import secrets
            topic.set(f"raid-{os.uname().nodename}-{secrets.token_hex(5)}")
        ttk.Button(r, text="Make one", command=gen).pack(side="left", padx=6)
        r = tk.Frame(body, bg=PANEL)
        r.pack(anchor="w", pady=2, fill="x")
        tk.Label(r, text="Server", bg=PANEL, fg=FG, width=7, anchor="w").pack(side="left")
        ttk.Entry(r, textvariable=server, width=34).pack(side="left")

        # over-temperature alarm
        talarm = tk.BooleanVar(value=cur.get("temp_alarm", False))
        tdrive = tk.StringVar(value=str(cur.get("temp_alarm_drive", 65)))
        tcpu = tk.StringVar(value=str(cur.get("temp_alarm_cpu", 80)))
        tk.Label(body, text="Over-temperature alarm (optional)", bg=PANEL, fg=FG,
                 font=("DejaVu Sans", 10, "bold")).pack(anchor="w", pady=(12, 0))
        ttk.Checkbutton(body, text="Sound the alarm if a drive or the Pi gets too hot (checks every 2 minutes)",
                        variable=talarm).pack(anchor="w", pady=2)
        r = tk.Frame(body, bg=PANEL)
        r.pack(anchor="w", pady=2)
        tk.Label(r, text="Drives at", bg=PANEL, fg=FG).pack(side="left")
        ttk.Combobox(r, textvariable=tdrive, values=["55", "60", "65", "70", "75"], width=4,
                     state="readonly").pack(side="left", padx=4)
        tk.Label(r, text="°C     Pi processor at", bg=PANEL, fg=FG).pack(side="left")
        ttk.Combobox(r, textvariable=tcpu, values=["70", "75", "80", "85"], width=4,
                     state="readonly").pack(side="left", padx=4)
        tk.Label(r, text="°C   (warns 5 °C before)", bg=PANEL, fg=MUTED).pack(side="left")

        # web page
        webon = tk.BooleanVar(value=cur.get("web", False))
        port = tk.StringVar(value=str(cur.get("web_port", 8484)))
        pw1, pw2 = tk.StringVar(), tk.StringVar()
        has_pw = bool(cur.get("web_password"))
        tk.Label(body, text="Web page (optional)", bg=PANEL, fg=FG,
                 font=("DejaVu Sans", 10, "bold")).pack(anchor="w", pady=(12, 0))
        ttk.Checkbutton(body, text="Show RAID Watch on a web page for phones/PCs on your home network",
                        variable=webon).pack(anchor="w", pady=2)
        r = tk.Frame(body, bg=PANEL)
        r.pack(anchor="w", pady=2)
        tk.Label(r, text="Port", bg=PANEL, fg=FG, width=9, anchor="w").pack(side="left")
        ttk.Entry(r, textvariable=port, width=7).pack(side="left")
        r = tk.Frame(body, bg=PANEL)
        r.pack(anchor="w", pady=2)
        tk.Label(r, text="Password", bg=PANEL, fg=FG, width=9, anchor="w").pack(side="left")
        ttk.Entry(r, textvariable=pw1, width=20, show="•").pack(side="left")
        tk.Label(r, text=" again", bg=PANEL, fg=FG).pack(side="left")
        ttk.Entry(r, textvariable=pw2, width=20, show="•").pack(side="left", padx=4)
        tk.Label(body, bg=PANEL, fg=MUTED, justify="left", wraplength=560, text=(
            ("A password is set. Leave these blank to keep it. " if has_pw else "Pick a password of 8+ characters. ")
            + "Use the page on your home network only; don't open this port to the internet.")).pack(anchor="w")
        bf = tk.Frame(win, bg=PANEL)
        bf.pack(fill="x", padx=18, pady=14)

        def save():
            conf = dict(cur, sound=sound.get(), led_alarm=led.get(), remind_hours=int(remind.get()),
                        smart_hours=int(smart.get()), ntfy_topic=topic.get().strip(),
                        ntfy_server=server.get().strip() or "https://ntfy.sh",
                        temp_alarm=talarm.get(), temp_alarm_drive=int(tdrive.get()), temp_alarm_cpu=int(tcpu.get()),
                        web=webon.get())
            if not port.get().strip().isdigit() or not 1024 <= int(port.get()) <= 65535:
                return messagebox.showerror(APP, "The web page port must be a number from 1024 to 65535.", parent=win)
            conf["web_port"] = int(port.get())
            if pw1.get() or pw2.get():
                if pw1.get() != pw2.get():
                    return messagebox.showerror(APP, "The two passwords don't match.", parent=win)
                if len(pw1.get()) < 8:
                    return messagebox.showerror(APP, "Use a password of at least 8 characters.", parent=win)
                sys.path.insert(0, HERE)
                import raidwatch
                conf["web_password"] = raidwatch.make_hash(pw1.get())
            elif webon.get() and not has_pw:
                return messagebox.showerror(APP, "Set a password for the web page first.", parent=win)
            win.destroy()

            def job():
                if self.sys.write_root_file(WATCH_CONF, json.dumps(conf, indent=1) + "\n") == 0:
                    self.sys.run(["chmod", "600", WATCH_CONF], root=True, quiet=True)
                    msg = "✓ RAID Watch settings saved (used from its next check)."
                    if conf["ntfy_topic"]:
                        msg += "  Phone alerts go to ntfy topic " + conf["ntfy_topic"] + "."
                    if conf["temp_alarm"]:
                        msg += f"  Over-temperature alarm: drives {conf['temp_alarm_drive']} °C, Pi {conf['temp_alarm_cpu']} °C."
                    self.log(msg, "good")
                    if conf["web"]:
                        rc, ips = self.sys.run(["hostname", "-I"], quiet=True)
                        ip = (ips.split() or ["<this Pi's address>"])[0] if not DEMO else "192.168.1.50"
                        self.log(f"  Web page: http://{ip}:{conf['web_port']}  (sign in with your password)", "good")
            self.work("saving RAID Watch settings", job)
        ttk.Button(bf, text="Save", style="Accent.TButton", command=save).pack(side="right")
        ttk.Button(bf, text="Cancel", command=win.destroy).pack(side="right", padx=8)

    # ============================================================ IDENTIFY (blink a drive's light)
    def _disk_dicts(self, names):
        info = {d["name"]: d for d in self.sys.disks()}
        out = []
        for n in names:
            d = dict(info.get(n) or {"name": n, "path": f"/dev/{n}", "size": 0, "model": "", "serial": ""})
            d["where"] = self.sys.location(n)
            out.append(d)
        return out

    def identify(self, names, title="Identify drive", note=None, target=None):
        """Blink the activity light of the given disks (by name) in a slow on/off rhythm."""
        if not self.sys.can_blink():
            return messagebox.showinfo(APP, "Blinking a drive needs admin rights. Restart RAID Forge and enter "
                                            "your password when it asks.")
        disks = self._disk_dicts(names)
        busy = [a for a in self.arrays if a.get("op")]
        on_s = off_s = 2.0 if busy else 1.0
        stop = threading.Event()
        left = {"s": 30}
        win = tk.Toplevel(self, bg=PANEL)
        win.title(title)
        win.transient(self)
        tk.Label(win, text=title, bg=PANEL, fg=FG, font=("DejaVu Sans", 14, "bold")).pack(anchor="w", padx=18, pady=(16, 2))
        tk.Label(win, bg=PANEL, fg=MUTED, justify="left", wraplength=640, text=note or (
            f"Look for the activity light that goes on for {on_s:.0f} s, then off for {off_s:.0f} s, in time with "
            "the dot below. It only reads from the drive, so it's safe even while the array is in use.")
                 ).pack(anchor="w", padx=18)
        if busy:
            tk.Label(win, bg=PANEL, fg=WARN, justify="left", wraplength=640, text=(
                f"{busy[0]['alias']} is busy ({busy[0]['op']}), so every light is flickering. RAID Forge is using "
                "longer 2-second bursts: look for the light that goes fully dark for 2 seconds at a time.")
                     ).pack(anchor="w", padx=18, pady=(6, 0))
        mid = tk.Frame(win, bg=PANEL)
        mid.pack(fill="x", padx=18, pady=12)
        dot = tk.Canvas(mid, width=64, height=64, bg=PANEL, highlightthickness=0)
        dot.pack(side="left", padx=(0, 16))
        lamp = dot.create_oval(8, 8, 56, 56, fill=LINE, outline="")
        lst = tk.Frame(mid, bg=PANEL)
        lst.pack(side="left", fill="x", expand=True)
        for d in disks:
            r = tk.Frame(lst, bg=CARD)
            r.pack(fill="x", pady=2)
            tk.Label(r, text=f"{d['path']}   {human(d['size']) if d['size'] else ''}", bg=CARD, fg=FG,
                     font=("DejaVu Sans", 10, "bold")).pack(anchor="w", padx=10, pady=(4, 0))
            sub = "  ·  ".join(x for x in (d.get("model"), f"serial {d['serial']}" if d.get("serial") else "",
                                            d.get("where")) if x)
            tk.Label(r, text=sub or "no details reported", bg=CARD, fg=MUTED, font=("DejaVu Sans", 9),
                     wraplength=520, justify="left").pack(anchor="w", padx=10, pady=(0, 4))
        for d in self._disk_dicts(target or []):
            r = tk.Frame(win, bg=PANEL, highlightthickness=2, highlightbackground=WARN)
            r.pack(fill="x", padx=18, pady=(0, 10))
            tk.Label(r, text=f"Looking for {d['path']} (its light stays dark)", bg=PANEL, fg=WARN,
                     font=("DejaVu Sans", 10, "bold")).pack(anchor="w", padx=10, pady=(4, 0))
            sub = "  ·  ".join(x for x in (d.get("model"), f"serial {d['serial']}" if d.get("serial") else "",
                                            d.get("where")) if x)
            tk.Label(r, text=sub or "It isn't answering, so check the drives not listed above",
                     bg=PANEL, fg=MUTED, font=("DejaVu Sans", 9)).pack(anchor="w", padx=10, pady=(0, 4))
        status = tk.Label(win, text="", bg=PANEL, fg=MUTED)
        status.pack(anchor="w", padx=18)
        bf = tk.Frame(win, bg=PANEL)
        bf.pack(fill="x", padx=18, pady=14)
        more = ttk.Button(bf, text="Keep blinking (+30 s)", command=lambda: left.update(s=left["s"] + 30))
        more.pack(side="left")

        def close():
            stop.set()
            win.destroy()
        ttk.Button(bf, text="Stop", style="Accent.TButton", command=close).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", close)

        def phase(on):
            self.ui(lambda: win.winfo_exists() and dot.itemconfigure(lamp, fill=ACCENT if on else LINE))

        def tick():
            if not win.winfo_exists() or stop.is_set():
                return
            left["s"] -= 1
            if left["s"] <= 0:
                return close()
            status.configure(text=f"Blinking… stops in {left['s']} s")
            win.after(1000, tick)
        names_txt = ", ".join(d["path"] for d in disks)
        self.log(f"Blinking {names_txt} (reads only).", "info")
        threading.Thread(target=self.sys.blink, args=(disks, stop, on_s, off_s, phase), daemon=True).start()
        tick()
        return win

    def identify_member(self):
        """My Arrays: blink one member, or all the healthy ones to find a dead/missing drive."""
        a = self.current_array()
        if not a:
            return
        members = [(disk_of(d), s) for d, s in a["devices"]]
        healthy = [n for n, s in members if s != "failed"]
        failed = [n for n, s in members if s == "failed"]
        opts = [(f"one:{n}", f"/dev/{n}  ({s})") for n, s in members]
        opts.append(("healthy", "Blink all the healthy drives: the one that stays dark is the failed or missing one"))
        pick = self._pick_from("Identify drives", f"Which drive in {a['alias']} do you want to find?", opts)
        if not pick:
            return
        if pick == "healthy":
            self.identify(healthy, "Find the failed drive", note=(
                "Every healthy drive in the array blinks together in time with the dot. The failed or missing "
                "drive is the one whose light stays dark (or doesn't match). Check its port and serial below "
                "against the others."), target=failed)
        else:
            self.identify([pick[4:]])

    def identify_health_row(self):
        sel = self.htree.selection()
        if not sel:
            return messagebox.showinfo(APP, "Click a drive in the list first (run Check all drives if it's empty).")
        self.identify([os.path.basename(self.htree.item(sel[0], "values")[0])])

    # ============================================================ MOVE SYSTEM TO RAID
    def _migration_checks(self, a):
        """Returns (items, info). items: [(state, text)] with state ok/warn/bad."""
        S, items = self.sys, []
        md = f"/dev/{a['name']}"
        info = dict(md=md, fstype="ext4", fs_uuid="", nonempty=0, need_tools=[], need_initramfs=False, swap="none",
                    unformatted=False)
        add = lambda st, t: items.append((st, t))
        if DEMO:
            used, size = 9_800_000_000, a["size"] or 4_000_000_000_000
            add("ok", "The system is on the SD card (/dev/mmcblk0p2)")
            add("ok", "Boot files (/dev/mmcblk0p1) stay on the SD card")
            add("ok", f"{a['level'].upper()} array is healthy")
            add("ok", f"Needs about {human(used * 1.15)}, the array has {human(size)}")
            add("warn", "The array has 3 files/folders on it, so it must be erased first")
            add("ok", "Startup files (initramfs) are on and will include RAID support")
            add("warn", "Swap is a file on the drive (dphys-swapfile). The low-wear option moves it into RAM (zram)")
            info.update(fs_uuid="demo-fs-uuid", nonempty=3, used=used, swap="dphys")
            return items, info

        src = S.root_source()
        if "/md" in src:
            add("bad", f"The system already runs from RAID ({src}). Use 'Boot from SD again…' to go back.")
        elif "mmcblk" not in src:
            add("bad", f"The system isn't on the SD card ({src}). This tool moves an SD-card system.")
        else:
            add("ok", f"The system is on the SD card ({src})")
        rc, bsrc = S.run(["findmnt", "-no", "SOURCE", BOOT_DIR], quiet=True)
        bsrc = bsrc.strip()
        if not bsrc or not os.path.exists(CMDLINE):
            add("bad", f"Can't find the boot partition ({BOOT_DIR}/cmdline.txt)")
        else:
            add("ok", f"Boot files ({bsrc}) stay on the SD card")

        if a["state"] != "active" or "_" in a.get("health", "") or any(s == "failed" for _, s in a["devices"]):
            add("bad", "The array is degraded or not running. Fix it first (Replace a drive…)")
        elif a["op"] and a["pct"] is not None:
            add("warn", f"Still syncing ({a['pct']:.0f}%). That's OK, it keeps syncing after the move, but the copy will be slower")
        else:
            add("ok", f"{a['level'].upper()} array is healthy")
        if a["level"] == "raid0":
            add("warn", "RAID 0: if either drive dies the whole system is lost. RAID 1 is the safe choice")

        rc, out = S.run(["blkid", "-o", "export", md], root=True, quiet=True)
        kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        if kv.get("TYPE") in ("ext4", "xfs"):
            info["fstype"], info["fs_uuid"] = kv["TYPE"], kv.get("UUID", "")
        else:
            info["unformatted"] = True
            add("warn", "The array has no ext4/XFS filesystem yet. It will be formatted as ext4")

        rc, out = S.run(["blockdev", "--getsize64", md], root=True, quiet=True)
        size = int(out.strip() or 0) if out.strip().isdigit() else (a["size"] or 0)
        used = S.used_bytes("/")
        info["used"] = used
        need = used * 1.15 + 1_000_000_000
        if size and size < need:
            add("bad", f"Not enough room: the system needs about {human(need)}, the array has {human(size)}")
        else:
            add("ok", f"Needs about {human(need)}, the array has {human(size)}")

        if not info["unformatted"]:
            mnt, tmp = a.get("mount"), False
            if not mnt:
                S.run(["mkdir", "-p", NEWROOT], root=True, quiet=True)
                rc, _ = S.run(["mount", "-o", "ro", md, NEWROOT], root=True, quiet=True)
                mnt, tmp = (NEWROOT, True) if rc == 0 else (None, False)
            if mnt:
                rc, out = S.run(["ls", "-A", mnt], root=True, quiet=True)
                info["nonempty"] = len([x for x in out.split() if x != "lost+found"])
            if tmp:
                S.run(["umount", NEWROOT], root=True, quiet=True)
            if info["nonempty"]:
                add("warn", f"The array has {info['nonempty']} files/folders on it ({a.get('mount') or 'not mounted'}), "
                            "so it must be erased first")
            else:
                add("ok", "The array is empty")

        for tool, pkg in (("rsync", "rsync"), ("update-initramfs", "initramfs-tools")):
            if not S.has(tool):
                info["need_tools"].append(pkg)
        if not S.has("mdadm"):
            add("bad", "mdadm isn't installed")
        if info["need_tools"]:
            add("warn", f"Will install first: {', '.join(info['need_tools'])}")
        if not re.search(r"^\s*(auto_initramfs=1|initramfs\s)", S.read_text(CONFIG_TXT), re.M):
            info["need_initramfs"] = True
            add("warn", "Startup files (initramfs) are off. They'll be turned on so the Pi can find the RAID")
        else:
            add("ok", "Startup files (initramfs) are on and will include RAID support")
        rc, sw = S.run(["swapon", "--show=NAME", "--noheadings"], quiet=True)
        if "zram" in sw:
            info["swap"] = "zram"
            add("ok", "Swap already lives in RAM (zram), so it won't wear the drives")
        elif S.has("dphys-swapfile") or os.path.exists("/etc/dphys-swapfile"):
            info["swap"] = "dphys"
            add("warn", "Swap is a file on the drive (dphys-swapfile). The low-wear option moves it into RAM (zram)")
        return items, info

    def migrate_wizard(self):
        a = self.current_array()
        if not a:
            return
        if self.busy:
            return messagebox.showinfo(APP, "Another job is still running. Wait for it to finish.")
        win = tk.Toplevel(self, bg=PANEL)
        win.title("Move system to RAID")
        win.transient(self)
        win.geometry("760x640")
        tk.Label(win, text=f"Move the whole system onto {a['alias']}", bg=PANEL, fg=FG,
                 font=("DejaVu Sans", 14, "bold")).pack(anchor="w", padx=18, pady=(16, 4))
        tk.Label(win, bg=PANEL, fg=MUTED, justify="left", wraplength=720, text=(
            "Copies everything on the SD card (programs, settings, your files) onto the array, then tells the "
            "Pi to load the system from the array. The SD card keeps only the small boot partition, so the Pi "
            "still starts from it, and it keeps its old copy of the system as a fallback. "
            "'Boot from SD again…' switches back at any time.")).pack(anchor="w", padx=18)

        box = tk.Frame(win, bg=CARD)
        box.pack(fill="x", padx=18, pady=12)
        tk.Label(box, text="Checking…", bg=CARD, fg=MUTED).pack(anchor="w", padx=12, pady=10)

        erase = tk.BooleanVar(value=False)
        er = ttk.Checkbutton(win, text="Erase everything on the array first", variable=erase)
        lowwear = tk.BooleanVar(value=True)
        lw = ttk.Checkbutton(win, variable=lowwear,
                             text="Low-wear: keep swap in compressed RAM (zram) instead of a file on the drives")
        prog = ttk.Progressbar(win, mode="determinate", maximum=100)
        stat = tk.Label(win, text="", bg=PANEL, fg=MUTED, anchor="w", justify="left", wraplength=720)
        bf = tk.Frame(win, bg=PANEL)
        bf.pack(side="bottom", fill="x", padx=18, pady=14)
        go = ttk.Button(bf, text="Move system to RAID", style="Accent.TButton")
        go.pack(side="right")
        go.state(["disabled"])
        close = ttk.Button(bf, text="Close", command=win.destroy)
        close.pack(side="right", padx=8)
        state = {}

        def show(items, info):
            if not win.winfo_exists():
                return
            for w in box.winfo_children():
                w.destroy()
            icon = {"ok": ("✓", GOOD), "warn": ("!", WARN), "bad": ("✗", BAD)}
            for st, text in items:
                r = tk.Frame(box, bg=CARD)
                r.pack(fill="x", padx=12, pady=3)
                tk.Label(r, text=icon[st][0], bg=CARD, fg=icon[st][1], width=2,
                         font=("DejaVu Sans", 11, "bold")).pack(side="left")
                tk.Label(r, text=text, bg=CARD, fg=FG, justify="left", wraplength=660, anchor="w").pack(side="left")
            state.update(items=items, info=info)
            if info["swap"] == "dphys":
                lw.pack(anchor="w", padx=18)
            if info["nonempty"] or info["unformatted"]:
                erase.set(bool(info["unformatted"]))
                er.pack(anchor="w", padx=18)
            prog.pack(fill="x", padx=18, pady=(10, 2))
            stat.pack(fill="x", padx=18)
            stat.configure(text="Close other programs before starting. Copying takes a few minutes per GB "
                                "on USB drives, less on NVMe.", fg=MUTED)
            refresh_go()

        def refresh_go(*_):
            items, info = state.get("items", []), state.get("info", {})
            ok = items and not any(st == "bad" for st, _ in items) and \
                (not info.get("nonempty") or erase.get())
            go.state(["!disabled"] if ok else ["disabled"])
        erase.trace_add("write", refresh_go)

        def checks():
            items, info = self._migration_checks(a)
            self.ui(lambda: show(items, info))
        threading.Thread(target=checks, daemon=True).start()

        def setp(pct, text, col=MUTED):
            def f():
                if win.winfo_exists():
                    if pct is not None:
                        prog["value"] = pct
                    stat.configure(text=text, fg=col)
            self.ui(f)

        def start():
            info = state["info"]
            msg = (f"Copy the whole system onto {a['alias']} and start from it after a reboot?\n\n"
                   + ("EVERYTHING on the array will be erased first.\n\n" if erase.get() else "")
                   + "The SD card is not erased: it keeps the boot files and a fallback copy of the system.")
            if not messagebox.askyesno(APP, msg, parent=win):
                return
            go.state(["disabled"])
            er.state(["disabled"])
            lw.state(["disabled"])

            def job():
                ok = self._do_migrate(a, info, erase.get(), setp, lowwear.get())
                if ok:
                    self.ui(lambda: self._offer_reboot_msg(
                        "The system is copied and the Pi is set to run from RAID.\n\nReboot now?", parent=win))
            self.work("moving system to RAID", job)
        go.configure(command=start)

    def _offer_reboot_msg(self, text, parent=None):
        if messagebox.askyesno(APP, text, parent=parent or self):
            self.sys.run(["systemctl", "reboot"], root=True)

    def _do_migrate(self, a, info, erase, setp, lowwear=False):
        S, md = self.sys, info["md"]

        def fail(text):
            self.log(text, "bad")
            setp(None, "✗ " + text + "  Nothing on the SD card was changed; it still starts normally.", BAD)
            S.run(["umount", NEWROOT], root=True, quiet=True)
            return False

        self.log(f"── Moving the system to {a['alias']} ──", "info")
        if info["need_tools"]:
            setp(2, "Installing " + ", ".join(info["need_tools"]) + "…")
            rc, _ = S.run(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                           "--no-install-recommends"] + info["need_tools"], root=True, timeout=1200)
            if rc != 0:
                return fail("Couldn't install the tools it needs.")

        setp(5, "Getting the array ready…")
        if a.get("mount"):
            if S.run(["umount", a["mount"]], root=True)[0] != 0 and not DEMO:
                return fail(f"Couldn't unmount {a['mount']} (is a program using it?).")
        S.run(["mkdir", "-p", NEWROOT], root=True, quiet=True)
        old_uuid = info["fs_uuid"]                 # its data-mount line must go too, even after a reformat
        if erase or info["unformatted"]:
            fstype = info["fstype"] if info["fstype"] in ("ext4", "xfs") else "ext4"
            label = a["alias"].split("/")[-1][:12]
            mk = ["mkfs.ext4", "-F", "-L", label, md] if fstype == "ext4" else ["mkfs.xfs", "-f", "-L", label, md]
            setp(7, "Erasing and formatting the array…")
            if S.run(mk, root=True, timeout=3600)[0] != 0:
                return fail("Formatting the array failed.")
            S.run(["udevadm", "settle"], root=True, quiet=True)
            rc, out = S.run(["blkid", "-s", "UUID", "-o", "value", md], root=True, quiet=True)
            info["fstype"], info["fs_uuid"] = fstype, out.strip() or info["fs_uuid"]
        if not info["fs_uuid"]:
            return fail("Couldn't read the array's filesystem ID.")
        if S.run(["mount", md, NEWROOT], root=True)[0] != 0:
            return fail("Couldn't mount the array.")

        # make sure the array is listed in mdadm.conf (it goes into the startup files)
        rc, out = S.run(["mdadm", "--detail", "--brief", md], root=True, quiet=True)
        line = next((l for l in out.splitlines() if l.startswith("ARRAY")), "")
        um = re.search(r"UUID=(\S+)", line)
        if um and um.group(1) not in S.read_text(MDADM_CONF):
            S.run(["mkdir", "-p", os.path.dirname(MDADM_CONF)], root=True, quiet=True)
            S.write_root_file(MDADM_CONF, line + "\n", append=True)

        if lowwear and info.get("swap") == "dphys":
            setp(9, "Low-wear: moving swap into RAM (zram)…")
            rc, _ = S.run(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                           "--no-install-recommends", "zram-tools"], root=True, timeout=1200)
            if rc == 0 or DEMO:
                S.write_root_file("/etc/default/zramswap", ZRAM_CONF)
                S.run(["systemctl", "enable", "zramswap"], root=True)
                # keep today's swap running until the reboot; just stop it coming back
                S.run(["systemctl", "disable", "dphys-swapfile"], root=True)
                self.log("✓ Low-wear: swap moves into RAM (zram) from the next boot.", "good")
            else:
                self.log("Couldn't install zram-tools. Carrying on with the swap file (wear is still fine).", "warn")

        setp(10, "Copying the system… (this is the long part)")
        if DEMO:
            import time
            for pct in range(0, 101, 5):
                setp(10 + pct * 0.75, f"Copying the system… {pct}%   (demo)")
                time.sleep(0.12)
            rc = 0
        else:
            args = ["rsync", "-aAXHx", "--numeric-ids", "--info=progress2", "--no-inc-recursive"]
            for ex in RSYNC_EXCLUDES:
                args += ["--exclude", ex]
            args += ["/", NEWROOT + "/"]
            last = [0]

            def on_text(t):
                m = re.search(r"(\d+)%.*?([\d.]+[kMG]B/s)?\s+(\d+:\d+:\d+)", t)
                if m and int(m.group(1)) != last[0]:
                    last[0] = int(m.group(1))
                    setp(10 + last[0] * 0.75, f"Copying the system… {last[0]}%"
                                              f"{'   ' + m.group(2) if m.group(2) else ''}   {m.group(3)} left")
            rc = S.run_stream(args, root=True, on_text=on_text)
        if rc not in (0, 24):                       # 24 = some files vanished while copying (normal on a live system)
            return fail(f"Copying stopped with an error (rsync code {rc}).")

        setp(86, "Setting up the copy to start on its own…")
        for d in ("dev", "proc", "sys", "run", "tmp", "mnt", "media", os.path.relpath(BOOT_DIR, "/")):
            S.run(["mkdir", "-p", os.path.join(NEWROOT, d)], root=True, quiet=True)
        S.run(["chmod", "1777", os.path.join(NEWROOT, "tmp")], root=True, quiet=True)
        fstab_new = edit_fstab(S.read_text(os.path.join(NEWROOT, "etc/fstab"),
                                           S.read_text("/etc/fstab")) if not DEMO else
                               "PARTUUID=abcd-01  /boot/firmware  vfat  defaults  0  2\n"
                               "PARTUUID=abcd-02  /  ext4  defaults,noatime  0  1\n",
                               info["fs_uuid"], info["fstype"], old_uuids=(old_uuid,))
        if S.write_root_file(os.path.join(NEWROOT, "etc/fstab"), fstab_new) != 0:
            return fail("Couldn't write the new system's fstab.")
        S.run(["cp", MDADM_CONF, os.path.join(NEWROOT, MDADM_CONF.lstrip("/"))], root=True, quiet=True)
        if old_uuid and old_uuid != info["fs_uuid"] and not DEMO:   # reformatted: the SD copy's old line is dead
            live = S.read_text("/etc/fstab")
            kept = "".join(l for l in live.splitlines(True) if f"UUID={old_uuid}" not in l.split("#")[0])
            if kept != live:
                S.write_root_file("/etc/fstab", kept)

        if info["need_initramfs"]:
            S.run(["cp", CONFIG_TXT, CONFIG_TXT + ".raidforge.bak"], root=True, quiet=True)
            S.write_root_file(CONFIG_TXT, ensure_initramfs(S.read_text(CONFIG_TXT)))

        # drivers the startup files must carry to reach the drives and assemble the array
        mods_path = "/etc/initramfs-tools/modules"
        have = S.read_text(mods_path).split()
        want = []
        for m in INITRAMFS_MODULES:
            if m in have:
                continue
            rc, out = S.run(["modinfo", "-F", "filename", m], quiet=True)
            if DEMO or (rc == 0 and ".ko" in out):     # built-in or unknown on this kernel: nothing to add
                want.append(m)
        if want:
            S.write_root_file(mods_path, "# Added by RAID Forge (system on RAID)\n" + "\n".join(want) + "\n",
                              append=True)

        setp(90, "Rebuilding the startup files with RAID support (a minute or two)…")
        if S.run(["update-initramfs", "-u", "-k", "all"], root=True, timeout=1800)[0] != 0 and not DEMO:
            return fail("Rebuilding the startup files (initramfs) failed.")
        if not DEMO:
            if not any(f.startswith("initramfs") for f in os.listdir(BOOT_DIR)):
                return fail(f"No initramfs file in {BOOT_DIR}, so the Pi couldn't find the RAID. Stopped.")
            kv = os.uname().release
            img = f"/boot/initrd.img-{kv}"
            if shutil.which("lsinitramfs") and os.path.exists(img):
                rc, out = S.run(["lsinitramfs", img], root=True, quiet=True, timeout=300)
                if "mdadm" not in out:
                    return fail("The startup files don't contain mdadm, so the Pi couldn't find the RAID. Stopped.")
        # copy the fresh startup files' source config into the new root too
        S.run(["rsync", "-aAX", "/etc/initramfs-tools/", os.path.join(NEWROOT, "etc/initramfs-tools/")],
              root=True, quiet=True)

        setp(96, "Pointing the Pi at the RAID…")
        if DEMO or not os.path.exists(CMDLINE_SD):
            S.run(["cp", CMDLINE, CMDLINE_SD], root=True)
        old = S.read_text(CMDLINE, "console=serial0,115200 console=tty1 root=PARTUUID=abcd-02 rootfstype=ext4 fsck.repair=yes rootwait")
        if S.write_root_file(CMDLINE, edit_cmdline(old, info["fs_uuid"], info["fstype"])) != 0:
            return fail("Couldn't change cmdline.txt.")
        note = (f"RAID Forge moved this Pi's system to {a['alias']} (filesystem UUID {info['fs_uuid']}).\n"
                f"cmdline.txt now starts the system from the RAID.\n\n"
                f"TO GO BACK TO THE SD CARD: open RAID Forge > My Arrays > 'Boot from SD again…'\n"
                f"or, if the Pi won't start, put this SD card in another computer and copy\n"
                f"cmdline.txt.raidforge-sd over cmdline.txt on the boot partition.\n")
        S.write_root_file(os.path.join(BOOT_DIR, "RAID-FORGE-README.txt"), note)
        S.run(["sync"], root=True, quiet=True)
        S.run(["umount", NEWROOT], root=True, quiet=True)
        setp(100, "✓ Done. Reboot and the Pi runs from the RAID. The SD card still starts it.", GOOD)
        self.log(f"✓ System copied to {a['alias']}. Reboot to start from it.", "good")
        return True

    def undo_migration(self):
        if not DEMO and not os.path.exists(CMDLINE_SD):
            return messagebox.showinfo(APP, "The system isn't set to run from RAID, so there's nothing to undo.")
        if not messagebox.askyesno(APP, "Go back to running the system from the SD card?\n\n"
                                        "After the reboot the Pi uses the copy on the SD card. Anything changed "
                                        "since the move (new files, installs) is only on the RAID; it stays there "
                                        "and you can mount the array to get it."):
            return

        def job():
            if self.sys.run(["cp", CMDLINE_SD, CMDLINE], root=True)[0] == 0:
                self.sys.run(["rm", "-f", CMDLINE_SD, os.path.join(BOOT_DIR, "RAID-FORGE-README.txt")],
                             root=True, quiet=True)
                self.log("✓ cmdline.txt restored. The system will start from the SD card after a reboot.", "good")
                self.ui(lambda: self._offer_reboot_msg("Set to start from the SD card. Reboot now?"))
        self.work("switching back to the SD card", job)

    # ============================================================ TAB 3: health
    def _health_tab(self):
        root = self.tab_health
        top = ttk.Frame(root, style="Bg.TFrame")
        top.pack(fill="x", pady=(10, 6))
        ttk.Button(top, text="Check all drives (SMART)", command=self.check_health).pack(side="left")
        ttk.Button(top, text="Blink selected drive", command=self.identify_health_row).pack(side="left", padx=(6, 0))
        ttk.Label(top, text="Health, temperature and SSD wear. Some USB enclosures don't pass this through.",
                  foreground=MUTED).pack(side="left", padx=10)

        # TRIM panel (packed at the bottom first so the table takes the rest)
        tp = ttk.Frame(root, style="Panel.TFrame", padding=12)
        tp.pack(side="bottom", fill="x", pady=(8, 0))
        hdr = ttk.Frame(tp, style="Panel.TFrame")
        hdr.pack(fill="x")
        ttk.Label(hdr, text="TRIM", style="H.Panel.TLabel").pack(side="left")
        ttk.Label(hdr, text="  tells SSDs which blocks are free, so they stay fast and wear evenly",
                  style="Muted.Panel.TLabel").pack(side="left")
        ttk.Button(hdr, text="TRIM now", command=self.trim_now).pack(side="right")
        ttk.Button(hdr, text="Apply", command=self.apply_trim).pack(side="right", padx=6)
        self.trim_sched = tk.StringVar(value="Weekly (recommended)")
        ttk.Combobox(hdr, textvariable=self.trim_sched, state="readonly", width=20,
                     values=["Weekly (recommended)", "Daily", "Off"]).pack(side="right")
        ttk.Label(hdr, text="Run automatically:", style="Muted.Panel.TLabel").pack(side="right", padx=6)
        self.trim_info = tk.Label(tp, text="Checking…", bg=PANEL, fg=MUTED, justify="left", anchor="w",
                                  wraplength=1100, font=("DejaVu Sans", 9))
        self.trim_info.pack(fill="x", pady=(8, 0))

        self.wear_msg = tk.Label(root, text="", bg=BG, fg=WARN, justify="left", anchor="w", wraplength=1100)
        self.wear_msg.pack(side="bottom", fill="x", pady=(6, 0))
        cols = ("drive", "model", "health", "wear", "temp", "hours", "notes")
        self.htree = ttk.Treeview(root, columns=cols, show="headings", selectmode="browse")
        for c, t, w in zip(cols, ("Drive", "Model", "Health", "SSD wear", "Temp", "Power-on hours", "Notes"),
                           (110, 220, 90, 150, 70, 110, 330)):
            self.htree.heading(c, text=t)
            self.htree.column(c, width=w, anchor="w")
        self.htree.tag_configure("good", foreground=GOOD)
        self.htree.tag_configure("bad", foreground=BAD)
        self.htree.tag_configure("warn", foreground=WARN)
        self.htree.pack(fill="both", expand=True)
        self.after(600, self.refresh_trim)

    @staticmethod
    def wear_text(w):
        if w is None:
            return "—", None
        bar = "█" * min(10, round(w / 10)) + "░" * (10 - min(10, round(w / 10)))
        return f"{bar} {w}%", ("bad" if w >= 80 else "warn" if w >= 60 else None)

    def check_health(self):
        def job():
            rows, wear = [], {}
            for d in self.sys.disks():
                if d["tran"] == "mmc":
                    continue
                if DEMO:
                    w = {"sda": 81, "sdb": 79, "sdc": 12, "sdd": 3, "sde": None}.get(d["name"])
                    wear[d["name"]] = w
                    wt, wtag = self.wear_text(w)
                    note = ("replace soon: over 80% of rated writes used" if wtag == "bad" else
                            "past 60%: plan a replacement" if wtag == "warn" else "")
                    rows.append((d["path"], d["model"], "PASSED", wt, "34 °C", "8,812", note, wtag or "good"))
                    continue
                args = ["smartctl", "-H", "-A", "-l", "devstat", "-j", d["path"]]
                if d["tran"] == "usb":
                    args[1:1] = ["-d", "sat"]
                rc, out = self.sys.run(args, root=True, quiet=True)
                try:
                    j = json.loads(out)
                except ValueError:
                    rows.append((d["path"], d["model"], "unknown", "—", "—", "—", "smartctl not available", "warn"))
                    continue
                passed = j.get("smart_status", {}).get("passed")
                temp = j.get("temperature", {}).get("current")
                hrs = j.get("power_on_time", {}).get("hours")
                w = None if d["rota"] else wear_from_smart(j)
                wear[d["name"]] = w
                wt, wtag = self.wear_text(w)
                notes = []
                if wtag == "bad":
                    notes.append("replace soon: over 80% of rated writes used")
                elif wtag == "warn":
                    notes.append("past 60%: plan a replacement")
                for attr in j.get("ata_smart_attributes", {}).get("table", []):
                    if attr.get("id") in (5, 197, 198) and attr.get("raw", {}).get("value", 0):
                        notes.append(f"{attr['name']}={attr['raw']['value']}")
                nv = j.get("nvme_smart_health_information_log", {})
                if nv.get("media_errors"):
                    notes.append(f"media errors {nv['media_errors']}")
                if nv.get("available_spare") is not None and nv.get("available_spare_threshold") is not None \
                        and nv["available_spare"] <= nv["available_spare_threshold"]:
                    notes.append("spare blocks used up")
                health = "PASSED" if passed else ("FAILING" if passed is False else "unknown")
                tag = "bad" if passed is False or wtag == "bad" else \
                    ("warn" if wtag == "warn" or any("=" in n or "media" in n for n in notes) else "good")
                rows.append((d["path"], d["model"], health, wt, f"{temp} °C" if temp else "—",
                             f"{hrs:,}" if hrs else "—", ", ".join(notes), tag))
            lock = lockstep_warnings(self._read_arrays(), wear)

            def show():
                self.htree.delete(*self.htree.get_children())
                for r in rows:
                    self.htree.insert("", "end", values=r[:7], tags=(r[7],))
                self.wear_msg.configure(text="\n".join("⚠ " + m for m in lock))
            self.ui(show)
            self.log(f"Health check finished for {len(rows)} drive(s)."
                     + (f" {len(lock)} mirror pair(s) wearing in step." if lock else ""), "warn" if lock else "good")
        self.work("checking drive health", job)

    # ---- TRIM
    def _trim_state(self):
        """Returns (schedule, lines). schedule: Weekly/Daily/Off/Unavailable."""
        S, lines = self.sys, []
        if DEMO:
            sched = getattr(self, "_demo_trim", "Weekly (recommended)")
            first = ("warn", "Automatic TRIM is off. SSDs slowly lose speed and wear less evenly without it") \
                if sched == "Off" else ("ok", f"Automatic TRIM is on: every {'day' if sched == 'Daily' else 'week'} "
                                              "(fstrim.timer)")
            return sched, [first,
                ("ok", "nvme0n1, nvme1n1: TRIM supported"),
                ("warn", "sde (USB): this enclosure doesn't pass TRIM through, so the SSD can't be told which blocks are free"),
                ("warn", "md127 (RAID 5): Linux blocks TRIM on RAID 5/6 by default for safety. Wear is still fine"),
                ("ok", "Other arrays pass TRIM through to their SSDs")]
        rc, en = S.run(["systemctl", "is-enabled", "fstrim.timer"], quiet=True)
        en = en.strip()
        daily = "OnCalendar=daily" in S.read_text(TRIM_OVERRIDE)
        if rc != 0 and en not in ("disabled", "masked"):
            sched = "Unavailable"
            lines.append(("bad", "fstrim.timer isn't installed (util-linux). TRIM now still works if fstrim exists"))
        elif en == "enabled":
            sched = "Daily" if daily else "Weekly (recommended)"
            lines.append(("ok", f"Automatic TRIM is on: every {'day' if daily else 'week'} (fstrim.timer)"))
        else:
            sched = "Off"
            lines.append(("warn", "Automatic TRIM is off. SSDs slowly lose speed and wear less evenly without it"))
        rc, out = S.run(["lsblk", "-J", "-b", "-o", "NAME,TYPE,TRAN,ROTA,DISC-MAX"], quiet=True)
        try:
            devs = json.loads(out)["blockdevices"]
        except (ValueError, KeyError):
            devs = []
        ok_ssd = []
        for d in devs:
            if d.get("type") != "disk" or d.get("rota") or re.match(r"(loop|zram|ram|mmcblk)", d["name"]):
                continue
            if int(d.get("disc-max") or 0) > 0:
                ok_ssd.append(d["name"])
            elif d.get("tran") == "usb":
                lines.append(("warn", f"{d['name']} (USB): this enclosure doesn't pass TRIM through, so the SSD "
                                      "can't be told which blocks are free. A UASP enclosure that supports TRIM fixes it"))
            else:
                lines.append(("warn", f"{d['name']}: doesn't report TRIM support"))
        if ok_ssd:
            lines.append(("ok", f"{', '.join(ok_ssd)}: TRIM supported"))
        for a in self._read_arrays():
            mx = S.read_text(f"/sys/block/{a['name']}/queue/discard_max_bytes", "0").strip()
            if a["level"] in ("raid5", "raid6") and mx in ("0", ""):
                lines.append(("warn", f"{a['name']} ({a['level'].upper().replace('RAID', 'RAID ')}): Linux blocks TRIM "
                                      "on RAID 5/6 by default for safety. Wear is still fine"))
            elif mx not in ("0", ""):
                lines.append(("ok", f"{a['name']} ({a['level']}): passes TRIM through to its drives"))
        return sched, lines

    def refresh_trim(self):
        def job():
            sched, lines = self._trim_state()

            def show():
                if sched != "Unavailable":
                    self.trim_sched.set(sched)
                icon = {"ok": "✓", "warn": "!", "bad": "✗"}
                self.trim_info.configure(text="\n".join(f"{icon[s]}  {t}" for s, t in lines),
                                         fg=WARN if any(s != "ok" for s, _ in lines) else MUTED)
            self.ui(show)
        threading.Thread(target=job, daemon=True).start()

    def apply_trim(self):
        choice = self.trim_sched.get()
        S = self.sys

        def job():
            if DEMO:
                self._demo_trim = choice
            if choice == "Daily":
                S.run(["mkdir", "-p", os.path.dirname(TRIM_OVERRIDE)], root=True, quiet=True)
                S.write_root_file(TRIM_OVERRIDE, TRIM_DAILY)
            else:
                S.run(["rm", "-f", TRIM_OVERRIDE], root=True, quiet=True)
            S.run(["systemctl", "daemon-reload"], root=True, quiet=True)
            if choice == "Off":
                rc, _ = S.run(["systemctl", "disable", "--now", "fstrim.timer"], root=True)
            else:
                rc, _ = S.run(["systemctl", "enable", "--now", "fstrim.timer"], root=True)
                S.run(["systemctl", "restart", "fstrim.timer"], root=True, quiet=True)
            self.log(f"✓ Automatic TRIM: {choice.split(' ')[0].lower()}." if rc == 0 else
                     "Couldn't change the TRIM schedule.", "good" if rc == 0 else "bad")
            self.ui(self.refresh_trim)
        self.work("setting TRIM schedule", job)

    def trim_now(self):
        def job():
            self.log("TRIM on every mounted SSD that supports it…", "info")
            if DEMO:
                self.log("  /mnt/vault: 212.4 GiB (228051234816 bytes) trimmed (demo)", "muted")
                self.log("  /: 18.1 GiB (19434569728 bytes) trimmed (demo)", "muted")
                rc = 0
            else:
                rc = self.sys.run_stream(["fstrim", "-av"], root=True,
                                         on_text=lambda t: self.log("  " + t, "muted"))
            self.log("✓ TRIM finished." if rc == 0 else "TRIM finished with warnings (see above).",
                     "good" if rc == 0 else "warn")
        self.work("running TRIM", job)

    # ============================================================ refresh
    def refresh_all(self):
        def job():
            facts = dict(board=self.sys.board(), root=self.sys.root_source(), tools=self.sys.has("mdadm"),
                         pcie=dict(config=self.sys.pcie_config(), devices=self.sys.pcie_devices()))
            disks = self.sys.disks()
            arrays = self._read_arrays()
            self.ui(lambda: (self.render_checks(facts), self.render_disks(disks), self.render_arrays(arrays)))
        threading.Thread(target=job, daemon=True).start()

    def _read_arrays(self):
        arrays = System.parse_mdstat(self.sys.mdstat())
        for a in arrays:
            a["alias"] = self.sys.md_alias(a["name"])
            a["mount"] = self.sys.mountpoint_of(f"/dev/{a['name']}")
            a["space"] = self.sys.space(a["mount"])
        return arrays

    def _auto_refresh(self):
        def job():
            arrays = self._read_arrays()
            self.ui(lambda: self.render_arrays(arrays))
        if not self.busy:
            threading.Thread(target=job, daemon=True).start()
        self.after(3000, self._auto_refresh)


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(f"{APP} {VERSION}")
        sys.exit(0)
    # Opened from a menu/desktop icon with no password-free sudo: ask for the password ONCE by
    # restarting as admin, instead of a password box for every disk command. Cancel = carry on as-is.
    if (not DEMO and os.geteuid() != 0 and "--elevated" not in sys.argv and shutil.which("pkexec")
            and subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0):
        env = [f"{k}={os.environ[k]}" for k in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
               if os.environ.get(k)]
        if os.environ.get("DISPLAY") and not os.environ.get("XAUTHORITY"):
            env.append(f"XAUTHORITY={os.path.expanduser('~/.Xauthority')}")
        rc = subprocess.call(["pkexec", "env", *env, f"SUDO_USER={USER}", sys.executable,
                              os.path.abspath(__file__), "--elevated", *sys.argv[1:]])
        if rc == 0:                       # ran as admin and was closed normally
            sys.exit(0)
        # cancelled (126/127) or the admin copy couldn't open a window: run normally instead
    App().mainloop()
