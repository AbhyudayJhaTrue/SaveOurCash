#!/usr/bin/env python3
"""
SaveOurCash - see what your laptop costs to run in Singapore, and cut it down.

Requirements:   pip install psutil matplotlib
Optional:       pip install screen-brightness-control   (brightness control on more laptops)
Best on Windows 10/11. Run as Administrator if you want Wi-Fi switched off fully.
"""
import ctypes
import hmac
import json
import os
import platform
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
import tkinter as tk
from datetime import datetime
from tkinter import messagebox

import matplotlib

matplotlib.use("TkAgg")
import matplotlib.dates as mdates
import psutil
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

APP_NAME = "SaveOurCash"
IS_WIN = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

# ---- Settings ---------------------------------------------------------------
SG_TARIFF_CENTS = 31.16      # SP Group regulated tariff, Oct-Dec 2026, incl. 9% GST (28.59c before GST)
SAMPLE_SECONDS = 60          # how often RAM / battery / CPU are logged
HISTORY_HOURS = 48
GAP_LIMIT = 600              # ignore gaps longer than 10 min (app was closed / laptop asleep)
IMPORT_GAP = 4 * 3600        # Windows' own battery log is sparse, so allow longer gaps between its points
IDLE_LIMIT = 300             # seconds without input before idle power-saving kicks in
DATA_DIR = os.path.join(os.path.expanduser("~"), ".saveourcash")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")

# ---- Login (temporary hard-coded credentials) -------------------------------
VALID_USER = "Abhyuday"
VALID_PASS = "12345"
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 30

# ---- Palette ----------------------------------------------------------------
INK = "#12372A"        # deep forest (sidebar)
INK_HOVER = "#1C4D3B"
INK_ACTIVE = "#256148"
PAPER = "#EEF4EF"      # window background
CARD = "#FFFFFF"
TEXT = "#1B2A25"
MUTED = "#6B7F76"
COIN = "#E9B44C"       # accent / call to action
COIN_HOVER = "#F2C56B"
TEAL = "#2A9D8F"
LINE = "#DCE6DF"

F_BODY = ("Segoe UI", 10)
F_SMALL = ("Segoe UI", 9)
F_BTN = ("Segoe UI Semibold", 11)
F_TITLE = ("Georgia", 24, "bold")
F_BIG = ("Georgia", 22, "bold")


# =============================================================================
# System helpers
# =============================================================================
def run(cmd, timeout=20):
    kw = {"creationflags": 0x08000000} if IS_WIN else {}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def powershell(script):
    return run(["powershell", "-NoProfile", "-Command", script])


def get_brightness():
    try:
        import screen_brightness_control as sbc
        v = sbc.get_brightness()
        return int(v[0] if isinstance(v, list) else v)
    except Exception:
        pass
    if IS_WIN:
        try:
            r = powershell("(Get-CimInstance -Namespace root/WMI -ClassName "
                           "WmiMonitorBrightness).CurrentBrightness")
            return int(r.stdout.strip().splitlines()[0])
        except Exception:
            return None
    return None


def set_brightness(value):
    value = max(5, min(100, int(value)))
    try:
        import screen_brightness_control as sbc
        sbc.set_brightness(value)
        return True
    except Exception:
        pass
    if IS_WIN:
        try:
            r = powershell("Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
                           "| Invoke-CimMethod -MethodName WmiSetBrightness "
                           "-Arguments @{Timeout=1; Brightness=%d}" % value)
            return r.returncode == 0
        except Exception:
            return False
    return False


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds():
    """Seconds since last keyboard/mouse input, or None if unsupported."""
    try:
        if IS_WIN:
            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(info)
            ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
            return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0
        if IS_LINUX:
            return int(run(["xprintidle"]).stdout.strip()) / 1000.0
    except Exception:
        pass
    return None


def foreground_pid():
    if not IS_WIN:
        return None
    try:
        user32 = ctypes.windll.user32
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
        return pid.value
    except Exception:
        return None


def net_bytes():
    total = 0
    for name, c in psutil.net_io_counters(pernic=True).items():
        if name.lower().startswith("lo") or "loopback" in name.lower():
            continue
        total += c.bytes_sent + c.bytes_recv
    return total


def fmt_mb(b):
    return f"{b / 1024 / 1024:,.0f} MB"


# =============================================================================
# Windows battery report import (powercfg /batteryreport keeps about 3 days)
# =============================================================================
def _read_text(path):
    raw = open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="ignore")
    return raw.decode("utf-8-sig", errors="ignore")


def _parse_dt(text):
    text = re.sub(r"(\.\d{6})\d+", r"\1", text.strip().replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def parse_battery_xml(path):
    """Read (timestamp, percent, plugged) points from the XML report. Tolerant of naming differences."""
    root = ET.fromstring(_read_text(path).encode("utf-8"))
    name = lambda tag: tag.rsplit("}", 1)[-1].lower()
    default_full = None
    for el in root.iter():
        if name(el.tag) == "fullchargecapacity" and (el.text or "").strip().isdigit():
            default_full = int(el.text.strip())
            break
    out = []
    for el in root.iter():
        a = {name(k): v for k, v in el.attrib.items()}
        for child in el:
            if len(child) == 0 and child.text:
                a.setdefault(name(child.tag), child.text.strip())
        ts = a.get("localtimestamp") or a.get("timestamp")
        cap = a.get("chargecapacity")
        if not ts or not cap:
            continue
        dt = _parse_dt(ts)
        try:
            cap = float(re.sub(r"[^\d.]", "", cap))
            full = float(re.sub(r"[^\d.]", "", a.get("fullchargecapacity") or "") or default_full or 0)
        except ValueError:
            continue
        if dt is None or full <= 0:
            continue
        ac = (a.get("ac") or a.get("acstatus") or "").lower()
        plugged = True if ac in ("1", "true", "yes") else False if ac in ("0", "false", "no") else None
        out.append((dt.timestamp(), min(100.0, cap / full * 100), plugged))
    return out


def parse_battery_html(path):
    """Fallback: read the 'Recent usage' table of the HTML report (English Windows)."""
    text = _read_text(path)
    out = []
    for tr in re.findall(r"<tr.*?>(.*?)</tr>", text, re.S | re.I):
        cells = [re.sub(r"<.*?>", "", c).replace("&nbsp;", " ").strip()
                 for c in re.findall(r"<t[dh].*?>(.*?)</t[dh]>", tr, re.S | re.I)]
        if len(cells) < 4:
            continue
        dt = _parse_dt(cells[0])
        source = [c for c in cells if c in ("AC", "Battery")]
        pct = [c for c in cells if re.fullmatch(r"\d+\s*%", c)]
        if dt and source and pct:
            out.append((dt.timestamp(), float(pct[0].rstrip("% ").strip()), source[0] == "AC"))
    return out


def import_windows_battery():
    """Returns (points, message). Points are (timestamp, percent, plugged)."""
    if not IS_WIN:
        return [], "Battery import only works on Windows."
    os.makedirs(DATA_DIR, exist_ok=True)
    problem = "Windows returned no battery history."
    for use_xml in (True, False):
        path = os.path.join(DATA_DIR, "battery_report." + ("xml" if use_xml else "html"))
        cmd = ["powercfg", "/batteryreport", "/duration", "3", "/output", path]
        if use_xml:
            cmd.insert(2, "/xml")
        try:
            r = run(cmd, timeout=90)
            if r.returncode != 0 or not os.path.exists(path):
                problem = (r.stdout + r.stderr).strip() or problem
                continue
            points = (parse_battery_xml if use_xml else parse_battery_html)(path)
        except Exception as exc:
            problem = str(exc)
            continue
        if points:
            return points, f"Imported {len(points)} battery points from Windows."
        problem = f"The report was created but no battery entries could be read from it ({path})."
    return [], problem


# =============================================================================
# History logger (RAM, battery, CPU every minute, kept for 48 hours)
# =============================================================================
class History:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = []  # [timestamp, ram %, battery % or None, plugged or None, cpu %]
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                self.rows = json.load(f)
        except Exception:
            self.rows = []
        self._prune()

    def _prune(self):
        cutoff = time.time() - HISTORY_HOURS * 3600
        self.rows = [r for r in self.rows if r[0] >= cutoff]

    def add(self):
        batt = psutil.sensors_battery()
        row = [time.time(), psutil.virtual_memory().percent,
               None if batt is None else batt.percent,
               None if batt is None else bool(batt.power_plugged),
               psutil.cpu_percent(interval=None)]
        with self.lock:
            self.rows.append(row)
            self._prune()

    def merge_import(self, imported):
        """Add Windows battery-report points that are older than anything this app logged itself.
        Imported rows carry a 6th field (1) and have no RAM/CPU values."""
        cutoff = time.time() - HISTORY_HOURS * 3600
        with self.lock:
            real = [r for r in self.rows if len(r) < 6 or not r[5]]
            first_real = min((r[0] for r in real), default=float("inf"))
            new = [[ts, None, pct, plugged, None, 1] for ts, pct, plugged in imported
                   if cutoff <= ts < first_real]
            self.rows = sorted(new + real, key=lambda r: r[0])
        self.save()
        return len(new)

    def snapshot(self):
        with self.lock:
            self._prune()
            return list(self.rows)

    def save(self):
        try:
            with self.lock:
                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.rows, f)
        except Exception:
            pass


def estimate_energy(rows, idle_w, max_w):
    """Estimate energy from CPU load: power = idle + (max - idle) * cpu%."""
    wh, seconds = 0.0, 0.0
    for a, b in zip(rows, rows[1:]):
        dt = b[0] - a[0]
        if dt <= 0 or dt > GAP_LIMIT or a[4] is None or b[4] is None:
            continue
        power = idle_w + (max_w - idle_w) * ((a[4] + b[4]) / 2) / 100
        wh += power * dt / 3600
        seconds += dt
    return wh / 1000, seconds  # kWh, seconds covered


# =============================================================================
# UI widgets
# =============================================================================
class Btn(tk.Label):
    """Label-based button so colours look the same on Windows, macOS and Linux."""

    def __init__(self, master, text, command, bg, fg, hover, active=None, **kw):
        kw.setdefault("anchor", "w")
        super().__init__(master, text=text, bg=bg, fg=fg, font=F_BTN, cursor="hand2",
                         padx=18, pady=12, **kw)
        self._bg, self._hover, self._active_bg, self._on = bg, hover, active or hover, False
        self.bind("<Enter>", lambda e: self.config(bg=self._hover))
        self.bind("<Leave>", lambda e: self.config(bg=self._active_bg if self._on else self._bg))
        self.bind("<Button-1>", lambda e: command())

    def set_active(self, on):
        self._on = on
        self.config(bg=self._active_bg if on else self._bg)


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry("1180x740")
        root.minsize(1000, 680)
        root.configure(bg=PAPER)

        self.history = History()
        self.stop = threading.Event()
        psutil.cpu_percent(interval=None)
        threading.Thread(target=self._sample_loop, daemon=True).start()

        self.state = {}           # remembered settings so "Undo changes" can restore them
        self.idle_stop = threading.Event()
        self.idle_dimmed = None
        self.heavy = []
        self.chart_kind = "ram"

        self._build_sidebar()
        self._build_main()
        self.show_chart("ram")
        self._refresh_stats()
        self._import_result, self._import_manual = None, False
        if IS_WIN:
            self.start_import(auto=True)
        root.protocol("WM_DELETE_WINDOW", self._close)

    # ---- background sampling ------------------------------------------------
    def _sample_loop(self):
        count = 0
        while not self.stop.is_set():
            try:
                self.history.add()
                count += 1
                if count % 5 == 0:
                    self.history.save()
            except Exception:
                pass
            self.stop.wait(SAMPLE_SECONDS)

    def _close(self):
        self.stop.set()
        self.idle_stop.set()
        self.history.save()
        self.root.destroy()

    # ---- layout -------------------------------------------------------------
    def _build_sidebar(self):
        side = tk.Frame(self.root, bg=INK, width=270)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        tk.Label(side, text=APP_NAME, bg=INK, fg="#FFFFFF", font=F_TITLE,
                 anchor="w").pack(fill="x", padx=22, pady=(30, 2))
        tk.Label(side, text="Know what your laptop costs.\nThen make it cost less.", bg=INK,
                 fg="#A9C4B7", font=F_BODY, justify="left", anchor="w").pack(fill="x", padx=22, pady=(0, 26))

        self.btn_ram = Btn(side, "RAM, last 48 hours", lambda: self.show_chart("ram"),
                           INK, "#FFFFFF", INK_HOVER, INK_ACTIVE)
        self.btn_ram.pack(fill="x", pady=1)
        self.btn_batt = Btn(side, "Battery, last 48 hours", lambda: self.show_chart("battery"),
                            INK, "#FFFFFF", INK_HOVER, INK_ACTIVE)
        self.btn_batt.pack(fill="x", pady=1)
        imp = Btn(side, "Import Windows battery history", self.start_import, INK, "#A9C4B7", INK_HOVER)
        imp.config(font=F_BODY, pady=8)
        imp.pack(fill="x", pady=1)

        tk.Frame(side, bg=INK, height=26).pack()
        Btn(side, "Calculate", self.calculate, INK, "#FFFFFF", INK_HOVER).pack(fill="x", pady=1)

        tk.Frame(side, bg=INK).pack(expand=True, fill="both")

        Btn(side, "Undo changes", self.undo, INK, "#A9C4B7", INK_HOVER).pack(fill="x", pady=(0, 6))
        save = Btn(side, "Save it all", self.save_it_all, COIN, INK, COIN_HOVER)
        save.config(font=("Segoe UI Semibold", 13), pady=16, anchor="center")
        save.pack(fill="x", padx=18, pady=(0, 10))
        self.status = tk.Label(side, text="Logging every minute while this app is open.", bg=INK,
                               fg="#7FA394", font=F_SMALL, wraplength=230, justify="left", anchor="w")
        self.status.pack(fill="x", padx=22, pady=(0, 20))

    def _card(self, parent):
        return tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1)

    def _build_main(self):
        main = tk.Frame(self.root, bg=PAPER)
        main.pack(side="left", fill="both", expand=True, padx=26, pady=24)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=3)

        # Live stats
        stats = tk.Frame(main, bg=PAPER)
        stats.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        self.stat_vars = {}
        for i, (key, label) in enumerate([("ram", "Memory in use"), ("cpu", "CPU load"),
                                          ("batt", "Battery"), ("cost", "Estimated cost, 48 h")]):
            stats.columnconfigure(i, weight=1, uniform="s")
            card = self._card(stats)
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 12, 0))
            tk.Label(card, text=label, bg=CARD, fg=MUTED, font=F_SMALL, anchor="w").pack(fill="x", padx=16, pady=(12, 0))
            var = tk.StringVar(value="-")
            tk.Label(card, textvariable=var, bg=CARD, fg=TEXT, font=F_BIG, anchor="w").pack(fill="x", padx=16, pady=(0, 12))
            self.stat_vars[key] = var

        # Chart
        chart_card = self._card(main)
        chart_card.grid(row=1, column=0, sticky="nsew")
        self.fig = Figure(figsize=(7, 3.6), dpi=100, facecolor=CARD)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_card)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=10)

        # Calculator
        calc = self._card(main)
        calc.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        tk.Label(calc, text="Cost estimate for Singapore", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 11),
                 anchor="w").grid(row=0, column=0, columnspan=8, sticky="w", padx=16, pady=(12, 6))

        self.entries = {}
        fields = [("tariff", "Tariff (cents/kWh, with GST)", f"{SG_TARIFF_CENTS:.2f}"),
                  ("idle", "Laptop idle power (W)", "8"),
                  ("max", "Laptop full-load power (W)", "45"),
                  ("hours", "Hours on per day", "8")]
        for i, (key, label, default) in enumerate(fields):
            tk.Label(calc, text=label, bg=CARD, fg=MUTED, font=F_SMALL).grid(row=1, column=i, sticky="w", padx=(16, 4))
            e = tk.Entry(calc, width=9, font=F_BODY, relief="flat", bg=PAPER, fg=TEXT,
                         highlightthickness=1, highlightbackground=LINE, highlightcolor=TEAL)
            e.insert(0, default)
            e.grid(row=2, column=i, sticky="w", padx=(16, 4), pady=(2, 8), ipady=4)
            self.entries[key] = e
        Btn(calc, "Calculate", self.calculate, COIN, INK, COIN_HOVER, anchor="center").grid(
            row=1, column=4, rowspan=2, padx=(12, 16), pady=(2, 8), sticky="e")
        calc.columnconfigure(3, weight=1)

        self.result = tk.Label(calc, text="Press Calculate to turn your last 48 hours into Singapore dollars.",
                               bg=CARD, fg=TEXT, font=F_BODY, justify="left", anchor="w", wraplength=780)
        self.result.grid(row=3, column=0, columnspan=8, sticky="ew", padx=16, pady=(0, 14))

    # ---- live stats ---------------------------------------------------------
    def _refresh_stats(self):
        self.stat_vars["ram"].set(f"{psutil.virtual_memory().percent:.0f}%")
        self.stat_vars["cpu"].set(f"{psutil.cpu_percent(interval=None):.0f}%")
        b = psutil.sensors_battery()
        if b is None:
            self.stat_vars["batt"].set("No battery")
        else:
            self.stat_vars["batt"].set(f"{b.percent:.0f}%" + (" (plugged in)" if b.power_plugged else ""))
        self.root.after(3000, self._refresh_stats)

    # ---- battery import -----------------------------------------------------
    def start_import(self, auto=False):
        if getattr(self, "_importing", False):
            return
        self._importing, self._import_result, self._import_manual = True, None, not auto
        self.status.config(text="Reading Windows battery history...")

        def work():
            self._import_result = import_windows_battery()
        threading.Thread(target=work, daemon=True).start()
        self.root.after(500, self._poll_import)

    def _poll_import(self):
        if self._import_result is None:
            self.root.after(500, self._poll_import)
            return
        points, message = self._import_result
        self._importing = False
        added = self.history.merge_import(points) if points else 0
        self.status.config(text=(f"Added {added} earlier battery points from Windows." if points else
                                 "Couldn't read Windows battery history."))
        if not points and self._import_manual:
            messagebox.showinfo(APP_NAME, message)
        self.show_chart(self.chart_kind)

    # ---- charts -------------------------------------------------------------
    def show_chart(self, kind):
        self.chart_kind = kind
        self.btn_ram.set_active(kind == "ram")
        self.btn_batt.set_active(kind == "battery")
        rows = self.history.snapshot()
        ax = self.ax
        ax.clear()
        ax.set_facecolor(CARD)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(LINE)
        ax.tick_params(colors=MUTED, labelsize=9)
        ax.grid(axis="y", color=LINE, linewidth=0.8)
        ax.set_ylim(0, 100)

        idx = 2 if kind == "battery" else 1
        times, vals = [], []
        prev = None
        for r in rows:
            if r[idx] is None:
                continue
            imported = len(r) > 5 and r[5]
            limit = IMPORT_GAP if (imported or (prev is not None and prev[1])) else GAP_LIMIT
            if prev is not None and r[0] - prev[0] > limit:   # break the line over gaps
                times.append(datetime.fromtimestamp(prev[0] + 1))
                vals.append(float("nan"))
            times.append(datetime.fromtimestamp(r[0]))
            vals.append(r[idx])
            prev = (r[0], imported)

        colour = TEAL if kind == "ram" else COIN
        title = "RAM usage (%)" if kind == "ram" else "Battery level (%)"
        used = [r for r in rows if r[idx] is not None]
        hours = (used[-1][0] - used[0][0]) / 3600 if len(used) > 1 else 0
        ax.set_title(f"{title}  -  {hours:.1f} h recorded so far", loc="left", color=TEXT,
                     fontsize=12, fontweight="bold", pad=12)

        if not times:
            msg = ("No battery detected on this device." if kind == "battery"
                   else "Collecting data. Leave SaveOurCash open and the chart fills in.")
            ax.text(0.5, 0.5, msg, transform=ax.transAxes, ha="center", va="center", color=MUTED, fontsize=11)
        else:
            ax.plot(times, vals, color=colour, linewidth=2.2)
            ax.fill_between(times, vals, color=colour, alpha=0.15)
            if kind == "battery":   # shade periods spent on the charger
                start = None
                for r in rows:
                    t = datetime.fromtimestamp(r[0])
                    if r[3] and start is None:
                        start = t
                    if not r[3] and start is not None:
                        ax.axvspan(start, t, color=TEAL, alpha=0.10, linewidth=0)
                        start = None
                if start is not None:
                    ax.axvspan(start, times[-1], color=TEAL, alpha=0.10, linewidth=0)
                note = "green = plugged in"
                if any(len(r) > 5 and r[5] for r in rows):
                    note += "   |   older part comes from Windows' own battery log (fewer points)"
                ax.text(0.995, 0.03, note, transform=ax.transAxes, ha="right", color=TEAL, fontsize=9)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%H:%M"))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))
        self.fig.tight_layout()
        self.canvas.draw_idle()

    # ---- calculate ----------------------------------------------------------
    def _read_inputs(self):
        try:
            vals = {k: float(e.get()) for k, e in self.entries.items()}
            if vals["tariff"] <= 0 or vals["idle"] <= 0 or vals["max"] < vals["idle"] or not 0 < vals["hours"] <= 24:
                raise ValueError
            return vals
        except ValueError:
            messagebox.showerror(APP_NAME, "Check the numbers: tariff and watts must be above 0, "
                                           "full-load watts must be at least the idle watts, "
                                           "and hours per day must be between 0 and 24.")
            return None

    def calculate(self):
        v = self._read_inputs()
        if v is None:
            return
        rows = self.history.snapshot()
        kwh, secs = estimate_energy(rows, v["idle"], v["max"])
        if secs < 120:
            self.result.config(text="Not enough data yet. Keep SaveOurCash open for a few minutes and try again.")
            return
        rate = v["tariff"] / 100
        hours = secs / 3600
        avg_w = kwh * 1000 / hours
        cost = kwh * rate
        per_month = avg_w / 1000 * v["hours"] * 30 * rate
        self.stat_vars["cost"].set(f"S${cost:.2f}")
        self.result.config(text=(
            f"Recorded: {hours:.1f} h   |   Average draw: {avg_w:.1f} W   |   Energy: {kwh:.3f} kWh\n"
            f"Cost over the recorded period: S${cost:.2f}\n"
            f"At {v['hours']:.0f} h a day, that's about S${per_month:.2f} a month "
            f"and S${per_month * 12:.2f} a year.\n"
            "Estimate only: power is modelled from your CPU load and the watts above, not measured at the wall."))

    # ---- Save it all ---------------------------------------------------------
    def save_it_all(self):
        if not messagebox.askyesno(
                APP_NAME,
                "Save it all will:\n\n"
                " - check CPU use and the heaviest processes\n"
                " - lower screen brightness\n"
                " - offer to close known background apps\n"
                " - start idle detection\n"
                " - offer to turn off Wi-Fi if nothing is using it\n"
                " - switch to the power-saver profile\n"
                " - lower the priority of heavy background tasks\n\n"
                "You can reverse most of it with 'Undo changes'. Continue?"):
            return
        self.status.config(text="Working...")
        self.root.update_idletasks()

        report = []
        steps = [("CPU monitoring", self.step_cpu), ("Screen brightness", self.step_brightness),
                 ("Background process management", self.step_background), ("Idle detection", self.step_idle),
                 ("Wi-Fi management", self.step_wifi), ("Power mode management", self.step_power),
                 ("Application scheduling", self.step_schedule)]
        for title, fn in steps:
            try:
                lines = fn()
            except Exception as exc:  # one failing step should never stop the others
                lines = [f"Not applied: {exc}"]
            report.append((title, lines))
        self.status.config(text="Done. Use 'Undo changes' to reverse.")
        self.show_report(report)

    def step_cpu(self):
        procs = list(psutil.process_iter(["pid", "name"]))
        for p in procs:
            try:
                p.cpu_percent(None)
            except Exception:
                pass
        time.sleep(1)
        ncpu = psutil.cpu_count() or 1
        rows, self.heavy = [], []
        me = os.getpid()
        fg = foreground_pid()
        for p in procs:
            try:
                name = p.info["name"] or "?"
                if p.pid in (0, me) or name == "System Idle Process":
                    continue
                cpu = p.cpu_percent(None) / ncpu
                rows.append((cpu, p.memory_info().rss, name))
                if cpu >= 3 and p.pid != fg and p.pid > 4:
                    self.heavy.append((p, name, cpu))
            except Exception:
                continue
        rows.sort(reverse=True)
        total = psutil.cpu_percent(interval=None)
        out = [f"Overall CPU load: {total:.0f}%. Heaviest processes right now:"]
        for cpu, mem, name in rows[:5]:
            out.append(f"   {name}: {cpu:.1f}% CPU, {fmt_mb(mem)}")
        if len(out) == 1:
            out.append("   Nothing is using noticeable CPU. Good.")
        return out

    def step_brightness(self):
        current = get_brightness()
        if current is None:
            return ["Not applied: couldn't read screen brightness (desktop monitor or unsupported driver). "
                    "Tip: pip install screen-brightness-control"]
        batt = psutil.sensors_battery()
        target = 60 if (batt is None or batt.power_plugged) else 40
        if current <= target:
            return [f"Left unchanged: brightness is already {current}%."]
        self.state.setdefault("brightness", current)
        if set_brightness(target):
            return [f"Brightness lowered from {current}% to {target}%."]
        return ["Not applied: Windows refused the brightness change."]

    def step_background(self):
        targets = {"spotify", "discord", "steam", "steamwebhelper", "epicgameslauncher", "onedrive", "dropbox",
                   "slack", "skype", "teams", "ms-teams", "origin", "eadesktop", "battle.net", "itunes",
                   "adobeupdateservice", "googledrivefs"}
        found = {}
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                n = (p.info["name"] or "").lower().removesuffix(".exe")
                if n in targets:
                    found.setdefault(p.info["name"], []).append((p, p.info["memory_info"].rss))
            except Exception:
                continue
        if not found:
            return ["No known background apps are running. Nothing to close."]
        listing = "\n".join(f" - {n}  ({fmt_mb(sum(m for _, m in ps))})" for n, ps in found.items())
        if not messagebox.askyesno(APP_NAME, "These background apps are using memory and CPU:\n\n"
                                             f"{listing}\n\nClose them? Unsaved work in them will be lost."):
            return ["Left running (you chose not to close them): " + ", ".join(found)]
        closed, failed = [], []
        for name, plist in found.items():
            ok = True
            for p, _ in plist:
                try:
                    p.terminate()
                except Exception:
                    ok = False
            (closed if ok else failed).append(name)
        out = []
        if closed:
            out.append("Closed: " + ", ".join(closed))
        if failed:
            out.append("Couldn't close (access denied): " + ", ".join(failed))
        return out

    def step_idle(self):
        if idle_seconds() is None:
            return ["Not applied: idle detection isn't supported on this system "
                    "(on Linux, install xprintidle)."]
        if not self.idle_stop.is_set() and self.state.get("idle_thread"):
            return ["Idle detection was already running."]
        self.idle_stop.clear()
        t = threading.Thread(target=self._idle_loop, daemon=True)
        t.start()
        self.state["idle_thread"] = t
        return [f"Idle detection is on. After {IDLE_LIMIT // 60} minutes without keyboard or mouse input, "
                "the screen dims to 10%, and returns to normal as soon as you move."]

    def _idle_loop(self):
        while not self.idle_stop.wait(5):
            idle = idle_seconds()
            if idle is None:
                return
            if idle >= IDLE_LIMIT and self.idle_dimmed is None:
                cur = get_brightness()
                if cur is not None and cur > 10 and set_brightness(10):
                    self.idle_dimmed = cur
            elif idle < 10 and self.idle_dimmed is not None:
                set_brightness(self.idle_dimmed)
                self.idle_dimmed = None

    def step_wifi(self):
        if IS_WIN:
            r = run(["netsh", "wlan", "show", "interfaces"])
            m = re.search(r"^\s*Name\s*:\s*(.+)$", r.stdout, re.M)
            if not m:
                return ["Left unchanged: no active Wi-Fi connection found."]
            iface = m.group(1).strip()
        elif IS_LINUX:
            iface = None
        else:
            return ["Not applied: Wi-Fi control isn't supported on this operating system."]

        before = net_bytes()
        time.sleep(2)
        rate = (net_bytes() - before) / 2
        if rate > 20_000:
            return [f"Left on: Wi-Fi is busy right now ({rate / 1024:.0f} KB/s), so turning it off would interrupt something."]
        if not messagebox.askyesno(APP_NAME, "Wi-Fi looks idle. Turn it off to save power?\n\n"
                                             "You'll be offline until you press 'Undo changes' "
                                             "or turn Wi-Fi back on yourself."):
            return ["Left on (you chose to keep Wi-Fi on)."]
        if IS_WIN:
            r = run(["netsh", "interface", "set", "interface", f"name={iface}", "admin=disabled"])
            if r.returncode == 0:
                self.state["wifi"] = iface
                return [f"Wi-Fi adapter '{iface}' turned off."]
            run(["netsh", "wlan", "disconnect"])
            self.state["wifi_disconnected"] = True
            return ["Disconnected from Wi-Fi. The adapter stays powered because turning it off needs "
                    "Administrator rights. Run SaveOurCash as Administrator to switch it off fully."]
        r = run(["nmcli", "radio", "wifi", "off"])
        if r.returncode == 0:
            self.state["wifi"] = "nmcli"
            return ["Wi-Fi radio turned off."]
        return ["Not applied: nmcli couldn't turn Wi-Fi off."]

    def step_power(self):
        if IS_WIN:
            before = run(["powercfg", "/getactivescheme"]).stdout
            m = re.search(r"([0-9a-fA-F-]{36})\s*\(([^)]*)\)", before)
            old = (m.group(1), m.group(2)) if m else None
            if old and "saver" in old[1].lower():
                return [f"Left unchanged: already on '{old[1]}'."]
            r = run(["powercfg", "/setactive", "SCHEME_MAX"])  # SCHEME_MAX = Power saver
            if r.returncode != 0:
                return ["Not applied: Windows wouldn't switch to Power saver "
                        "(some laptops hide it; pick 'Best power efficiency' in Settings > System > Power)."]
            if old:
                self.state["power"] = old[0]
            return [f"Power plan switched to Power saver (was '{old[1] if old else 'unknown'}')."]
        if IS_LINUX:
            cur = run(["powerprofilesctl", "get"]).stdout.strip()
            r = run(["powerprofilesctl", "set", "power-saver"])
            if r.returncode == 0:
                self.state["power_linux"] = cur or "balanced"
                return [f"Power profile switched to power-saver (was '{cur}')."]
            return ["Not applied: powerprofilesctl isn't available."]
        return ["Not applied: power profiles aren't supported on this operating system."]

    def step_schedule(self):
        if not self.heavy:
            return ["Nothing to defer: no heavy background tasks found."]
        lowered, saved = [], []
        for p, name, cpu in self.heavy:
            try:
                old = p.nice()
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if IS_WIN else 10)
                saved.append((p, old))
                lowered.append(f"{name} ({cpu:.0f}% CPU)")
            except Exception:
                continue
        self.state["priorities"] = saved
        if not lowered:
            return ["Not applied: Windows blocked priority changes for these processes."]
        return ["Heavy tasks now yield to whatever you're using, so their work happens in the gaps:",
                "   " + ", ".join(lowered)]

    # ---- Undo ---------------------------------------------------------------
    def undo(self):
        out = []
        self.idle_stop.set()
        if self.idle_dimmed is not None:
            set_brightness(self.idle_dimmed)
            self.idle_dimmed = None
        self.state.pop("idle_thread", None)
        out.append("Idle detection stopped.")
        if "brightness" in self.state:
            b = self.state.pop("brightness")
            out.append(f"Brightness restored to {b}%." if set_brightness(b) else "Couldn't restore brightness.")
        if "wifi" in self.state:
            w = self.state.pop("wifi")
            if w == "nmcli":
                run(["nmcli", "radio", "wifi", "on"])
            else:
                run(["netsh", "interface", "set", "interface", f"name={w}", "admin=enabled"])
            out.append("Wi-Fi turned back on (it may take a few seconds to reconnect).")
        if self.state.pop("wifi_disconnected", None):
            out.append("Wi-Fi was only disconnected: Windows reconnects automatically, or pick your network.")
        if "power" in self.state:
            run(["powercfg", "/setactive", self.state.pop("power")])
            out.append("Previous power plan restored.")
        if "power_linux" in self.state:
            run(["powerprofilesctl", "set", self.state.pop("power_linux")])
            out.append("Previous power profile restored.")
        for p, old in self.state.pop("priorities", []):
            try:
                p.nice(old)
            except Exception:
                pass
        out.append("Process priorities restored. (Apps that were closed stay closed.)")
        self.status.config(text="Changes undone.")
        self.show_report([("Undo changes", out)])

    # ---- report box ---------------------------------------------------------
    def show_report(self, sections):
        win = tk.Toplevel(self.root)
        win.title(f"{APP_NAME} - what changed")
        win.geometry("700x600")
        win.configure(bg=PAPER)
        win.transient(self.root)
        tk.Label(win, text="Here's what SaveOurCash did", bg=PAPER, fg=TEXT, font=("Georgia", 16, "bold"),
                 anchor="w").pack(fill="x", padx=22, pady=(20, 8))
        frame = tk.Frame(win, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        frame.pack(fill="both", expand=True, padx=22)
        box = tk.Text(frame, wrap="word", bg=CARD, fg=TEXT, font=F_BODY, relief="flat", padx=16, pady=12,
                      spacing1=2, spacing3=2)
        scroll = tk.Scrollbar(frame, command=box.yview)
        box.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        box.pack(side="left", fill="both", expand=True)
        box.tag_configure("h", font=("Segoe UI Semibold", 11), foreground=INK, spacing1=12)
        box.tag_configure("bad", foreground="#A4452C")
        for title, lines in sections:
            box.insert("end", title + "\n", "h")
            for line in lines:
                bad = line.startswith(("Not applied", "Couldn't", "Left unchanged"))
                box.insert("end", line + "\n", "bad" if line.startswith(("Not applied", "Couldn't")) else "")
        box.config(state="disabled")
        Btn(win, "Close", win.destroy, INK, "#FFFFFF", INK_HOVER, anchor="center").pack(
            padx=22, pady=16, anchor="e")


class LoginScreen(tk.Frame):
    def __init__(self, root, on_success):
        super().__init__(root, bg=INK)
        self.root, self.on_success = root, on_success
        self.fails, self.locked_until = 0, 0.0

        root.title(f"{APP_NAME} - Log in")
        root.configure(bg=INK)
        root.resizable(False, False)
        w, h = 480, 620
        root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
        self.pack(fill="both", expand=True)

        tk.Label(self, text=APP_NAME, bg=INK, fg="#FFFFFF", font=("Georgia", 30, "bold")).pack(pady=(64, 4))
        tk.Label(self, text="Know what your laptop costs.\nThen make it cost less.", bg=INK, fg="#A9C4B7",
                 font=F_BODY, justify="center").pack()

        card = tk.Frame(self, bg=CARD)
        card.pack(fill="x", padx=44, pady=34)
        tk.Label(card, text="Log in", bg=CARD, fg=TEXT, font=("Georgia", 16, "bold"),
                 anchor="w").pack(fill="x", padx=24, pady=(22, 6))
        self.user = self._field(card, "Username")
        self.pw = self._field(card, "Password", show="\u2022")

        self.show = tk.BooleanVar(value=False)
        tk.Checkbutton(card, text="Show password", variable=self.show, command=self._toggle,
                       bg=CARD, fg=MUTED, activebackground=CARD, activeforeground=MUTED, selectcolor=CARD,
                       font=F_SMALL, anchor="w", bd=0, highlightthickness=0).pack(fill="x", padx=20, pady=(6, 0))

        self.msg = tk.Label(card, text="", bg=CARD, fg="#A4452C", font=F_SMALL, wraplength=340,
                            justify="left", anchor="w")
        self.msg.pack(fill="x", padx=24, pady=(8, 0))
        Btn(card, "Log in", self.submit, COIN, INK, COIN_HOVER, anchor="center").pack(
            fill="x", padx=24, pady=(10, 24))

        self.user.focus_set()

    def _field(self, parent, label, show=""):
        tk.Label(parent, text=label, bg=CARD, fg=MUTED, font=F_SMALL, anchor="w").pack(fill="x", padx=24, pady=(10, 2))
        e = tk.Entry(parent, font=("Segoe UI", 11), relief="flat", bg=PAPER, fg=TEXT, show=show,
                     highlightthickness=1, highlightbackground=LINE, highlightcolor=TEAL, insertbackground=TEXT)
        e.pack(fill="x", padx=24, ipady=7)
        e.bind("<Return>", lambda ev: self.submit())
        return e

    def _toggle(self):
        self.pw.config(show="" if self.show.get() else "\u2022")

    def submit(self):
        wait = self.locked_until - time.time()
        if wait > 0:
            self.msg.config(text=f"Too many attempts. Try again in {wait:.0f} seconds.")
            return
        user, pw = self.user.get().strip(), self.pw.get()
        ok_user = hmac.compare_digest(user.encode("utf-8"), VALID_USER.encode("utf-8"))
        ok_pass = hmac.compare_digest(pw.encode("utf-8"), VALID_PASS.encode("utf-8"))
        if ok_user and ok_pass:
            self.destroy()
            self.root.resizable(True, True)
            self.on_success()
            return
        self.fails += 1
        self.pw.delete(0, "end")
        if self.fails >= MAX_ATTEMPTS:
            self.fails = 0
            self.locked_until = time.time() + LOCKOUT_SECONDS
            self.msg.config(text=f"Too many attempts. Try again in {LOCKOUT_SECONDS} seconds.")
        else:
            self.msg.config(text="Username or password is incorrect.")
        self.pw.focus_set()


def main():
    root = tk.Tk()
    LoginScreen(root, lambda: App(root))
    root.mainloop()


if __name__ == "__main__":
    main()
