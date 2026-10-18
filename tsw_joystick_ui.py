"""
Train Sim World 7 joystick bridge - desktop window.

Shows joystick / game / train status, live stick and lever gauges, and settings.
The bridge itself (game API, lever detection, emergency exclusion) lives in tsw_joystick.py.

Usage:
    pythonw tsw_joystick_ui.py               (or double-click Start TSW Joystick.bat)
    pythonw tsw_joystick_ui.py --paused      start with the bridge paused
    pythonw tsw_joystick_ui.py --with-game   close the window once the game exits (how tsw_autostart.py opens it)
"""

import ctypes
import ctypes.wintypes as wintypes
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

import tsw_autostart as autostart
import tsw_joystick as core   # sets SDL env vars before pygame is imported
import pygame

try:
    import winsound
except ImportError:
    winsound = None

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "settings.json")
LOG_FILE = os.path.join(HERE, "bridge.log")    # the log, to read after; the one before kept as bridge.log.1
LOG_MAX = 1_000_000                            # bytes: past this the log starts a new file
DEFAULTS = {"axis": core.Y_AXIS, "invert": core.INVERT_Y, "deadzone": core.DEADZONE,
            "toggle_button": None, "on_top": False,
            "joystick": "", "rev_enabled": core.USE_REVERSER, "rev_axis": core.REVERSER_AXIS,
            "rev_invert": core.REVERSER_INVERT, "aws_button": core.AWS_BUTTON,
            "alerter_button": core.ALERTER_BUTTON,
            "door_open_left_button": core.DOOR_OPEN_LEFT_BUTTON,
            "door_open_right_button": core.DOOR_OPEN_RIGHT_BUTTON,
            "door_close_left_button": core.DOOR_CLOSE_LEFT_BUTTON,
            "door_close_right_button": core.DOOR_CLOSE_RIGHT_BUTTON,
            "look_enabled": core.USE_LOOK, "look_axis": core.LOOK_AXIS, "look_invert": core.LOOK_INVERT,
            "look_deadzone": core.LOOK_DEADZONE, "look_angle": core.LOOK_MAX_ANGLE,
            "look_smoothing": core.LOOK_SMOOTHING}

BG, PANEL, TRACK = "#16181d", "#1f232b", "#2b3039"
FG, MUTED = "#e6e8ec", "#8a919e"
GOOD, WARN, BAD = "#4cc38a", "#e5b54a", "#e5534b"
POWER, BRAKE = "#4c9be8", "#e08a3c"
FONT = "Segoe UI"
DOT = {"ok": GOOD, "warn": WARN, "bad": BAD, "idle": MUTED}

# joystick button settings: (title, what the button does); one joystick button can't do two of these
BUTTON_ACTIONS = {"aws_button": ("AWS button", "acknowledges AWS"),
                  "alerter_button": ("Alerter button", "acknowledges alerter / DSD / SIFA"),
                  "door_open_left_button": ("Open left doors", "opens the left doors"),
                  "door_open_right_button": ("Open right doors", "opens the right doors"),
                  "door_close_left_button": ("Close left doors", "closes the left doors"),
                  "door_close_right_button": ("Close right doors", "closes the right doors"),
                  "toggle_button": ("Pause button", "pauses / resumes")}
# cab buttons held down for as long as their joystick button is held: setting -> TrainControls attribute
CAB_BUTTONS = {"aws_button": "aws", "alerter_button": "alerter",
               "door_open_left_button": "door_open_left", "door_open_right_button": "door_open_right",
               "door_close_left_button": "door_close_left", "door_close_right_button": "door_close_right"}
LIGHTS = {"aws_button": "AWS", "alerter_button": "Alerter"}    # shown in the live view while held
REDRAW_SECONDS = 1.0          # the window's gauges are drawn again at least this often while it can be seen
BUTTON_NAMES = {"aws": "AWS", "alerter": "alerter", "door_open_left": "open left doors",
                "door_open_right": "open right doors", "door_close_left": "close left doors",
                "door_close_right": "close right doors"}           # cab buttons, as the log names them
BUTTON_AGAIN_SECONDS = 5.0     # a cab button press the game doesn't answer is made again for this long


def apply_style(r):
    """The windows' dark look (the bridge, the route maps)."""
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


def panel(parent, title):
    """A titled panel, as in the bridge window."""
    p = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 10))
    p.pack(fill="x", pady=(0, 10))
    ttk.Label(p, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 6))
    return p


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    used = set()
    for key in BUTTON_ACTIONS:         # one joystick button can't do two things
        if s[key] in used:
            s[key] = None
        elif s[key] is not None:
            used.add(s[key])
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
    s = re.sub(r"^(RVM|BP)_|_RVM(?=_C$)", "", str(cls))     # the Class 70 is CL70_FL_RVM_C
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
        core.load_game_files()        # read the game's file index in the background, for exact handle places
        self.controls = None          # TrainControls, replaced as a whole on train change
        self.train = None
        self.game_status = ("idle", "Starting...")
        self.enabled = True
        self.y = None                 # latest stick value from the UI thread, None = no joystick
        self.force_detect = False
        self.redetects = 0            # times the train was looked at again for a handle the game didn't answer for
        self.running = True
        self.last_sent = None
        self.last_check = 0.0
        self.rev_zone = None          # slider zone from the UI thread, None = reverser control off
        self.rev_sync = core.ReverserSync(log)
        self.rev_actual = None        # train's current reverser notch, for display
        self.speed = None             # m/s, for display
        self.last_poll = 0.0
        self.button_queue = queue.Queue()   # (cab button "aws" / "door_open_left" etc., pressed) from the UI
        self.button_again = None      # (button, pressed, until): a press the game didn't answer, made again
        self.send_failed = False      # the stick's last send got no answer: sent again until it does

    def set_game(self, level, text):
        if (level, text) != self.game_status:
            self.game_status = (level, text)
            self.log(f"Game: {text}")

    def drop_train(self):
        if self.controls is not None:
            self.controls.let_go()
        self.controls = None
        self.train = None
        self.last_sent = None
        self.rev_sync.reset()
        self.rev_actual = self.speed = None

    def _handle_buttons(self, controls):
        while True:
            if self.button_again is not None:
                (name, down, until), self.button_again = self.button_again, None
            else:
                try:
                    name, down = self.button_queue.get_nowait()
                except queue.Empty:
                    return
                until = time.monotonic() + BUTTON_AGAIN_SECONDS
            if controls is None:
                continue
            if down and not self.enabled:
                continue              # paused: ignore presses, but always pass releases through
            try:
                message = controls.press(name, down)
            except Exception as e:
                if not core.no_answer(e):
                    raise
                if time.monotonic() < until:
                    # the game stalled (it does as it saves on arriving at a stop): press or let go again next
                    # time round, before anything pressed since (an AWS acknowledge lost would brake the train)
                    self.button_again = (name, down, until)
                    return
                self.log(f"The game didn't answer: the {BUTTON_NAMES[name]} button wasn't "
                         f"{'pressed' if down else 'let go'}")
                continue
            if message:
                self.log(message)

    def run(self):
        try:
            while self.running:
                time.sleep(1.0 / core.POLL_HZ)
                try:
                    self.step()
                except Exception as e:
                    self.set_game("bad", f"Error: {e}")
                    self.drop_train()
                    time.sleep(1)
        finally:
            if self.controls is not None:
                self.controls.let_go()        # never leave a handle held when the bridge stops

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
            again = (train == self.train and not force and self.controls is not None and self.controls.missing
                     and self.redetects < core.REDETECTS)
            if train != self.train or force or again or (self.controls is not None and self.controls.cab_changed()):
                if again:
                    self.redetects += 1
                    self.log(f"No answer from {', '.join(self.controls.missing)} - looking at the train again")
                else:
                    self.redetects = 0
                if self.controls is not None:
                    self.controls.let_go()
                controls = core.TrainControls(self.api, log=self.log)
                controls.train_id = train
                controls.detect()
                self.controls, self.train, self.last_sent = controls, train, None
                self.rev_sync.reset()
                self.log(f"Train: {pretty_train(train)} -> {controls.describe()}")

        controls = self.controls
        self._handle_buttons(controls)
        if controls is None:
            return
        if now - self.last_poll > 0.5:
            self.last_poll = now
            self.speed = controls.speed()
            try:
                self.rev_actual = controls.reverser.position() if controls.reverser else None
            except Exception as e:
                if not core.no_answer(e):
                    raise                 # (no answer: the game stalled, read it next time round)

        if not self.enabled:
            controls.let_go()
            self.last_sent = None
            self.rev_sync.reset()
            return
        self.rev_sync.update(controls, self.rev_zone)

        y = self.y
        if y is None:
            controls.let_go()
            self.last_sent = None
            return
        if self.last_sent is None:
            self.last_sent = y        # don't snap levers until the stick actually moves
            self.send_failed = False
            return
        if abs(y - self.last_sent) < core.SEND_THRESHOLD and not self.send_failed:
            return                    # stick not moved: leave levers alone so the keyboard still works
        try:
            controls.apply(y)
        except Exception as e:
            if not core.no_answer(e):
                self.log(f"Send failed: {e}")
                self.drop_train()
                return
            # the game stalled (it does as it saves on arriving at a stop): keep the train and send where the stick
            # is again next time round, even if it's back where it was (the send may have got there). Dropping the
            # train left the handle where it was until the stick moved again, which can be as you ease the brake
            if not self.send_failed:
                self.log(f"Send failed ({e}) - sending again")
            self.send_failed = True
            return
        if self.send_failed:
            self.log("Sent again")
        self.send_failed = False
        self.last_sent = y


def window_hidden(hwnd):
    """Whether a window can't be seen: minimised, or wholly under one window of another program (the game, full
    screen). A window that may be see-through doesn't count as covering it: layered or click-through ones (a
    graphics driver's overlay can lie over the whole screen) and ones Windows keeps out of sight (cloaked)."""
    user32 = ctypes.windll.user32
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    if user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
        return True
    mine, other, pid, cloaked = wintypes.RECT(), wintypes.RECT(), wintypes.DWORD(), wintypes.DWORD()
    user32.GetWindowRect(hwnd, ctypes.byref(mine))
    h = user32.GetWindow(hwnd, 3)                 # GW_HWNDPREV: the window just above, and so on up
    while h:
        if user32.IsWindowVisible(h) and not user32.GetWindowLongW(h, -20) & 0x00080020:   # layered, click-through
            user32.GetWindowRect(h, ctypes.byref(other))
            if (other.left <= mine.left and other.top <= mine.top
                    and other.right >= mine.right and other.bottom >= mine.bottom):
                user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                ctypes.windll.dwmapi.DwmGetWindowAttribute(h, 14, ctypes.byref(cloaked), 4)   # DWMWA_CLOAKED
                if pid.value != os.getpid() and not cloaked.value:
                    return True
        h = user32.GetWindow(h, 3)
    return False


# ---------------------------------------------------------------- window
class App:
    CW, CH = 560, 290    # canvas size at 96 dpi
    GH = 250             # height of the gauge area; the look bar sits below it

    def __init__(self, root, start_paused=False, with_game=False):
        self.root = root
        self.s = load_settings()
        self.logq = queue.Queue()
        self.last_log = None
        self.log_file = None          # bridge.log, opened with the first line
        self.bridge = Bridge(self.log)
        self.bridge.enabled = not start_paused
        self.look = core.LookController(self.log)
        self.look_raw = 0.0           # twist position, +1 = full right
        self.stick = None
        self.next_stick_try = 0.0
        self.assigning = None         # settings key waiting for a joystick button press
        self.held = {name: False for name in CAB_BUTTONS.values()}   # cab buttons held down now
        self.raw_y = 0.0
        self.y = 0.0
        self.y_filter = core.AxisFilter()
        self.follow = core.HandleFollower()   # moves the handle after the stick at the train's handle speed
        self.hs_train = None          # train the handle speed setting shows ("" = not in a train)
        self._hs_quiet = False        # the slider is being set to the train's value, not by you
        self.rev_s = 0.0              # slider position, +1 = forward end
        self.rev_zone = None
        self.frame = 0
        self.drawn, self.drawn_at = None, 0.0     # what the gauges last showed (_draw_state), and when
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
        if with_game:
            self.log("Opened with the game - closes when the game exits")
            self.game_seen = time.monotonic()
            self._close_with_game()

    # ---- layout
    def _style(self):
        apply_style(self.root)

    def _panel(self, parent, title):
        return panel(parent, title)

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

        ttk.Label(grid, text="Handle speed", style="Muted.TLabel").grid(row=3, column=0, sticky="w",
                                                                       pady=3, padx=(0, 14))
        hs_row = ttk.Frame(grid, style="Panel.TFrame")
        hs_row.grid(row=3, column=1, columnspan=3, sticky="w", pady=3)
        ttk.Label(hs_row, text="With stick", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.hs_scale = ttk.Scale(hs_row, from_=0.0, to=core.HANDLE_SECONDS_MAX, length=round(180 * self.k),
                                  value=0.0, command=self.on_handle_speed)
        self.hs_scale.pack(side="left")
        self.hs_scale.bind("<ButtonRelease-1>", self.on_handle_speed_done)
        ttk.Label(hs_row, text="Slow", style="Muted.TLabel").pack(side="left", padx=(6, 10))
        self.hs_label = ttk.Label(hs_row, text="", style="Panel.TLabel")
        self.hs_label.pack(side="left")
        self._sync_handle_speed()

        ttk.Label(grid, text="Reverser", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=3, padx=(0, 14))
        rev_row = ttk.Frame(grid, style="Panel.TFrame")
        rev_row.grid(row=4, column=1, columnspan=3, sticky="w", pady=3)
        self.rev_var = tk.BooleanVar(value=bool(self.s["rev_enabled"]))
        ttk.Checkbutton(rev_row, text="Use slider", variable=self.rev_var, style="Panel.TCheckbutton",
                        command=self.on_rev_enabled).pack(side="left", padx=(0, 12))
        self.rev_axis_combo = ttk.Combobox(rev_row, state="readonly", width=8)
        self.rev_axis_combo.pack(side="left")
        self.rev_axis_combo.bind("<<ComboboxSelected>>", self.on_rev_axis)
        self.rev_invert_var = tk.BooleanVar(value=bool(self.s["rev_invert"]))
        ttk.Checkbutton(rev_row, text="Invert", variable=self.rev_invert_var, style="Panel.TCheckbutton",
                        command=self.on_rev_invert).pack(side="left", padx=(12, 0))

        ttk.Label(grid, text="Look", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=3, padx=(0, 14))
        look_row = ttk.Frame(grid, style="Panel.TFrame")
        look_row.grid(row=5, column=1, columnspan=3, sticky="w", pady=3)
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

        ttk.Label(grid, text="Look smoothing", style="Muted.TLabel").grid(row=6, column=0, sticky="w",
                                                                         pady=3, padx=(0, 14))
        smooth_row = ttk.Frame(grid, style="Panel.TFrame")
        smooth_row.grid(row=6, column=1, columnspan=3, sticky="w", pady=3)
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
        for row, (key, (title, _)) in enumerate(BUTTON_ACTIONS.items(), start=7):
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
        for key, label in self.btn_labels.items():
            if self.assigning == key:
                text = "Press a joystick button..."
            elif self.s[key] is None:
                text = "Not set"
            else:
                text = f"Button {self.s[key] + 1} {BUTTON_ACTIONS[key][1]}"
            label.config(text=text)

    def on_assign(self, key):
        self.assigning = key
        self._refresh_button_label()

    def on_clear_button(self, key):
        self.assigning = None
        self._release_all()
        self.s[key] = None
        save_settings(self.s)
        self._refresh_button_label()

    def _assign_button(self, button):
        key, self.assigning = self.assigning, None
        self._release_all()               # its release would no longer reach the cab button
        for other in BUTTON_ACTIONS:
            if other != key and self.s[other] == button:
                self.s[other] = None      # one joystick button can't do two things
        self.s[key] = button
        save_settings(self.s)
        self._refresh_button_label()
        self.log(f"Button {button + 1} now {BUTTON_ACTIONS[key][1]}")

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

    def on_handle_speed(self, value):
        if self._hs_quiet:
            return
        seconds = round(float(value), 1)
        self.follow.seconds = seconds if seconds >= 0.1 else 0.0
        self._show_handle_speed()

    def on_handle_speed_done(self, _event=None):
        if self.hs_train:
            core.save_handle_seconds(self.hs_train, self.follow.seconds)
            self.log(f"Handle speed on {pretty_train(core.train_family(self.hs_train))}: "
                     + (f"{self.follow.seconds:.1f} s from Off to full" if self.follow.seconds
                        else "follows the stick at once"))

    def _sync_handle_speed(self):
        """Show and use the handle speed of the train you're in, each train having its own."""
        train = self.bridge.train or ""
        if train == self.hs_train:
            return
        self.hs_train = train
        self.follow.seconds = core.handle_seconds(train) if train else core.HANDLE_SECONDS
        self._hs_quiet = True
        try:
            self.hs_scale.state(["!disabled"])        # a disabled slider ignores being set
            self.hs_scale.set(self.follow.seconds)
        finally:
            self._hs_quiet = False
        if not train:
            self.hs_scale.state(["disabled"])
        self._show_handle_speed()

    def _show_handle_speed(self):
        seconds = self.follow.seconds
        text = f"{seconds:.1f} s from Off to full" if seconds else "instant"
        if self.hs_train:
            text += f"  ·  {pretty_train(core.train_family(self.hs_train))}"
        else:
            text += "  ·  set in a train"
        self.hs_label.config(text=text)

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

    def _close_with_game(self):
        now = time.monotonic()
        if autostart.game_running():
            self.game_seen = now
        elif now - self.game_seen > autostart.GONE_SECONDS:
            self.close()
            return
        self.root.after(autostart.CHECK_SECONDS * 1000, self._close_with_game)

    def close(self):
        save_settings(self.s)
        self.bridge.running = False
        self.bridge.join(timeout=3)   # lets go of any handle it's holding
        self.look.running = False
        self.log("Bridge closed")
        self._drain_log()
        if self.log_file is not None:
            self.log_file.close()
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

    def _set_held(self, name, down):
        if down != self.held[name]:
            self.held[name] = down
            self.bridge.button_queue.put((name, down))

    def _release_all(self):
        for name in self.held:
            self._set_held(name, False)

    def _poll_joystick(self):
        sid = self.stick.get_instance_id() if self.stick else None
        for ev in pygame.event.get():
            if ev.type == pygame.JOYDEVICEREMOVED and ev.instance_id == sid:
                self.stick = None
                self.log("Joystick disconnected")
                self._release_all()
            elif ev.type == pygame.JOYDEVICEADDED and self.stick is None:
                self.next_stick_try = 0.0
            elif ev.type in (pygame.JOYBUTTONDOWN, pygame.JOYBUTTONUP) and ev.instance_id == sid:
                down = ev.type == pygame.JOYBUTTONDOWN
                cab_button = next((name for key, name in CAB_BUTTONS.items() if ev.button == self.s[key]), None)
                if down and self.assigning:
                    self._assign_button(ev.button)
                elif cab_button:
                    self._set_held(cab_button, down)
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
        self.y = self.follow.update(self.y_filter.update(core.read_y(self.stick, axis, invert, dz)))
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
            parts.append(f"Alerter: {pretty_lever(c.alerter.name)}" if c.alerter
                         else "alerter / DSD / SIFA not found")
            doors = [f"{action} {side}" for action in ("open", "close") for side in ("left", "right")
                     if getattr(c, f"door_{action}_{side}")]
            parts.append("Doors: open / close both sides" if len(doors) == 4
                         else f"Doors: {', '.join(doors)} only" if doors else "door buttons not found")
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
        self._write_log(lines)
        t = self.logtext
        t.config(state="normal")
        for line in lines:
            t.insert("end", line + "\n")
        excess = int(t.index("end-1c").split(".")[0]) - 200
        if excess > 0:
            t.delete("1.0", f"{excess + 1}.0")
        t.see("end")
        t.config(state="disabled")

    def _write_log(self, lines):
        """The log also goes to bridge.log, so what happened can be read after (the window keeps 200 lines)."""
        try:
            if self.log_file is None:
                if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX:
                    os.replace(LOG_FILE, LOG_FILE + ".1")
                self.log_file = open(LOG_FILE, "a", encoding="utf-8")
                self.log_file.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}  bridge started\n")
            for line in lines:
                self.log_file.write(line + "\n")
            self.log_file.flush()
            if self.log_file.tell() > LOG_MAX:
                self.log_file.close()
                self.log_file = None          # the next line starts a new file
        except OSError:
            self.log_file = None

    # ---- gauges
    def _redraw(self):
        """The gauges drawn again only when something on them has changed (and at least once a second, for
        anything _draw_state leaves out), and not while the window can't be seen: drawing them each tick cost
        the bridge most of its 15% of a core while the game covered it."""
        if window_hidden(int(self.root.wm_frame(), 16)):
            self.drawn = None                     # drawn as soon as it can be seen again
            return
        state, now = self._draw_state(), time.monotonic()
        if state == self.drawn and now - self.drawn_at < REDRAW_SECONDS:
            return
        self.drawn, self.drawn_at = state, now
        self._draw()

    def _draw_state(self):
        """What the gauges show, to tell whether they need drawing again: the stick, the handles it's moving,
        the reverser, the buttons held, the twist look and the settings drawn."""
        c, b, look = self.bridge.controls, self.bridge, self.look
        levers = tuple((lv.name, lv.neutral, lv.brake_end, lv.safe_lo, lv.safe_hi)
                       for lv in (c.throttle, c.brake) if lv) if c else ()
        return (id(c), levers, b.enabled, b.game_status[0], self.stick is not None, round(self.raw_y, 3),
                round(self.y, 3), b.rev_actual, self.rev_zone, bool(c and c.reverser and c.reverser.ok),
                abs(b.speed or 0) > core.REVERSER_MAX_SPEED, tuple(sorted(self.held.items())), look.state,
                None if look.yaw is None else round(look.yaw, 1), None if look.twist is None else round(look.twist, 3),
                look.steering, tuple(self.s[key] for key in ("deadzone", "look_angle", "look_enabled", "rev_enabled",
                                                              *LIGHTS)))

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

        # AWS and alerter: light up while their joystick button is held
        y += 14 * k
        width, bh = 170 * k, 22 * k
        for key, title in LIGHTS.items():
            name, title = CAB_BUTTONS[key], title.upper()
            found = c is not None and getattr(c, name) is not None
            button = self.s[key]
            if button is None:
                text = f"{title}  -  no button set"
            elif c is not None and not found:
                text = f"{title}  -  not on this train"
            else:
                text = f"{title}  -  button {button + 1}"
            lit = self.held[name] and button is not None
            cv.create_rectangle(rx - width, y, rx, y + bh, outline="",
                                fill=(GOOD if live and found else MUTED) if lit else TRACK)
            cv.create_text(rx - width / 2, y + bh / 2, text=text, font=(FONT, 9, "bold"),
                           fill="#0b1a12" if lit else (FG if found else MUTED))
            y += bh + 4 * k

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
            self._sync_handle_speed()
            self._poll_joystick()
            self._update_status()
            self._redraw()
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
    root.title(autostart.BRIDGE_TITLE)
    root.resizable(False, False)
    App(root, start_paused="--paused" in sys.argv, with_game="--with-game" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # pythonw has no console: leave a trace next to the script
        with open(os.path.join(HERE, "error.log"), "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        raise
