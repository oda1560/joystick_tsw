"""
Train Sim World 7 joystick bridge - desktop window.

Shows joystick / game / train status, live stick and lever gauges, and settings.
The bridge itself (game API, lever detection, emergency exclusion) lives in tsw_joystick.py.

Usage:
    pythonw tsw_joystick_ui.py            (or double-click Start TSW Joystick.bat)
    pythonw tsw_joystick_ui.py --paused   start with the bridge paused
"""

import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import tkinter as tk
from tkinter import ttk

import tsw_joystick as core   # sets SDL env vars before pygame is imported
import pygame

try:
    import winsound
except ImportError:
    winsound = None

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "settings.json")
DEFAULTS = {"axis": core.Y_AXIS, "invert": core.INVERT_Y, "deadzone": core.DEADZONE,
            "toggle_button": None, "on_top": False, "joystick": "",
            "rev_enabled": core.USE_REVERSER, "rev_axis": core.REVERSER_AXIS,
            "rev_invert": core.REVERSER_INVERT, "aws_button": core.AWS_BUTTON,
            "look_enabled": core.USE_LOOK, "look_axis": core.LOOK_AXIS, "look_invert": core.LOOK_INVERT,
            "look_deadzone": core.LOOK_DEADZONE, "look_angle": core.LOOK_MAX_ANGLE,
            "look_smoothing": core.LOOK_SMOOTHING}

BG, PANEL, TRACK = "#16181d", "#1f232b", "#2b3039"
FG, MUTED = "#e6e8ec", "#8a919e"
GOOD, WARN, BAD = "#4cc38a", "#e5b54a", "#e5534b"
POWER, BRAKE = "#4c9be8", "#e08a3c"
FONT = "Segoe UI"
DOT = {"ok": GOOD, "warn": WARN, "bad": BAD, "idle": MUTED}


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    if s["toggle_button"] is not None and s["toggle_button"] == s["aws_button"]:
        s["toggle_button"] = None      # one joystick button can't do both
    return s


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except OSError:
        pass


def pretty_train(cls):
    if not cls:
        return "-"
    s = re.sub(r"^(RVM|BP)_", "", str(cls))
    return re.sub(r"_C$", "", s).replace("_", " ")


def pretty_lever(name):
    s = re.sub(r"^(IrregularLever|SimpleLever|Lever)_", "", name)
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s).replace("_", " ")


def pretty_zone(label):
    s = re.sub(r"\{[^}]*\}", "", label).strip(" -")
    return s.title() if s else ""


# ---------------------------------------------------------------- bridge worker
class Bridge(threading.Thread):
    """Talks to the game on a background thread so a slow API never freezes the window."""

    def __init__(self, log):
        super().__init__(daemon=True)
        self.api = core.TSWApi()
        self.log = log
        self.controls = None          # TrainControls, replaced as a whole on train change
        self.train = None
        self.game_status = ("idle", "Starting...")
        self.enabled = True
        self.y = None                 # latest stick value from the UI thread, None = no joystick
        self.force_detect = False
        self.running = True
        self.last_sent = None
        self.last_check = 0.0
        self.rev_zone = None          # slider zone from the UI thread, None = reverser control off
        self.rev_sync = core.ReverserSync(log)
        self.rev_actual = None        # train's current reverser notch, for display
        self.speed = None             # m/s, for display
        self.last_poll = 0.0
        self.aws_queue = queue.Queue()   # True = AWS button pressed, False = released (from UI thread)

    def set_game(self, level, text):
        if (level, text) != self.game_status:
            self.game_status = (level, text)
            self.log(f"Game: {text}")

    def drop_train(self):
        self.controls = None
        self.train = None
        self.last_sent = None
        self.rev_sync.reset()
        self.rev_actual = self.speed = None

    def _handle_aws(self, controls):
        while True:
            try:
                down = self.aws_queue.get_nowait()
            except queue.Empty:
                return
            if controls is None or controls.aws is None:
                continue
            if down and not self.enabled:
                continue              # paused: ignore presses, but always pass releases through
            controls.aws.set(down)
            if down:
                self.log("AWS acknowledged")

    def run(self):
        while self.running:
            time.sleep(1.0 / core.POLL_HZ)
            try:
                self.step()
            except Exception as e:
                self.set_game("bad", f"Error: {e}")
                self.drop_train()
                time.sleep(1)

    def step(self):
        if not self.api.key and not self.api.load_key():
            self.set_game("bad", "No API key - start TSW7 with -HTTPAPI in Steam launch options")
            time.sleep(2)
            return

        now = time.time()
        if self.force_detect or now - self.last_check > core.TRAIN_CHECK_SECONDS:
            self.last_check = now
            force, self.force_detect = self.force_detect, False
            try:
                train = self.api.get_value("CurrentDrivableActor.ObjectClass")
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    self.api.load_key()   # key may have been regenerated
                self.set_game("warn", f"Game API error {e.code}")
                self.drop_train()
                return
            except Exception:
                self.set_game("bad", "Game not reachable - is TSW7 running with -HTTPAPI?")
                self.drop_train()
                return
            if train is None:
                self.set_game("warn", "Connected - not in a cab")
                self.drop_train()
                return
            self.set_game("ok", "Connected")
            if train != self.train or force:
                controls = core.TrainControls(self.api)
                controls.detect()
                controls.train_id = train
                self.controls, self.train, self.last_sent = controls, train, None
                self.rev_sync.reset()
                self.log(f"Train: {pretty_train(train)} -> {controls.describe()}")

        controls = self.controls
        self._handle_aws(controls)
        if controls is None:
            return
        if now - self.last_poll > 0.5:
            self.last_poll = now
            self.speed = controls.speed()
            self.rev_actual = controls.reverser.position() if controls.reverser else None

        if not self.enabled:
            self.last_sent = None
            self.rev_sync.reset()
            return
        self.rev_sync.update(controls, self.rev_zone)

        y = self.y
        if y is None:
            self.last_sent = None
            return
        if self.last_sent is None:
            self.last_sent = y        # don't snap levers until the stick actually moves
            return
        if abs(y - self.last_sent) < core.SEND_THRESHOLD:
            return                    # stick not moved: leave levers alone so the keyboard still works
        try:
            controls.apply(y)
            self.last_sent = y
        except Exception as e:
            self.log(f"Send failed: {e}")
            self.drop_train()


# ---------------------------------------------------------------- window
class App:
    CW, CH = 560, 290    # canvas size at 96 dpi
    GH = 250             # height of the gauge area; the look bar sits below it

    def __init__(self, root, start_paused=False):
        self.root = root
        self.s = load_settings()
        self.logq = queue.Queue()
        self.last_log = None
        self.bridge = Bridge(self.log)
        self.bridge.enabled = not start_paused
        self.look = core.LookController(self.log)
        self.look_raw = 0.0           # twist position, +1 = full right
        self.stick = None
        self.next_stick_try = 0.0
        self.assigning = None         # settings key waiting for a joystick button press
        self.aws_held = False
        self.raw_y = 0.0
        self.y = 0.0
        self.rev_s = 0.0              # slider position, +1 = forward end
        self.rev_zone = None
        self.frame = 0
        self.k = root.winfo_fpixels("1i") / 96.0     # display scaling for canvas drawing

        pygame.init()
        self._style()
        self._build()
        self._refresh_toggle()
        self._refresh_button_label()
        root.attributes("-topmost", bool(self.s["on_top"]))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.bridge.start()
        self.look.start()
        self.log("Bridge started" + (" (paused)" if start_paused else ""))
        self.tick()

    # ---- layout
    def _style(self):
        r = self.root
        r.configure(bg=BG)
        st = ttk.Style(r)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, fieldbackground=TRACK, font=(FONT, 10),
                     bordercolor=TRACK, lightcolor=PANEL, darkcolor=PANEL, troughcolor=TRACK)
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("Panel.TLabel", background=PANEL, foreground=FG)
        st.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        st.configure("Section.TLabel", background=PANEL, foreground=MUTED, font=(FONT, 8, "bold"))
        st.configure("Head.TLabel", background=BG, foreground=FG, font=(FONT, 15, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=MUTED)
        st.configure("TButton", background=TRACK, foreground=FG, borderwidth=0, padding=(10, 4))
        st.map("TButton", background=[("active", "#3a414d"), ("pressed", "#454d5b")])
        st.configure("Panel.TCheckbutton", background=PANEL, foreground=FG)
        st.map("Panel.TCheckbutton", background=[("active", PANEL)],
               indicatorcolor=[("selected", GOOD), ("!selected", TRACK)])
        st.configure("TCombobox", fieldbackground=TRACK, background=TRACK, foreground=FG,
                     arrowcolor=FG, selectbackground=TRACK, selectforeground=FG, padding=3)
        st.map("TCombobox", fieldbackground=[("readonly", TRACK)], foreground=[("readonly", FG)],
               selectbackground=[("readonly", TRACK)], selectforeground=[("readonly", FG)])
        st.configure("Horizontal.TScale", background=MUTED, troughcolor=TRACK)
        r.option_add("*TCombobox*Listbox.background", PANEL)
        r.option_add("*TCombobox*Listbox.foreground", FG)
        r.option_add("*TCombobox*Listbox.selectBackground", "#3a414d")
        r.option_add("*TCombobox*Listbox.selectForeground", FG)

    def _panel(self, parent, title):
        p = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 10))
        p.pack(fill="x", pady=(0, 10))
        ttk.Label(p, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        return p

    def _build(self):
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        hdr = ttk.Frame(outer)
        hdr.pack(fill="x")
        ttk.Label(hdr, text="TSW7 Joystick Bridge", style="Head.TLabel").pack(side="left")
        self.toggle_btn = tk.Button(hdr, command=self.toggle, relief="flat", bd=0, cursor="hand2",
                                    font=(FONT, 11, "bold"), padx=16, pady=5)
        self.toggle_btn.pack(side="right")
        ttk.Label(outer, text="Push the stick forward for power, pull back to brake. "
                              "Emergency brake is never used.",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 10))

        # status
        sp = self._panel(outer, "STATUS")
        self.status = {}
        for key, title in (("joy", "Joystick"), ("game", "Game"), ("train", "Train")):
            row = ttk.Frame(sp, style="Panel.TFrame")
            row.pack(fill="x", pady=2)
            d = round(12 * self.k)
            dot = tk.Canvas(row, width=d, height=d, bg=PANEL, highlightthickness=0)
            dot.create_oval(1, 1, d - 1, d - 1, fill=MUTED, outline="", tags="dot")
            dot.pack(side="left", padx=(0, 8))
            ttk.Label(row, text=title, style="Muted.TLabel", width=9).pack(side="left")
            val = ttk.Label(row, text="-", style="Panel.TLabel")
            val.pack(side="left")
            self.status[key] = [dot, val, None]
        self.lever_desc = ttk.Label(sp, text="", style="Muted.TLabel",
                                    wraplength=round(520 * self.k), justify="left")
        self.lever_desc.pack(anchor="w", pady=(4, 0))

        # live gauges
        gp = self._panel(outer, "LIVE")
        self.canvas = tk.Canvas(gp, width=round(self.CW * self.k), height=round(self.CH * self.k),
                                bg=PANEL, highlightthickness=0)
        self.canvas.pack()

        # settings
        setp = self._panel(outer, "SETTINGS")
        grid = ttk.Frame(setp, style="Panel.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Joystick", style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=3, padx=(0, 14))
        self.joy_combo = ttk.Combobox(grid, state="readonly", width=40)
        self.joy_combo.grid(row=0, column=1, columnspan=3, sticky="w", pady=3)
        self.joy_combo.bind("<<ComboboxSelected>>", self.on_joystick)

        ttk.Label(grid, text="Axis", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=3, padx=(0, 14))
        axis_row = ttk.Frame(grid, style="Panel.TFrame")
        axis_row.grid(row=1, column=1, columnspan=3, sticky="w", pady=3)
        self.axis_combo = ttk.Combobox(axis_row, state="readonly", width=8)
        self.axis_combo.pack(side="left")
        self.axis_combo.bind("<<ComboboxSelected>>", self.on_axis)
        self.invert_var = tk.BooleanVar(value=bool(self.s["invert"]))
        ttk.Checkbutton(axis_row, text="Invert", variable=self.invert_var, style="Panel.TCheckbutton",
                        command=self.on_invert).pack(side="left", padx=(12, 12))
        self.axes_live = ttk.Label(axis_row, text="", style="Muted.TLabel", font=("Consolas", 9))
        self.axes_live.pack(side="left")

        ttk.Label(grid, text="Dead zone", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=3, padx=(0, 14))
        dz_row = ttk.Frame(grid, style="Panel.TFrame")
        dz_row.grid(row=2, column=1, columnspan=3, sticky="w", pady=3)
        ttk.Label(dz_row, text="Stick", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.dz_scale = ttk.Scale(dz_row, from_=0.0, to=0.3, length=round(130 * self.k),
                                  value=float(self.s["deadzone"]), command=self.on_deadzone)
        self.dz_scale.pack(side="left")
        self.dz_scale.bind("<ButtonRelease-1>", lambda e: save_settings(self.s))
        self.dz_label = ttk.Label(dz_row, text="", style="Panel.TLabel", width=5)
        self.dz_label.pack(side="left", padx=(6, 12))
        self.on_deadzone(self.s["deadzone"])
        ttk.Label(dz_row, text="Twist", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.look_dz_scale = ttk.Scale(dz_row, from_=0.0, to=0.5, length=round(130 * self.k),
                                       value=float(self.s["look_deadzone"]), command=self.on_look_deadzone)
        self.look_dz_scale.pack(side="left")
        self.look_dz_scale.bind("<ButtonRelease-1>", lambda e: save_settings(self.s))
        self.look_dz_label = ttk.Label(dz_row, text="", style="Panel.TLabel", width=5)
        self.look_dz_label.pack(side="left", padx=(6, 0))
        self.on_look_deadzone(self.s["look_deadzone"])

        ttk.Label(grid, text="Reverser", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=3, padx=(0, 14))
        rev_row = ttk.Frame(grid, style="Panel.TFrame")
        rev_row.grid(row=3, column=1, columnspan=3, sticky="w", pady=3)
        self.rev_var = tk.BooleanVar(value=bool(self.s["rev_enabled"]))
        ttk.Checkbutton(rev_row, text="Use slider", variable=self.rev_var, style="Panel.TCheckbutton",
                        command=self.on_rev_enabled).pack(side="left", padx=(0, 12))
        self.rev_axis_combo = ttk.Combobox(rev_row, state="readonly", width=8)
        self.rev_axis_combo.pack(side="left")
        self.rev_axis_combo.bind("<<ComboboxSelected>>", self.on_rev_axis)
        self.rev_invert_var = tk.BooleanVar(value=bool(self.s["rev_invert"]))
        ttk.Checkbutton(rev_row, text="Invert", variable=self.rev_invert_var, style="Panel.TCheckbutton",
                        command=self.on_rev_invert).pack(side="left", padx=(12, 0))

        ttk.Label(grid, text="Look", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=3, padx=(0, 14))
        look_row = ttk.Frame(grid, style="Panel.TFrame")
        look_row.grid(row=4, column=1, columnspan=3, sticky="w", pady=3)
        self.look_var = tk.BooleanVar(value=bool(self.s["look_enabled"]))
        ttk.Checkbutton(look_row, text="Use twist", variable=self.look_var, style="Panel.TCheckbutton",
                        command=self.on_look_enabled).pack(side="left", padx=(0, 12))
        self.look_axis_combo = ttk.Combobox(look_row, state="readonly", width=8)
        self.look_axis_combo.pack(side="left")
        self.look_axis_combo.bind("<<ComboboxSelected>>", self.on_look_axis)
        self.look_invert_var = tk.BooleanVar(value=bool(self.s["look_invert"]))
        ttk.Checkbutton(look_row, text="Invert", variable=self.look_invert_var, style="Panel.TCheckbutton",
                        command=self.on_look_invert).pack(side="left", padx=(12, 12))
        self.look_angle_scale = ttk.Scale(look_row, from_=30, to=150, length=round(100 * self.k),
                                          value=float(self.s["look_angle"]), command=self.on_look_angle)
        self.look_angle_scale.pack(side="left")
        self.look_angle_scale.bind("<ButtonRelease-1>", lambda e: save_settings(self.s))
        self.look_angle_label = ttk.Label(look_row, text="", style="Panel.TLabel", width=5)
        self.look_angle_label.pack(side="left", padx=(6, 0))
        self.on_look_angle(self.s["look_angle"])

        ttk.Label(grid, text="Look smoothing", style="Muted.TLabel").grid(row=5, column=0, sticky="w",
                                                                         pady=3, padx=(0, 14))
        smooth_row = ttk.Frame(grid, style="Panel.TFrame")
        smooth_row.grid(row=5, column=1, columnspan=3, sticky="w", pady=3)
        ttk.Label(smooth_row, text="Quick", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.smooth_scale = ttk.Scale(smooth_row, from_=0.0, to=0.6, length=round(180 * self.k),
                                      value=float(self.s["look_smoothing"]), command=self.on_look_smoothing)
        self.smooth_scale.pack(side="left")
        self.smooth_scale.bind("<ButtonRelease-1>", lambda e: save_settings(self.s))
        ttk.Label(smooth_row, text="Smooth", style="Muted.TLabel").pack(side="left", padx=(6, 10))
        self.smooth_label = ttk.Label(smooth_row, text="", style="Panel.TLabel", width=7)
        self.smooth_label.pack(side="left")
        self.on_look_smoothing(self.s["look_smoothing"])

        self.btn_labels = {}
        for row, (key, title) in enumerate((("aws_button", "AWS button"),
                                            ("toggle_button", "Pause button")), start=6):
            ttk.Label(grid, text=title, style="Muted.TLabel").grid(row=row, column=0, sticky="w",
                                                                   pady=3, padx=(0, 14))
            btn_row = ttk.Frame(grid, style="Panel.TFrame")
            btn_row.grid(row=row, column=1, columnspan=3, sticky="w", pady=3)
            self.btn_labels[key] = ttk.Label(btn_row, text="", style="Panel.TLabel", width=24)
            self.btn_labels[key].pack(side="left")
            ttk.Button(btn_row, text="Assign",
                       command=lambda k=key: self.on_assign(k)).pack(side="left", padx=(0, 6))
            ttk.Button(btn_row, text="Clear",
                       command=lambda k=key: self.on_clear_button(k)).pack(side="left")

        bottom = ttk.Frame(setp, style="Panel.TFrame")
        bottom.pack(fill="x", pady=(8, 0))
        self.top_var = tk.BooleanVar(value=bool(self.s["on_top"]))
        ttk.Checkbutton(bottom, text="Keep this window on top", variable=self.top_var,
                        style="Panel.TCheckbutton", command=self.on_top).pack(side="left")
        ttk.Button(bottom, text="Re-detect train", command=self.on_redetect).pack(side="right")

        # log
        lp = self._panel(outer, "LOG")
        self.logtext = tk.Text(lp, height=4, bg=PANEL, fg=MUTED, relief="flat", bd=0,
                               font=("Consolas", 9), wrap="word", state="disabled",
                               highlightthickness=0)
        self.logtext.pack(fill="x")

    # ---- actions
    def log(self, msg):
        if msg == self.last_log:
            return
        self.last_log = msg
        self.logq.put(f"{time.strftime('%H:%M:%S')}  {msg}")

    def toggle(self):
        self.bridge.enabled = not self.bridge.enabled
        self._refresh_toggle()
        self.log("Bridge active" if self.bridge.enabled else "Bridge paused - joystick ignored")
        if winsound:
            tone = 880 if self.bridge.enabled else 440
            threading.Thread(target=winsound.Beep, args=(tone, 120), daemon=True).start()

    def _refresh_toggle(self):
        if self.bridge.enabled:
            self.toggle_btn.config(text="●  ACTIVE", bg=GOOD, fg="#0b1a12",
                                   activebackground=GOOD, activeforeground="#0b1a12")
        else:
            self.toggle_btn.config(text="❚❚  PAUSED", bg=WARN, fg="#1f1600",
                                   activebackground=WARN, activeforeground="#1f1600")

    def _refresh_button_label(self):
        action = {"aws_button": "acknowledges AWS", "toggle_button": "pauses / resumes"}
        for key, label in self.btn_labels.items():
            if self.assigning == key:
                text = "Press a joystick button..."
            elif self.s[key] is None:
                text = "Not set"
            else:
                text = f"Button {self.s[key] + 1} {action[key]}"
            label.config(text=text)

    def on_assign(self, key):
        self.assigning = key
        self._refresh_button_label()

    def on_clear_button(self, key):
        self.assigning = None
        self.s[key] = None
        save_settings(self.s)
        self._refresh_button_label()

    def _assign_button(self, button):
        key, self.assigning = self.assigning, None
        other = "toggle_button" if key == "aws_button" else "aws_button"
        if self.s[other] == button:
            self.s[other] = None          # one joystick button can't do both
        self.s[key] = button
        save_settings(self.s)
        self._refresh_button_label()
        what = "acknowledges AWS" if key == "aws_button" else "pauses / resumes the bridge"
        self.log(f"Button {button + 1} now {what}")

    def on_joystick(self, _event=None):
        self.s["joystick"] = self.joy_combo.get()
        save_settings(self.s)
        self.stick = None
        self.next_stick_try = 0.0

    def on_axis(self, _event=None):
        self.s["axis"] = self.axis_combo.current()
        save_settings(self.s)

    def on_look_enabled(self):
        self.s["look_enabled"] = bool(self.look_var.get())
        save_settings(self.s)
        self.log("Twist look " + ("on" if self.s["look_enabled"] else "off"))

    def on_look_axis(self, _event=None):
        self.s["look_axis"] = self.look_axis_combo.current()
        save_settings(self.s)

    def on_look_invert(self):
        self.s["look_invert"] = bool(self.look_invert_var.get())
        save_settings(self.s)

    def on_look_angle(self, value):
        self.s["look_angle"] = round(float(value))
        self.look_angle_label.config(text=f"±{self.s['look_angle']}°")

    def on_look_smoothing(self, value):
        self.s["look_smoothing"] = round(float(value), 2)
        self.smooth_label.config(text="off" if self.s["look_smoothing"] < 0.01
                                 else f"{self.s['look_smoothing']:.2f} s")

    def on_look_deadzone(self, value):
        self.s["look_deadzone"] = round(float(value), 3)
        self.look_dz_label.config(text=f"{self.s['look_deadzone'] * 100:.0f}%")

    def on_rev_enabled(self):
        self.s["rev_enabled"] = bool(self.rev_var.get())
        save_settings(self.s)
        self.log("Reverser control " + ("on" if self.s["rev_enabled"] else "off"))

    def on_rev_axis(self, _event=None):
        self.s["rev_axis"] = self.rev_axis_combo.current()
        save_settings(self.s)

    def on_rev_invert(self):
        self.s["rev_invert"] = bool(self.rev_invert_var.get())
        save_settings(self.s)

    def on_invert(self):
        self.s["invert"] = bool(self.invert_var.get())
        save_settings(self.s)

    def on_deadzone(self, value):
        self.s["deadzone"] = round(float(value), 3)
        self.dz_label.config(text=f"{self.s['deadzone'] * 100:.0f}%")

    def on_top(self):
        self.s["on_top"] = bool(self.top_var.get())
        self.root.attributes("-topmost", self.s["on_top"])
        save_settings(self.s)

    def on_redetect(self):
        self.bridge.force_detect = True
        self.log("Re-detecting train controls...")

    def close(self):
        save_settings(self.s)
        self.bridge.running = False
        self.look.running = False
        pygame.quit()
        self.root.destroy()

    # ---- joystick
    def _open_stick(self):
        pygame.joystick.quit()
        pygame.joystick.init()
        sticks = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
        self.joy_combo["values"] = [s.get_name() for s in sticks]
        if not sticks:
            return
        want = self.s.get("joystick") or ""
        chosen = (next((s for s in sticks if s.get_name() == want), None)
                  or next((s for s in sticks if core.JOYSTICK_NAME_HINT in s.get_name().lower()), None)
                  or sticks[0])
        self.stick = chosen
        self.joy_combo.set(chosen.get_name())
        n = chosen.get_numaxes()
        self.axis_combo["values"] = [f"Axis {i}" for i in range(n)]
        if self.s["axis"] < n:
            self.axis_combo.current(self.s["axis"])
        self.look_axis_combo["values"] = [f"Axis {i}" for i in range(n)]
        if self.s["look_axis"] < n:
            self.look_axis_combo.current(self.s["look_axis"])
        self.rev_axis_combo["values"] = [f"Axis {i}" for i in range(n)]
        if self.s["rev_axis"] < n:
            self.rev_axis_combo.current(self.s["rev_axis"])
        self.log(f"Joystick: {chosen.get_name()}")

    def _set_aws(self, down):
        if down != self.aws_held:
            self.aws_held = down
            self.bridge.aws_queue.put(down)

    def _poll_joystick(self):
        sid = self.stick.get_instance_id() if self.stick else None
        for ev in pygame.event.get():
            if ev.type == pygame.JOYDEVICEREMOVED and ev.instance_id == sid:
                self.stick = None
                self.log("Joystick disconnected")
                self._set_aws(False)
            elif ev.type == pygame.JOYDEVICEADDED and self.stick is None:
                self.next_stick_try = 0.0
            elif ev.type in (pygame.JOYBUTTONDOWN, pygame.JOYBUTTONUP) and ev.instance_id == sid:
                down = ev.type == pygame.JOYBUTTONDOWN
                if down and self.assigning:
                    self._assign_button(ev.button)
                elif ev.button == self.s["aws_button"]:
                    self._set_aws(down)
                elif down and ev.button == self.s["toggle_button"]:
                    self.toggle()

        if self.stick is None and time.time() >= self.next_stick_try:
            self.next_stick_try = time.time() + 1.0
            self._open_stick()
        if self.stick is None:
            self.bridge.y = None
            self.bridge.rev_zone = self.rev_zone = None
            self.look.twist = None
            return

        rev_axis = self.s["rev_axis"]
        if self.s["rev_enabled"] and rev_axis < self.stick.get_numaxes():
            raw = self.stick.get_axis(rev_axis)
            self.rev_s = raw if self.s["rev_invert"] else -raw
            self.rev_zone = core.reverser_zone(self.rev_s, self.rev_zone)
        else:
            self.rev_zone = None
        self.bridge.rev_zone = self.rev_zone

        look_axis = self.s["look_axis"]
        if self.s["look_enabled"] and look_axis < self.stick.get_numaxes():
            raw = self.stick.get_axis(look_axis)
            self.look_raw = -raw if self.s["look_invert"] else raw
            self.look.twist = core.shape_axis(self.look_raw, float(self.s["look_deadzone"]))
        else:
            self.look.twist = None
        self.look.max_angle = float(self.s["look_angle"])
        self.look.smoothing = float(self.s["look_smoothing"])
        self.look.enabled = self.bridge.enabled

        axis, invert, dz = self.s["axis"], bool(self.s["invert"]), float(self.s["deadzone"])
        raw = self.stick.get_axis(axis) if axis < self.stick.get_numaxes() else 0.0
        self.raw_y = raw if invert else -raw
        self.y = core.read_y(self.stick, axis, invert, dz)
        self.bridge.y = self.y

        if self.frame % 6 == 0:
            vals = "  ".join(f"{i}:{self.stick.get_axis(i):+.2f}" for i in range(self.stick.get_numaxes()))
            self.axes_live.config(text=vals)

    # ---- status
    def _set_status(self, key, level, text):
        dot, label, last = self.status[key]
        if last != (level, text):
            dot.itemconfig("dot", fill=DOT[level])
            label.config(text=text)
            self.status[key][2] = (level, text)

    def _update_status(self):
        if self.stick:
            self._set_status("joy", "ok", self.stick.get_name())
        else:
            self._set_status("joy", "bad", "Not found - plug in the joystick")
        self._set_status("game", *self.bridge.game_status)
        c = self.bridge.controls
        if c and (c.throttle or c.brake):
            self._set_status("train", "ok", pretty_train(self.bridge.train))
            parts = []
            if c.throttle and c.throttle.brake_end is not None and not c.brake:
                parts.append(f"One lever for power and brake ({pretty_lever(c.throttle.name)})")
            elif c.throttle:
                parts.append(f"Throttle: {pretty_lever(c.throttle.name)}")
            if c.brake:
                parts.append(f"Brake: {pretty_lever(c.brake.name)}")
            if any(lv.safe_lo > lv.lo or lv.safe_hi < lv.hi for lv in (c.throttle, c.brake) if lv):
                parts.append("emergency position blocked")
            if c.reverser and c.reverser.ok:
                parts.append(f"Reverser: {pretty_lever(c.reverser.name)}")
            else:
                parts.append("reverser not found")
            parts.append(f"AWS: {pretty_lever(c.aws.name)}" if c.aws else "AWS button not found")
            desc = "  ·  ".join(parts)
        elif c:
            self._set_status("train", "warn", f"{pretty_train(self.bridge.train)} - no throttle/brake found")
            desc = "Try Re-detect, or run tsw_joystick.py --list in this cab and share the output."
        else:
            self._set_status("train", "idle", "-")
            desc = ""
        if self.lever_desc.cget("text") != desc:
            self.lever_desc.config(text=desc)

    def _drain_log(self):
        lines = []
        while True:
            try:
                lines.append(self.logq.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        t = self.logtext
        t.config(state="normal")
        for line in lines:
            t.insert("end", line + "\n")
        excess = int(t.index("end-1c").split(".")[0]) - 200
        if excess > 0:
            t.delete("1.0", f"{excess + 1}.0")
        t.see("end")
        t.config(state="disabled")

    # ---- gauges
    def _draw(self):
        k, cv = self.k, self.canvas
        cv.delete("all")
        W, H = self.CW * k, self.CH * k
        top, bot = 34 * k, (self.GH - 36) * k
        small, smallb = (FONT, 8), (FONT, 8, "bold")
        c = self.bridge.controls
        live = (self.bridge.enabled and c is not None and self.stick is not None
                and self.bridge.game_status[0] == "ok")
        mark = GOOD if live else MUTED

        # stick gauge
        x, w = 22 * k, 26 * k
        mid, half = (top + bot) / 2, (bot - top) / 2
        cv.create_text(x + w / 2, 14 * k, text="STICK", fill=MUTED, font=smallb)
        cv.create_rectangle(x, top, x + w, bot, fill=TRACK, outline="")
        dz = float(self.s["deadzone"]) * half
        cv.create_rectangle(x, mid - dz, x + w, mid + dz, fill="#363c47", outline="")
        cv.create_text(x + w + 6 * k, top, text="Fwd", anchor="nw", fill=MUTED, font=small)
        cv.create_text(x + w + 6 * k, bot, text="Back", anchor="sw", fill=MUTED, font=small)
        if self.stick:
            py = mid - max(-1.0, min(1.0, self.raw_y)) * half
            if self.y:
                cv.create_rectangle(x, min(mid, py), x + w, max(mid, py),
                                    fill=POWER if self.y > 0 else BRAKE, outline="")
            cv.create_line(x - 5 * k, py, x + w + 5 * k, py, fill=FG, width=max(2, round(2 * k)))

        # lever gauges
        items = []
        if c and c.throttle:
            combined = c.throttle.brake_end is not None and not c.brake
            items.append((c.throttle, "combined" if combined else "throttle"))
        if c and c.brake:
            items.append((c.brake, "brake"))
        targets = c.targets(self.y) if c else {}
        for i, (lever, role) in enumerate(items):
            self._draw_lever(lever, role, targets.get(lever), (135 + i * 150) * k, top, bot, mark)
        if not items:
            msg = "Waiting for a train..." if c is None else "No throttle/brake found on this train"
            cv.create_text(255 * k, H / 2, text=msg, fill=MUTED, font=(FONT, 10))

        # readout
        rx = W - 14 * k
        if self.stick is None:
            text, color = "NO STICK", MUTED
        elif self.y > 0:
            text, color = f"POWER {self.y * 100:.0f}%", POWER
        elif self.y < 0:
            text, color = f"BRAKE {-self.y * 100:.0f}%", BRAKE
        else:
            text, color = "NEUTRAL", FG
        cv.create_text(rx, 62 * k, text=text, anchor="e", fill=color, font=(FONT, 17, "bold"))
        if not self.bridge.enabled:
            sub = "Paused - not sending"
        elif live:
            sub = "Sending to game"
        else:
            sub = "Preview - not connected"
        cv.create_text(rx, 88 * k, text=sub, anchor="e", fill=mark, font=(FONT, 9))
        self._draw_reverser(rx, 130 * k, live)
        self._draw_look(W)

    def _draw_look(self, W):
        """Horizontal bar: white line = where the twist points, green marker = where the view is."""
        k, cv, look = self.k, self.canvas, self.look
        y = (self.GH + 4) * k
        x0, x1 = 22 * k, W - 14 * k
        span = max(30.0, float(self.s["look_angle"]))
        px = lambda angle: x0 + (max(-span, min(span, angle)) + span) / (2 * span) * (x1 - x0)

        cv.create_text(x0, y, text="LOOK", anchor="w", fill=MUTED, font=(FONT, 8, "bold"))
        if not self.s["look_enabled"]:
            status = "twist look off"
        else:
            status = look.state
            if look.yaw is not None and look.state not in ("off", "no game"):
                side = "straight ahead" if abs(look.yaw) < 1 else \
                    f"{abs(look.yaw):.0f}° {'right' if look.yaw > 0 else 'left'}"
                status = f"view {side}  ·  {look.state}"
        warn = look.state in ("game not focused", "cursor active", "at view limit")
        cv.create_text(x1, y, text=status, anchor="e", fill=WARN if warn else MUTED, font=(FONT, 8))

        by = y + 10 * k
        bh = 10 * k
        cv.create_rectangle(x0, by, x1, by + bh, fill=TRACK, outline="")
        cv.create_line(px(0), by - 2 * k, px(0), by + bh + 2 * k, fill=MUTED)
        cv.create_text(x0, by + bh + 8 * k, text="Left", anchor="w", fill=MUTED, font=(FONT, 7))
        cv.create_text(x1, by + bh + 8 * k, text="Right", anchor="e", fill=MUTED, font=(FONT, 7))
        if self.stick and self.s["look_enabled"] and look.twist is not None:
            tx = px(look.twist * span)
            if look.twist:
                cv.create_rectangle(min(px(0), tx), by, max(px(0), tx), by + bh, fill="#3d4a5c", outline="")
            cv.create_line(tx, by - 3 * k, tx, by + bh + 3 * k, fill=FG, width=max(2, round(2 * k)))
        if look.yaw is not None and look.state not in ("off", "no game"):
            vx = px(look.yaw)
            cv.create_polygon(vx - 6 * k, by - 7 * k, vx + 6 * k, by - 7 * k, vx, by,
                              fill=GOOD if look.steering else MUTED, outline="")

    def _draw_reverser(self, rx, y, live):
        """R / N / F pills: filled = where the train's reverser is, outlined = where the slider is."""
        k, cv = self.k, self.canvas
        c = self.bridge.controls
        pw, ph, gap = 40 * k, 26 * k, 6 * k
        left = rx - 3 * pw - 2 * gap
        cv.create_text(left, y, text="REVERSER", anchor="w", fill=MUTED, font=(FONT, 8, "bold"))
        y += 12 * k
        actual = self.bridge.rev_actual
        slider = self.rev_zone
        for i, (pos, letter) in enumerate((("reverse", "R"), ("neutral", "N"), ("forward", "F"))):
            x0 = left + i * (pw + gap)
            is_actual = actual == pos
            fill = (GOOD if live else MUTED) if is_actual else TRACK
            outline = FG if slider == pos else ""
            cv.create_rectangle(x0, y, x0 + pw, y + ph, fill=fill, outline=outline,
                                width=max(2, round(2 * k)))
            cv.create_text(x0 + pw / 2, y + ph / 2, text=letter, font=(FONT, 10, "bold"),
                           fill="#0b1a12" if is_actual else FG)
        y += ph + 14 * k

        if not self.s["rev_enabled"]:
            note, color = "Slider control off", MUTED
        elif c is None:
            note, color = "", MUTED
        elif not (c.reverser and c.reverser.ok):
            note, color = "No reverser found on this train", WARN
        elif slider is None:
            note, color = "Slider axis not available", WARN
        elif actual not in (None, "forward", "neutral", "reverse"):
            note, color = f"Train reverser: {actual.title()}", MUTED
        elif actual and slider != actual and abs(self.bridge.speed or 0) > core.REVERSER_MAX_SPEED:
            note, color = "Locked while moving", WARN
        elif actual and slider != actual:
            note, color = "Move the slider to apply", MUTED
        else:
            note, color = "", MUTED
        if note:
            cv.create_text(rx, y, text=note, anchor="e", fill=color, font=(FONT, 9))

        # AWS: lights up while the AWS joystick button is held
        y += 18 * k
        width = 3 * pw + 2 * gap
        has_aws = c is not None and c.aws is not None
        button = self.s["aws_button"]
        if button is None:
            text = "AWS  -  no button set"
        elif c is not None and not has_aws:
            text = "AWS  -  not on this train"
        else:
            text = f"AWS  -  button {button + 1}"
        lit = self.aws_held and button is not None
        cv.create_rectangle(rx - width, y, rx, y + ph, outline="",
                            fill=(GOOD if live and has_aws else MUTED) if lit else TRACK)
        cv.create_text(rx - width / 2, y + ph / 2, text=text, font=(FONT, 9, "bold"),
                       fill="#0b1a12" if lit else (FG if has_aws else MUTED))

    def _draw_lever(self, lever, role, value, x, top, bot, mark):
        k, cv = self.k, self.canvas
        w = 26 * k
        small, smallb = (FONT, 8), (FONT, 8, "bold")
        if role == "brake":
            tv, bv = lever.lo, lever.hi                      # release at top, full brake at bottom
        else:
            brake_low = lever.brake_end is None or lever.brake_end <= lever.neutral
            tv, bv = (lever.hi, lever.lo) if brake_low else (lever.lo, lever.hi)

        def py(v):
            return top + (v - tv) / (bv - tv) * (bot - top) if bv != tv else top

        title = {"combined": "POWER / BRAKE", "throttle": "THROTTLE", "brake": "TRAIN BRAKE"}[role]
        cv.create_text(x + w / 2, 14 * k, text=title, fill=MUTED, font=smallb)
        cv.create_text(x + w / 2, bot + 16 * k, text=pretty_lever(lever.name), fill=MUTED, font=small)
        cv.create_rectangle(x, top, x + w, bot, fill=TRACK, outline="")

        # range the joystick is never allowed to reach (emergency)
        for a, b in ((lever.lo, lever.safe_lo), (lever.safe_hi, lever.hi)):
            if b - a > 1e-6:
                y1, y2 = sorted((py(a), py(b)))
                cv.create_rectangle(x, y1, x + w, y2, fill=BAD, stipple="gray50", outline="")

        rest = lever.safe_lo if role == "brake" else lever.neutral
        if value is not None and abs(value - rest) > 1e-4:
            braking = role == "brake" or (lever.brake_end is not None
                                          and (value - lever.neutral) * (lever.brake_end - lever.neutral) > 0)
            y1, y2 = sorted((py(rest), py(value)))
            cv.create_rectangle(x, y1, x + w, y2, fill=BRAKE if braking else POWER, outline="")
        if role == "combined":
            cv.create_line(x - 4 * k, py(lever.neutral), x + w + 4 * k, py(lever.neutral),
                           fill=FG, dash=(3, 2))

        # notch labels; Off is drawn at the detected neutral point (more reliable than the estimate)
        used = []
        labels = []
        if role != "brake":
            labels.append((py(lever.neutral), "Off", MUTED))
        for a, b, label, _ in lever.zones:
            if a is None or label.strip() in core.NEUTRAL_LABELS:
                continue
            centre = (max(a, lever.lo) + min(b, lever.hi)) / 2
            text = pretty_zone(label)
            if text and lever.lo <= centre <= lever.hi:
                labels.append((py(centre), text, BAD if core.is_emergency(label) else MUTED))
        for yy, text, color in labels:
            if any(abs(yy - u) < 12 * k for u in used):
                continue
            used.append(yy)
            cv.create_text(x + w + 7 * k, yy, text=text, anchor="w", fill=color, font=small)

        if value is not None:
            yy = py(value)
            cv.create_line(x - 5 * k, yy, x + w + 5 * k, yy, fill=mark, width=max(2, round(2 * k)))
            cv.create_polygon(x - 13 * k, yy - 6 * k, x - 5 * k, yy, x - 13 * k, yy + 6 * k,
                              fill=mark, outline="")

    # ---- main loop
    def tick(self):
        self.frame += 1
        try:
            self._poll_joystick()
            self._update_status()
            self._draw()
            self._drain_log()
        except Exception as e:
            self.log(f"UI error: {e}")
        self.root.after(33, self.tick)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.title("TSW7 Joystick Bridge")
    root.resizable(False, False)
    App(root, start_paused="--paused" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # pythonw has no console: leave a trace next to the script
        with open(os.path.join(HERE, "error.log"), "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        raise
