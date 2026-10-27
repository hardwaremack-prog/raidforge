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
VERSION = "1.0"
DEMO = "--demo" in sys.argv or os.environ.get("RAIDFORGE_DEMO") == "1"
USER = os.environ.get("SUDO_USER") or getpass.getuser()

CONFIG_TXT = next((p for p in ("/boot/firmware/config.txt", "/boot/config.txt")
                   if os.path.exists(p)), "/boot/firmware/config.txt")
MDADM_CONF = next((p for p in ("/etc/mdadm/mdadm.conf", "/etc/mdadm.conf")
                   if os.path.exists(p)), "/etc/mdadm/mdadm.conf")

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
    ("Quad NVMe HAT (PCIe switch)", "Up to 4 NVMe drives behind a PCIe switch. Use PCIe Gen 2."),
    ("USB 3 drives / USB HAT / dock", "Works on Pi 5, Pi 500 and Pi 400 (blue USB 3 ports). Use a powered hub or dock for 2.5\"/3.5\" drives."),
]


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

    def mountpoint_of(self, dev):
        if DEMO:
            return "/mnt/vault"
        rc, out = self.run(["findmnt", "-no", "TARGET", "-S", dev], quiet=True)
        return out.strip().splitlines()[0] if out.strip() else ""


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
        self.chk_labels["boot"].configure(
            text=f"{'SD card ✓' if on_sd else 'Not the SD card'}\n{src}", fg=GOOD if on_sd else WARN)
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
                opts = "defaults,nofail,x-systemd.device-timeout=30s"
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
        cols = ("dev", "level", "size", "health", "state", "mount", "progress")
        self.atree = ttk.Treeview(root, columns=cols, show="headings", height=6, selectmode="browse")
        for c, t, w in zip(cols, ("Array", "Level", "Size", "Drives", "State", "Mounted at", "Activity"),
                           (180, 70, 100, 110, 140, 160, 260)):
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
                      ("Add hot spare…", self.add_spare), ("Stop", self.stop_array)):
            ttk.Button(acts, text=t, command=fn).pack(side="left", padx=(0, 6))
        ttk.Button(acts, text="Delete array…", style="Danger.TButton", command=self.delete_array).pack(side="right")

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
            tag = "bad" if degraded or a["state"] != "active" else ("warn" if a["op"] else "good")
            self.atree.insert("", "end", iid=a["name"], tags=(tag,),
                              values=(f"{a['alias']}  ({a['name']})", a["level"].upper(),
                                      human(a["size"]) if a["size"] else "—", drives, state,
                                      a["mount"] or "not mounted", act or "idle ✓"))
        if not arrays:
            self.atree.insert("", "end", iid="_none", values=("No arrays yet — build one on tab ①", "", "", "", "", "", ""))
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

    def stop_array(self):
        a = self.current_array()
        if a and messagebox.askyesno(APP, f"Unmount and stop {a['alias']}?\nData is kept — "
                                          "use 'Find & start existing arrays' to bring it back."):
            def job():
                if a["mount"]:
                    self.sys.run(["umount", a["mount"]], root=True)
                self.sys.run(["mdadm", "--stop", f"/dev/{a['name']}"], root=True)
            self.work("stopping", job)

    def delete_array(self):
        a = self.current_array()
        if not a:
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

    # ============================================================ TAB 3: health
    def _health_tab(self):
        root = self.tab_health
        top = ttk.Frame(root, style="Bg.TFrame")
        top.pack(fill="x", pady=(10, 6))
        ttk.Button(top, text="Check all drives (SMART)", command=self.check_health).pack(side="left")
        ttk.Label(top, text="Reads each drive's self-monitoring data. USB enclosures may not pass it through.",
                  foreground=MUTED).pack(side="left", padx=10)
        cols = ("drive", "model", "health", "temp", "hours", "notes")
        self.htree = ttk.Treeview(root, columns=cols, show="headings", selectmode="none")
        for c, t, w in zip(cols, ("Drive", "Model", "Health", "Temp", "Power-on hours", "Notes"),
                           (120, 240, 110, 80, 130, 340)):
            self.htree.heading(c, text=t)
            self.htree.column(c, width=w, anchor="w")
        self.htree.tag_configure("good", foreground=GOOD)
        self.htree.tag_configure("bad", foreground=BAD)
        self.htree.tag_configure("warn", foreground=WARN)
        self.htree.pack(fill="both", expand=True)

    def check_health(self):
        def job():
            rows = []
            for d in self.sys.disks():
                if d["tran"] == "mmc":
                    continue
                if DEMO:
                    rows.append((d["path"], d["model"], "PASSED", "34 °C", "8,812", "", "good"))
                    continue
                args = ["smartctl", "-H", "-A", "-j", d["path"]]
                if d["tran"] == "usb":
                    args[1:1] = ["-d", "sat"]
                rc, out = self.sys.run(args, root=True, quiet=True)
                try:
                    j = json.loads(out)
                except ValueError:
                    rows.append((d["path"], d["model"], "unknown", "—", "—", "smartctl not available", "warn"))
                    continue
                passed = j.get("smart_status", {}).get("passed")
                temp = j.get("temperature", {}).get("current")
                hrs = j.get("power_on_time", {}).get("hours")
                notes = []
                for attr in j.get("ata_smart_attributes", {}).get("table", []):
                    if attr.get("id") in (5, 197, 198) and attr.get("raw", {}).get("value", 0):
                        notes.append(f"{attr['name']}={attr['raw']['value']}")
                nv = j.get("nvme_smart_health_information_log", {})
                if nv.get("percentage_used") is not None:
                    notes.append(f"wear {nv['percentage_used']}%")
                if nv.get("media_errors"):
                    notes.append(f"media errors {nv['media_errors']}")
                health = "PASSED" if passed else ("FAILING" if passed is False else "unknown")
                tag = "bad" if passed is False else ("warn" if any("=" in n for n in notes) else "good")
                rows.append((d["path"], d["model"], health, f"{temp} °C" if temp else "—",
                             f"{hrs:,}" if hrs else "—", ", ".join(notes), tag))

            def show():
                self.htree.delete(*self.htree.get_children())
                for r in rows:
                    self.htree.insert("", "end", values=r[:6], tags=(r[6],))
            self.ui(show)
            self.log(f"Health check finished for {len(rows)} drive(s).", "good")
        self.work("checking drive health", job)

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
