"""
Train Sim World 7 joystick bridge - desktop window.

Shows joystick / game / train status, live stick and lever gauges, and settings. While its joystick button or
its key (F6) is held, it shows over the game how far the next stop is (tsw_stops.py) and the speed limit now;
while you're stopped at a stop, whether to wait, shut the doors or go, with the times, your points (tsw_score.py)
and how the ride felt to the passengers, and at the end of a service how the run went against your best before
(tsw_history.py, all runs in the Service history window); while you're over the speed limit, the limit
(tsw_speed.py); while braking or speeding up is firm or harsh for the passengers, and in the last seconds of
a hard stop to ease the brake off before the train stops (tsw_comfort.py); and at a press of another button or
key (F7), a small see-through map of where you are on the service's line, following you, with how far the next
stop is and a ring on it (tsw_livemap.py).
The bridge itself (game API, lever detection, emergency exclusion) lives in tsw_joystick.py.

Usage:
    pythonw tsw_joystick_ui.py               (or double-click Start TSW Joystick.bat)
    pythonw tsw_joystick_ui.py --paused      start with the bridge paused
    pythonw tsw_joystick_ui.py --with-game   close the window once the game exits (how tsw_autostart.py opens it)
"""

import ctypes
import ctypes.wintypes as wintypes
import datetime
import json
import math
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
import tsw_comfort
import tsw_history
import tsw_livemap
import tsw_speed
import tsw_stops
import tsw_units
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
            "toggle_button": None, "on_top": False, "speed_popup": True, "stop_panel": True, "comfort": True,
            "joystick": "", "rev_enabled": core.USE_REVERSER, "rev_axis": core.REVERSER_AXIS,
            "rev_invert": core.REVERSER_INVERT, "aws_button": core.AWS_BUTTON,
            "alerter_button": core.ALERTER_BUTTON,
            "door_open_left_button": core.DOOR_OPEN_LEFT_BUTTON,
            "door_open_right_button": core.DOOR_OPEN_RIGHT_BUTTON,
            "door_close_left_button": core.DOOR_CLOSE_LEFT_BUTTON,
            "door_close_right_button": core.DOOR_CLOSE_RIGHT_BUTTON,
            "stop_button": core.STOP_BUTTON, "stop_key": "F6",
            "map_button": core.MAP_BUTTON, "map_key": "F7", "map_opacity": 0.7, "map_window": 0.27,
            "map_zoom": tsw_livemap.ZOOM, "map_auto_zoom": True, "map_corner": "Bottom left",
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
                  "stop_button": ("Next stop", "shows the next stop while held"),
                  "map_button": ("Route map", "shows / hides the route map"),
                  "toggle_button": ("Pause button", "pauses / resumes")}
# cab buttons held down for as long as their joystick button is held: setting -> TrainControls attribute
CAB_BUTTONS = {"aws_button": "aws", "alerter_button": "alerter",
               "door_open_left_button": "door_open_left", "door_open_right_button": "door_open_right",
               "door_close_left_button": "door_close_left", "door_close_right_button": "door_close_right"}
LIGHTS = {"aws_button": "AWS", "alerter_button": "Alerter"}    # shown in the live view while held
BUTTON_NAMES = {"aws": "AWS", "alerter": "alerter", "door_open_left": "open left doors",
                "door_open_right": "open right doors", "door_close_left": "close left doors",
                "door_close_right": "close right doors"}           # cab buttons, as the log names them
BUTTON_AGAIN_SECONDS = 5.0     # a cab button press the game doesn't answer is made again for this long
# keys that can show the next stop while held -> Windows key code. TSW7 binds none of them, in its controls or the
# engine's (DefaultInput.ini, BaseInput.ini, BaseEngine.ini); F4 / F5 / F9 and the number pad's digits, + and - have
# debug bindings, ` opens the console, F12 is Steam's screenshot, and Num . is Delete (TPWS / PZB) with Num Lock off
STOP_KEYS = {"F6": 0x75, "F10": 0x79, "F11": 0x7A, "6": 0x36, "7": 0x37, "-": 0xBD, "=": 0xBB,
             "Num *": 0x6A, "Num /": 0x6F, "Scroll Lock": 0x91, "Pause": 0x13}
# keys that show and hide the route map: those, and F7 (asked for), which the game also uses: it steps the
# destination display back (DestinationToggle) on trains that have one
MAP_KEYS = {"F7": 0x76, **STOP_KEYS}
MAP_CORNERS = ("Bottom left", "Bottom right", "Top left", "Top right", "Middle")    # where the route map goes
NO_KEY = "Not set"


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
    if s["stop_key"] not in STOP_KEYS:
        s["stop_key"] = None
    if s["map_key"] not in MAP_KEYS or s["map_key"] == s["stop_key"]:
        s["map_key"] = None
    s["map_opacity"] = min(1.0, max(0.3, float(s["map_opacity"])))
    s["map_window"] = min(0.6, max(0.15, float(s["map_window"])))
    if s["map_zoom"] not in tsw_livemap.ZOOMS:
        s["map_zoom"] = tsw_livemap.ZOOM
    s["map_auto_zoom"] = bool(s["map_auto_zoom"])
    if s["map_corner"] not in MAP_CORNERS:
        s["map_corner"] = DEFAULTS["map_corner"]
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


# ---------------------------------------------------------------- popups over the game
class Overlay:
    """A small panel over the game near the top of the screen, centred or in the right-hand corner. It's shown and
    hidden without ever taking focus from the game, and clicks go through it."""
    EX_STYLE = 0x08000000 | 0x00000080 | 0x00080000 | 0x00000020   # no activate, tool window, layered, click-through
    SHOW = 0x0001 | 0x0002 | 0x0010 | 0x0040    # SetWindowPos: keep size and place, don't activate, show
    HIDE = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0080
    RAISE = 0x0001 | 0x0002 | 0x0010
    TOPMOST = wintypes.HWND(-1)

    def __init__(self, root, k, top, lines, right=False):
        """`top`: the gap above it, as a share of the screen height; `lines`: font size and bold, line by line;
        `right`: in the top right corner, not centred."""
        user32 = self.user32 = ctypes.windll.user32
        self.set_pos = user32.SetWindowPos
        self.set_pos.argtypes = [wintypes.HWND, wintypes.HWND] + [ctypes.c_int] * 4 + [wintypes.UINT]
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        before = user32.GetForegroundWindow()
        self.root, self.gap, self.k, self.right = root, top, k, right
        self.under = ()               # overlays this one keeps below while they're shown
        win = self.win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.0)
        win.geometry("+-10000+-10000")
        win.configure(bg=PANEL)
        box = self.box = tk.Frame(win, bg=PANEL, padx=round(22 * k), pady=round(8 * k))
        box.pack()
        self.labels = [tk.Label(box, text="", bg=PANEL, fg=FG, font=(FONT, size, "bold") if bold else (FONT, size))
                       for size, bold in lines]
        self.lines = [None] * len(lines)
        self.shown = False
        self.place = None
        win.update_idletasks()        # Tk makes the window now, and makes it the active one
        self.hwnd = int(win.wm_frame(), 16)
        user32.SetWindowLongW(self.hwnd, -20, user32.GetWindowLongW(self.hwnd, -20) | self.EX_STYLE)
        self.set_pos(self.hwnd, None, 0, 0, 0, 0, self.HIDE)
        win.attributes("-alpha", 0.92)
        if before and user32.GetForegroundWindow() != before:
            user32.SetForegroundWindow(before)    # give back the focus it took

    def show(self):
        self._place()                 # the overlay above may have come or gone
        if not self.shown:
            self.win.update_idletasks()
            self.set_pos(self.hwnd, self.TOPMOST, 0, 0, 0, 0, self.SHOW)
            self.shown = True

    def _place(self):
        """Centred (or at the right), `gap` from the top of the screen, and below the overlays above while they're
        shown."""
        top = round(self.root.winfo_screenheight() * self.gap)
        for above in self.under:
            if above.shown and above.place is not None:
                top = max(top, above.place[1] + above.win.winfo_reqheight() + round(6 * self.k))
        spare = self.root.winfo_screenwidth() - self.win.winfo_reqwidth()
        place = (max(0, spare - round(24 * self.k) if self.right else spare // 2), top)
        if place != self.place:          # keep it in place as the text changes width
            self.place = place
            self.win.geometry(f"+{place[0]}+{place[1]}")

    def hide(self):
        if self.shown:
            self.set_pos(self.hwnd, None, 0, 0, 0, 0, self.HIDE)
            self.shown = False

    def keep_on_top(self):
        """Back on top if another program's window has come over it. The game's window is always on top too, so
        going back to the game from another window puts the game over anything already shown. Returns whether it
        had to."""
        if self.shown and self._covered():
            self.raise_()
            return True
        return False

    def raise_(self):
        """On top of the others, as when it's first shown."""
        if self.shown:
            self.set_pos(self.hwnd, self.TOPMOST, 0, 0, 0, 0, self.RAISE)

    def _covered(self):
        user32, mine = self.user32, wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(mine))
        other, pid = wintypes.RECT(), wintypes.DWORD()
        h = user32.GetWindow(self.hwnd, 3)        # GW_HWNDPREV: the window just above, and so on up
        while h:
            if user32.IsWindowVisible(h):           # most of the always-on-top windows aren't
                user32.GetWindowRect(h, ctypes.byref(other))
                if (other.left < mine.right and mine.left < other.right
                        and other.top < mine.bottom and mine.top < other.bottom):
                    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                    if pid.value != os.getpid():
                        return True
            h = user32.GetWindow(h, 3)
        return False

    def set(self, *lines):
        """Each line as (text, colour); a line without text is left out."""
        if list(lines) == self.lines:
            self._place()             # the overlay above may have come or gone
            return
        if [bool(t) for t, _ in lines] != [bool(old and old[0]) for old in self.lines]:
            for label in self.labels:
                label.pack_forget()
            for label, (text, _) in zip(self.labels, lines):
                if text:
                    label.pack()
        for label, line, old in zip(self.labels, lines, self.lines):
            if line != old:
                label.config(text=line[0], fg=line[1])
        self.lines = list(lines)
        self.win.update_idletasks()
        self._place()


class StopOverlay(Overlay):
    """How far the next stop (or go-via) is, while its joystick button is held, and the speed limit now."""

    def __init__(self, root, k):
        super().__init__(root, k, 0.10, [(26, True), (12, False), (10, False), (11, True)])

    def show(self, state, watch):
        """`watch`: the SpeedWatch, for the limit and the units."""
        self.update(state, watch)
        super().show()

    def update(self, state, watch):
        imperial = watch.imperial
        then = state["then"]
        if then and state["then_metres"] is not None:
            then += " in " + tsw_units.distance(state["then_metres"], imperial)
        if state["metres"] is not None:
            big = ("≈ " if state["estimated"] else "") + tsw_units.distance(state["metres"], imperial)
            lines = (big, WARN if state["metres"] < -1 else FG), (state["label"], FG), (then, MUTED)
        elif state["label"]:
            lines = ("-", MUTED), (state["label"], FG), (state["text"], MUTED)
        else:
            lines = ("-", MUTED), (state["text"], FG), ("", MUTED)
        self.set(*lines, self._limit(watch))

    @staticmethod
    def _limit(watch):
        """'Speed limit 60 mph', amber / red while over it as the speeding popup is."""
        limit, over = watch.limit, watch.state
        if limit is None:
            return "", MUTED
        colour = (BAD if over["counted"] else WARN) if over else FG
        return "Speed limit " + tsw_units.speed(limit, watch.imperial), colour


def clock_time(seconds):
    """'08:36:30' from seconds after midnight."""
    s = int(seconds) % 86400
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def minutes(seconds):
    """'1:05' from seconds."""
    s = int(round(abs(seconds)))
    return f"{s // 60}:{s % 60:02d}"


def whole(x):
    """A comfort mark or score as it's shown: 94.5 -> 95."""
    return int(math.floor(x + 0.5))


def mark_colour(mark):
    """Green from 90, amber from 75, else red."""
    return GOOD if whole(mark) >= 90 else WARN if whole(mark) >= 75 else BAD


def lost_text(lost, most=2):
    """What cost a ride its points, the costliest `most` (tsw_comfort.losses): 'jolt on stopping, 1.4 m/s²  ·  4 s
    firm braking'. Less than half a point isn't worth saying."""
    words = []
    for cost, what, *rest in lost[:most]:
        if cost < 0.5:
            break
        if what in ("stop", "way"):
            words.append(f"jolt {'on stopping' if what == 'stop' else 'at a stop on the way'}, {rest[0]:.1f} m/s²")
        else:
            kind, seconds = rest[0], max(1, whole(rest[-1]))
            doing = "braking" if kind == "brake" else "acceleration"
            words.append(f"harsh {doing} {rest[1]:.1f} m/s², {seconds} s" if what == "harsh"
                         else f"{seconds} s firm {doing}")
    return "  ·  ".join(words)


class DwellOverlay(Overlay):
    """While you're stopped at a stop, in the top right corner: whether to wait, shut the doors or go, the time
    now and the departure time, your points (tsw_stops.StopTracker.stop) and how the ride here felt to the
    passengers (tsw_comfort.py). Once you can go, also how far the next stop or go-via is, and that stays up for a
    while after you move off."""
    PHASES = {"wait": ("WAIT", FG), "close": ("SHUT THE DOORS", WARN), "depart": ("DEPART", GOOD),
              "signal": ("WAIT FOR THE SIGNAL", WARN), "end": ("END OF SERVICE", FG), "departed": ("DEPARTED", GOOD)}

    def __init__(self, root, k):
        super().__init__(root, k, 0.03, [(24, True), (13, True), (12, False), (11, False), (12, True), (10, False),
                                         (11, False), (11, True)], right=True)

    def show(self, stop, state, imperial, ride=None):
        """`state`: the tracker's next stop readout (tsw_stops.StopTracker.state); `ride`: the ride report
        (tsw_comfort.ComfortWatch.report)."""
        self.set(*self.lines_for(stop, state, imperial, ride))
        super().show()

    @staticmethod
    def ride_lines(ride):
        """'This ride 79  ·  jolt on stopping, 1.4 m/s²' (coloured by the mark) and 'Comfort 87 this service  ·
        62 passengers'."""
        if not ride:
            return ("", FG), ("", MUTED)
        why = lost_text(ride["lost"]) or ("smooth stop" if ride["stop"] == "smooth" else "")
        line = f"This ride {whole(ride['mark'])}" + (f"  ·  {why}" if why else "")
        n = ride["passengers"]
        tally = f"{n} passenger{'s' * (n != 1)}" if n else "no passengers aboard"
        if ride.get("score") is not None:
            tally = f"Comfort {whole(ride['score'])} this service  ·  {tally}"
        return (line, mark_colour(ride["mark"])), (tally, FG)

    @classmethod
    def lines_for(cls, stop, state=None, imperial=False, ride=None):
        action, colour = cls.PHASES[stop["phase"]]
        goal = ""
        if state and state["label"] and stop["phase"] in ("depart", "signal", "departed"):  # the game has let the
            goal = state["label"]                                   # train go: the readout is on to the next
            if state["metres"] is not None:
                goal += " in " + ("≈ " if state["estimated"] else "") + tsw_units.distance(state["metres"], imperial)
        left, departs = stop["left"], stop["departs"]
        if stop["phase"] == "close" and left is not None and left < 0:
            colour = BAD                                  # it's past the departure time
        where = stop["station"]
        if stop["phase"] == "end":
            where += "  ·  terminates here"
        elif stop["phase"] == "departed":
            where += "  ·  left " + clock_time(stop["moved"])
        elif departs is not None:
            where += "  ·  departs " + clock_time(departs)
        now = "now " + clock_time(stop["now"])
        if departs is not None and left is not None and stop["phase"] in ("wait", "close"):
            now += f"  ·  in {minutes(left)}" if left >= 0.5 else f"  ·  {minutes(left)} late"
        if stop["phase"] == "wait":
            now += "  ·  doors " + ("open" if stop["doors"] else "shut")
        points, arrival = stop["points"] or {}, ""
        score = ""
        if points.get("points") is not None:
            score = f"{points['points']:,} points"
            if points.get("change"):
                score += f"  ({points['change']:+,})"
        if points.get("late") is not None:
            late = points["late"]
            arrival = ("arrived on time" if abs(late) < 1 else
                       f"arrived {minutes(late)} {'late' if late > 0 else 'early'}")
            arrival += f", {points['off']:.1f} m from the marker"
        return ((action, colour), (goal, FG), (where, FG), (now, MUTED), (score, FG), (arrival, MUTED),
                *cls.ride_lines(ride))


def short_time(seconds):
    """'15:33' from seconds after midnight, or '08:36:30' where the seconds aren't 0."""
    return clock_time(seconds) if int(seconds) % 60 else clock_time(seconds)[:5]


class ScheduleOverlay(Overlay):
    """The service's schedule from the stop you're at, as the game's own (T) lists it: that stop and the next
    few, with their platforms and times. Under the what-to-do panel, for as long as that's shown."""
    STOPS = 5                 # the stop you're at and the next four
    TITLE, FOOT = (10, True), (9, False)      # font size, bold
    HEAD, ROW = 8, 11

    def __init__(self, root, k):
        super().__init__(root, k, 0.03, [], right=True)
        self.table = None
        self.content = None

    def show(self, stop):
        if not stop.get("schedule"):
            self.hide()
            return
        content = self.content_for(stop)
        if content != self.content:
            self._build(content)
        super().show()

    @classmethod
    def content_for(cls, stop):
        """(title, rows, footer, times) for tsw_stops.StopTracker.stop. Each row is cells of (text, colour, bold):
        a mark, the station, its platform, the arrival and departure times. The first row is a heading; a column
        nothing is known for is left out. `times`: which columns are times (set to the right)."""
        stops = stop.get("schedule") or []
        shown, rest = stops[:cls.STOPS], stops[cls.STOPS:]
        left = stop["phase"] == "departed"
        rows = []
        for n, (station, platform, arrives, departs) in enumerate(shown):
            mark, colour, bold = "", FG, False
            if n == 0:
                mark, colour, bold = ("✓", MUTED, False) if left else ("▸", FG, True)
            elif n == 1 and left:
                mark, bold = "▸", True                     # where you're going now
            rows.append([(mark, GOOD if mark == "✓" else colour, bold), (station, colour, bold),
                         ((platform.split()[-1].lstrip("0") or "0") if platform else "", MUTED, False),
                         (short_time(arrives) if arrives is not None else "", colour, bold),
                         (short_time(departs) if departs is not None else "", colour, bold)])
        keep = [c for c in range(5) if c < 2 or any(r[c][0] for r in rows)]
        heading = [("", MUTED, True), ("STATION", MUTED, True), ("PLAT", MUTED, True), ("ARR", MUTED, True),
                   ("DEP", MUTED, True)]
        rows = [tuple(r[c] for c in keep) for r in [heading] + rows]
        footer = ""
        if rest:
            station, _, arrives, _ = rest[-1]
            footer = f"+ {len(rest)} more stop{'s' * (len(rest) != 1)} to {station}"
            if arrives is not None:
                footer += f", arr {short_time(arrives)}"
        title = "SCHEDULE" + (f"  ·  {stop['service']}" if stop.get("service") else "")
        times = tuple(c >= 3 for c in keep)
        return title, tuple(rows), footer, times

    @staticmethod
    def _label(parent, text, colour, size, bold, **grid):
        tk.Label(parent, text=text, bg=PANEL, fg=colour, justify="left",
                 font=(FONT, size, "bold") if bold else (FONT, size)).grid(**grid)

    def _build(self, content):
        """Lays the table out again (only when it changes: once a stop or so)."""
        self.content = content
        title, rows, footer, times = content[:4]
        if self.table is not None:
            self.table.destroy()
        k, label = self.k, self._label
        table = self.table = tk.Frame(self.box, bg=PANEL)
        table.pack()
        last = len(rows[0]) - 1
        label(table, title, FG, *self.TITLE, row=0, column=0, columnspan=last + 1, sticky="w",
              pady=(0, round(4 * k)))
        for r, cells in enumerate(rows, start=1):
            size = self.HEAD if r == 1 else self.ROW
            for c, (text, colour, bold) in enumerate(cells):
                label(table, text, colour, size, bold, row=r, column=c, sticky="e" if times[c] else "w",
                      padx=(0, 0 if c == last else round((6 if c == 0 else 14) * k)))
        row = self._more(table, len(rows) + 1, last + 1)
        if footer:
            label(table, footer, MUTED, *self.FOOT, row=row, column=0, columnspan=last + 1, sticky="w",
                  pady=(round(4 * k), 0))
        self.win.update_idletasks()
        self._place()

    def _more(self, table, row, columns):
        """Anything under the table, from `row`: the row after it."""
        return row


class SummaryOverlay(ScheduleOverlay):
    """At the end of a service, under the what-to-do panel in place of the schedule: how the run went against the
    best run of the same service before (tsw_history.py), where it's better in green, and each ride's comfort mark."""
    RIDES = 10                # more rides than this: only those that lost points, the worst this many

    def show(self, run, best, count):
        content = self.summary_for(run, best, count)
        if content != self.content:
            self._build(content)
        Overlay.show(self)

    def _more(self, table, row, columns):
        """The rides' marks, in a table of their own under the comparison."""
        rides = self.content[4]
        if not rides:
            return row
        lines, note = rides
        k, label = self.k, self._label
        frame = tk.Frame(table, bg=PANEL)
        frame.grid(row=row, column=0, columnspan=columns, sticky="w", pady=(round(8 * k), 0))
        label(frame, "RIDES", FG, *self.TITLE, row=0, column=0, columnspan=3, sticky="w", pady=(0, round(2 * k)))
        for r, (station, mark, colour, why) in enumerate(lines, start=1):
            label(frame, station, FG, self.ROW, False, row=r, column=0, sticky="w", padx=(0, round(14 * k)))
            label(frame, mark, colour, self.ROW, True, row=r, column=1, sticky="e", padx=(0, round(14 * k)))
            label(frame, why, MUTED, *self.FOOT, row=r, column=2, sticky="w")
        if note:
            label(frame, note, MUTED, *self.FOOT, row=len(lines) + 1, column=0, columnspan=3, sticky="w")
        return row + 1

    @classmethod
    def rides_for(cls, marks):
        """(rows of (station, mark, colour, why), note) for the rides kept with a run (tsw_comfort._ride), or None.
        All of them, or on a long service those that lost points, in the order driven."""
        if not marks:
            return None
        shown = list(range(len(marks)))
        if len(marks) > cls.RIDES:
            lost = [i for i in shown if whole(marks[i]["mark"]) < 100]
            worst = set(sorted(lost, key=lambda i: marks[i]["mark"])[:cls.RIDES])
            shown = [i for i in lost if i in worst]
        rows = tuple((marks[i].get("station") or "-", str(whole(marks[i]["mark"])), mark_colour(marks[i]["mark"]),
                      lost_text(marks[i]["lost"])) for i in shown)
        rest = [r for i, r in enumerate(marks) if i not in shown]
        note = ""
        if rest:
            note = f"+ {len(rest)} more ride{'s' * (len(rest) != 1)}"
            if all(whole(r["mark"]) >= 100 for r in rest):
                note += " at 100"
        return rows, note

    @classmethod
    def summary_for(cls, run, best, count):
        """(title, rows, footer, times, rides): the first four as ScheduleOverlay.content_for has them, rides as
        rides_for gives them."""
        rows = [[("", MUTED, True), ("THIS RUN", MUTED, True)] + ([("BEST BEFORE", MUTED, True)] if best else [])]

        def row(label, key, text, better=None):
            if run.get(key) is None:
                return
            had = best is not None and best.get(key) is not None
            colour = GOOD if had and better and better(run, best) else FG
            cells = [(label, MUTED, False), (text(run), colour, True)]
            if best:
                cells.append((text(best) if had else "-", FG, False))
            rows.append(cells)

        row("Points", "points", lambda r: f"{r['points']:,}", lambda a, b: a["points"] > b["points"])
        row("Comfort", "comfort", lambda r: str(whole(r["comfort"])),
            lambda a, b: whole(a["comfort"]) > whole(b["comfort"]))
        row("On time", "on_time", lambda r: f"{r['on_time']} of {r['stops']} stops",
            lambda a, b: a["on_time"] / a["stops"] > b["on_time"] / b["stops"])
        row("Stop accuracy", "accuracy", lambda r: f"{r['accuracy']:.1f} m off",
            lambda a, b: a["accuracy"] < b["accuracy"])
        if run.get("rides"):
            row("Smooth rides", "smooth", lambda r: f"{r['smooth']} of {r['rides']}",
                lambda a, b: a["smooth"] / a["rides"] > b["smooth"] / max(b.get("rides") or 0, 1))
        if run.get("passengers"):
            row("Passengers", "passengers", lambda r: f"up to {r['passengers']}")
        footer = []
        if run["worst_late"] > tsw_history.ON_TIME:
            footer.append(f"Latest {minutes(run['worst_late'])} behind time at a stop")
        if run.get("comfort") is not None:
            footer.append("Comfort: each ride's mark, weighted by the passengers aboard" if run.get("passengers")
                          else "Comfort: the rides' marks, nobody aboard")
        if best:
            line = f"Best before on {best['finished'][:10]}"
            if best.get("train") and best["train"] != run.get("train"):
                line += f" with the {pretty_train(best['train'])}"
            footer.append(line + f"  ·  {count} runs of {run['service']} kept")
        else:
            footer.append(f"First run of {run['service']} kept")
        title = f"SERVICE COMPLETE  ·  {run['service']}" + (f"  ·  {run['route']}" if run.get("route") else "")
        times = tuple(c > 0 for c in range(len(rows[0])))
        return title, tuple(tuple(r) for r in rows), "\n".join(footer), times, cls.rides_for(run.get("marks"))


class SpeedOverlay(Overlay):
    """The speed limit, for as long as you're over it: amber while within the game's tolerance, red once the game
    counts it as speeding. Sits below the next stop readout, moving down while that's shown if it needs to."""

    def __init__(self, root, k):
        super().__init__(root, k, 0.20, [(9, True), (26, True), (11, False)])

    def show(self, state, imperial):
        limit, you = (tsw_units.speed(state[k], imperial) for k in ("limit", "speed"))
        if you == limit:                  # just over: "you 60 mph" under "60 mph" would look wrong
            you = tsw_units.speed(state["speed"], imperial, 1)
        color = BAD if state["counted"] else WARN
        self.set(("SPEED LIMIT", color), (limit, color), ("you " + you, FG))
        super().show()


class ComfortOverlay(Overlay):
    """In the last seconds of a hard stop, to ease the brake off before the train stops; then how the stop went;
    else while braking or speeding up is firm (amber) or harsh (red) for the passengers, and for a moment after
    what that cost the service's comfort (tsw_comfort.ComfortWatch.coach). Below the speeding popup."""

    def __init__(self, root, k):
        super().__init__(root, k, 0.20, [(10, True), (24, True), (12, False)])

    @staticmethod
    def aboard(n):
        return f"{n} passenger{'s' * (n != 1)} aboard" if n else "no passengers aboard"

    def show(self, coach):
        phase = coach["phase"]
        if phase == "ease":
            self.set((f"STOPPING IN {max(1, math.ceil(coach['seconds']))} s", WARN), ("EASE OFF THE BRAKE", WARN),
                     (f"braking {coach['braking']:.1f} m/s²", FG))
        elif phase == "stopped":
            note = f"braking {coach['braking']:.1f} m/s² as it stopped"
            if whole(coach.get("cost") or 0):
                note += f"  ·  −{whole(coach['cost'])}"
            score = coach.get("score")
            self.set((f"COMFORT {whole(score)}" if score is not None else "", FG),
                     ("JOLT ON STOPPING", BAD) if coach["jolt"] else ("SMOOTH STOP", GOOD), (note, MUTED))
        elif phase == "lost":
            what = "BRAKING" if coach["kind"] == "brake" else "ACCELERATION"
            self.set((f"{coach['level'].upper()} {what}  −{whole(coach['cost'])}",
                      BAD if coach["level"] == "harsh" else WARN),
                     (f"COMFORT {whole(coach['score'])}", FG),
                     (f"this ride {whole(coach['mark'])}  ·  {self.aboard(coach['passengers'])}", FG))
        else:
            colour = BAD if phase == "harsh" else WARN
            what = "BRAKING" if coach["kind"] == "brake" else "ACCELERATION"
            if phase == "harsh":
                note = self.aboard(coach["passengers"])
            else:
                note = "ease off a step" if coach["kind"] == "brake" else "ease off the power"
            self.set((f"{phase.upper()} {what}", colour), (f"{coach['felt']:.1f} m/s²", colour), (note, FG))
        super().show()

class MapOverlay(Overlay):
    """The route map over the game in a small see-through window that follows you (tsw_livemap.py): the part of
    the line you're on, with a dot where you are and an arrow the way you're going, the window's middle a little
    ahead of you, and how far the next stop (or go-via) is in its top left corner, with a ring where it is. Until
    the map is made, what's happening."""
    DOT, RING, EDGE = "#1f6fe0", "#ffffff", "#111111"
    STOP, PASSED = "#c2185b", "#c62828"
    EASE = 0.2                    # share of the way to where it should be that the view moves each time round
    MARGIN = 24                   # pixels from the screen's edges
    STOP_SLACK = 100.0            # metres: a stop further from you than its distance and this (and a tenth more)
                                  # isn't the one the distance is to: no ring

    def __init__(self, root, k, opacity, size, corner):
        super().__init__(root, k, 0.0, [(12, True)])
        self.win.attributes("-alpha", opacity)
        self.corner = corner          # one of MAP_CORNERS
        c = self.canvas = tk.Canvas(self.box, bg="#ffffff", highlightthickness=0, bd=0)
        self.map = None               # the map shown (MapWatch.map); None: the line of what's happening
        self.pieces = {}              # (column, row) -> (PhotoImage, canvas item, the drawing it's of)
        self.view = None              # the map's pixel at the window's top left
        self.stop_halo = c.create_oval(0, 0, 0, 0, outline=self.RING, width=max(4, round(6 * k)), state="hidden")
        self.stop_ring = c.create_oval(0, 0, 0, 0, outline=self.STOP, width=max(2, round(3 * k)), state="hidden")
        self.arrow = c.create_polygon(0, 0, 0, 0, 0, 0, fill=self.DOT, outline=self.EDGE,
                                      width=max(1, round(1.5 * k)), state="hidden")
        self.under = c.create_oval(0, 0, 0, 0, fill=self.EDGE, outline="", state="hidden")
        self.dot = c.create_oval(0, 0, 0, 0, fill=self.DOT, outline=self.RING, width=max(2, round(2.5 * k)),
                                 state="hidden")
        self.caption_box = c.create_rectangle(0, 0, 0, 0, fill="#ffffff", outline="")
        self.caption = c.create_text(0, 0, anchor="sw", text="", fill=self.EDGE, font=(FONT, 9, "bold"))
        self.bar = c.create_line(0, 0, 0, 0, fill=self.EDGE, width=max(2, round(3 * k)))
        self.bar_text = c.create_text(0, 0, anchor="w", text="", fill=self.EDGE, font=(FONT, 9))
        self.note = c.create_text(0, 0, anchor="n", text="", fill=self.EDGE, font=(FONT, 10, "bold"))
        self.stop_box = c.create_rectangle(0, 0, 0, 0, fill="#ffffff", outline="", state="hidden")
        self.stop_far = c.create_text(0, 0, anchor="nw", text="", fill=self.EDGE, font=(FONT, 13, "bold"),
                                      state="hidden")
        self.stop_what = c.create_text(0, 0, anchor="nw", text="", fill=self.EDGE, font=(FONT, 9), state="hidden")
        self.stop_shown = None        # what the corner says: (distance, its colour, the stop)
        self.border = c.create_rectangle(0, 0, 0, 0, outline="#333333", width=1)
        self.size = None
        self.resize(size)

    def set_opacity(self, opacity):
        self.win.attributes("-alpha", opacity)

    def resize(self, size):
        """`size`: (width, height) in pixels."""
        self.size = size
        w, h = size
        self.canvas.config(width=w, height=h)
        self.canvas.coords(self.border, 0, 0, w - 1, h - 1)
        self.canvas.coords(self.note, w / 2, round(8 * self.k))
        self.stop_shown = None
        self._caption()
        self.win.update_idletasks()
        self._place()

    def show(self, watch, stop=None, imperial=False):
        self.update(watch, stop, imperial)
        super().show()

    def update(self, watch, stop=None, imperial=False):
        """`stop`: the next stop readout (tsw_stops.StopTracker.state), or None; `imperial`: its distance in miles
        and yards."""
        if watch.map is not self.map:
            self._load(watch.map)
        if self.map is None:
            self.set((watch.status, FG))
            return
        if not watch.off_line:
            self._follow(watch.centre)
        missing = self._pieces(watch.done) if self.view is not None else True
        self._mark(watch, missing)
        self._next_stop(watch, stop, imperial)

    def _place(self):
        """In its corner of the screen."""
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h, m = self.win.winfo_reqwidth(), self.win.winfo_reqheight(), round(self.MARGIN * self.k)
        x = m if self.corner.endswith("left") else sw - w - m if self.corner.endswith("right") else (sw - w) // 2
        y = m if self.corner.startswith("Top") else sh - h - m if self.corner.startswith("Bottom") else (sh - h) // 2
        place = (max(0, x), max(0, y))
        if place != self.place:
            self.place = place
            self.win.geometry(f"+{place[0]}+{place[1]}")

    def _load(self, meta):
        """Another map: another service's or zoom's starts afresh; the same one drawn again (with its scenery) keeps
        the pieces it has till the new ones come."""
        old, self.map = self.map, meta
        if meta is None or old is None or meta["key"] != old["key"]:
            self.view = None
            for _, item, _ in self.pieces.values():
                self.canvas.delete(item)
            self.pieces = {}
        if meta is None:
            self.canvas.pack_forget()
            self.box.config(padx=round(22 * self.k), pady=round(8 * self.k))
            return
        self.set(("", FG))
        self.box.config(padx=0, pady=0)
        self.canvas.pack()
        self._caption()
        self.win.update_idletasks()
        self._place()

    def _caption(self):
        """The service and a scale bar, in the bottom left corner."""
        c, k, meta = self.canvas, self.k, self.map
        if meta is None:
            return
        w, h = self.size
        bar = max([b for b in (0.1, 0.25, 0.5, 1, 2, 5, 10, 20) if b * meta["unit_m"] / meta["zoom"] <= w * 0.25]
                  or [0.1])
        length, pad = bar * meta["unit_m"] / meta["zoom"], 6 * k
        y = h - pad - 7 * k
        c.coords(self.bar, 2 * pad, y, 2 * pad + length, y)
        c.coords(self.bar_text, 3 * pad + length, y)
        c.itemconfigure(self.bar_text, text=f"{bar:g} {meta['unit']}")
        c.coords(self.caption, 2 * pad, y - 7 * k)
        c.itemconfigure(self.caption, text=meta["title"])
        x0, y0, x1, y1 = c.bbox(self.caption, self.bar, self.bar_text) or (0, 0, 0, 0)
        c.coords(self.caption_box, x0 - pad, y0 - pad / 2, x1 + pad, y1 + pad / 2)

    def _follow(self, centre):
        """The view moves smoothly to where it should be, or jumps there from far away."""
        if centre is None:
            return
        w, h = self.size
        target = (centre[0] - w / 2, centre[1] - h / 2)
        if self.view is None or math.dist(target, self.view) > max(w, h):
            self.view = target
        else:
            self.view = (self.view[0] + (target[0] - self.view[0]) * self.EASE,
                         self.view[1] + (target[1] - self.view[1]) * self.EASE)

    def _pieces(self, done):
        """The map's pieces in view, one more loaded each time round (each takes about a hundredth of a second), or
        all those drawn at once on a map just come (another zoom shows whole), those far away let go. Returns
        whether some of the view isn't drawn yet."""
        c, size, meta = self.canvas, tsw_livemap.CHUNK, self.map
        vx, vy = round(self.view[0]), round(self.view[1])
        w, h = self.size
        mw, mh = meta["size"]
        cells = [(i, j) for i in range(max(0, vx // size), min((vx + w - 1) // size, (mw - 1) // size) + 1)
                 for j in range(max(0, vy // size), min((vy + h - 1) // size, (mh - 1) // size) + 1)]
        missing, loaded, fresh = False, False, not self.pieces
        for cell in cells:
            have = self.pieces.get(cell)
            if have and have[2] == meta["stamp"]:
                continue
            if cell not in done or (loaded and not fresh):
                missing = missing or cell not in done
                continue
            try:
                image = tk.PhotoImage(file=tsw_livemap.chunk_file(meta, cell))
            except tk.TclError:                 # drawn again just now
                continue
            if have:
                c.delete(have[1])
            item = c.create_image(0, 0, anchor="nw", image=image)
            c.tag_lower(item)
            self.pieces[cell] = (image, item, meta["stamp"])
            loaded = True
        for (i, j), (_, item, _) in list(self.pieces.items()):
            if not (vx - size - w < i * size < vx + w + size and vy - size - h < j * size < vy + h + size):
                c.delete(item)
                del self.pieces[(i, j)]
            else:
                c.coords(item, i * size - vx, j * size - vy)
        return missing

    def _mark(self, watch, missing):
        c, k = self.canvas, self.k
        items = (self.arrow, self.under, self.dot)
        if watch.position is None or watch.off_line or self.view is None:
            for item in items:
                c.itemconfigure(item, state="hidden")
        else:
            px, py = tsw_livemap.to_px(self.map, *watch.position)
            x, y, r, edge = px - round(self.view[0]), py - round(self.view[1]), 7 * k, 2 * k
            c.coords(self.under, x - r - edge, y - r - edge, x + r + edge, y + r + edge)
            c.coords(self.dot, x - r, y - r, x + r, y + r)
            if watch.heading:
                hx, hy = watch.heading
                tip, back, half = r + 17 * k, r - 2 * k, 8 * k        # its base under the dot
                c.coords(self.arrow, x + hx * tip, y + hy * tip, x + hx * back - hy * half,
                         y + hy * back + hx * half, x + hx * back + hy * half, y + hy * back - hx * half)
            for item in items:
                c.itemconfigure(item, state="normal" if item != self.arrow or watch.heading else "hidden")
        note = ("You're not on this map's line" if watch.off_line
                else "Where you are isn't known yet" if watch.position is None
                else watch.draw_failed if watch.draw_failed and missing
                else "Drawing this part of the map..." if missing else "")
        if c.itemcget(self.note, "text") != note:
            c.itemconfigure(self.note, text=note)

    def _next_stop(self, watch, stop, imperial):
        """How far the next stop is, in the top left corner (the note goes below it), and a ring where it is."""
        c, k = self.canvas, self.k
        shown = self.stop_lines(stop, imperial, self.PASSED, self.EDGE)
        if shown != self.stop_shown:
            self.stop_shown = shown
            far, colour, what = shown
            pad, w = 6 * k, self.size[0]
            top = round(8 * k)
            if what:
                c.coords(self.stop_far, 2 * pad, 1.5 * pad)
                c.itemconfigure(self.stop_far, text=far, fill=colour, state="normal" if far else "hidden")
                c.coords(self.stop_what, 2 * pad, (c.bbox(self.stop_far)[3] + k) if far else 1.5 * pad)
                c.itemconfigure(self.stop_what, text=what, width=max(1, w - 4 * pad), state="normal")
                x0, y0, x1, y1 = c.bbox(self.stop_far, self.stop_what)
                c.coords(self.stop_box, x0 - pad, y0 - pad / 2, x1 + pad, y1 + pad / 2)
                c.itemconfigure(self.stop_box, state="normal")
                top = round(y1 + pad / 2 + 6 * k)
            else:
                for item in (self.stop_box, self.stop_far, self.stop_what):
                    c.itemconfigure(item, state="hidden")
            c.coords(self.note, w / 2, top)
        place = self._stop_place(watch, stop)
        if place is None:
            for item in (self.stop_halo, self.stop_ring):
                c.itemconfigure(item, state="hidden")
            return
        px, py = tsw_livemap.to_px(self.map, *place)
        x, y, r = px - round(self.view[0]), py - round(self.view[1]), 10 * k
        for item in (self.stop_halo, self.stop_ring):
            c.coords(item, x - r, y - r, x + r, y + r)
            c.itemconfigure(item, state="normal")

    @staticmethod
    def stop_lines(stop, imperial, passed, colour):
        """(distance, its colour, the stop and the one after a go-via) as the corner says them; ("", colour, "")
        when there's no next stop."""
        if not stop or not stop["label"]:
            return "", colour, ""
        if stop["metres"] is None:
            return "", colour, f"{stop['label']}\n{stop['text']}"
        far = ("≈ " if stop["estimated"] else "") + tsw_units.distance(stop["metres"], imperial)
        what = stop["label"]
        if stop["then"]:
            what += f"\nthen {stop['then'][0].lower()}{stop['then'][1:]}" + (
                "" if stop["then_metres"] is None else " in " + tsw_units.distance(stop["then_metres"], imperial))
        return far, passed if stop["metres"] < -1 else colour, what

    def _stop_place(self, watch, stop):
        """Where on the map the next stop is, (x, y) in the world, or None: not this map's service, or not one of its
        stops, or further from you than its distance can be (the map is another part of the service's)."""
        meta = self.map
        if not stop or stop.get("at") is None or stop.get("service") != meta.get("service") or self.view is None \
                or watch.off_line:
            return None
        place = next(((x, y) for m, x, y in meta.get("stops") or () if abs(m - stop["at"]) < 1), None)
        if place is None or watch.position is None or stop["metres"] is None:
            return place
        far = abs(stop["metres"])
        return place if math.dist(place, watch.position) <= far * 1.1 + self.STOP_SLACK else None


# ---------------------------------------------------------------- window
class HistoryWindow:
    """The services you've driven to the end (tsw_history.py), newest first, each service's best run in green."""
    COLUMNS = (("finished", "Finished", 125, "w"), ("route", "Route", 150, "w"), ("service", "Service", 70, "w"),
               ("train", "Train", 150, "w"), ("points", "Points", 65, "e"), ("on_time", "On time", 65, "e"),
               ("accuracy", "From marker", 85, "e"), ("comfort", "Comfort", 65, "e"),
               ("smooth", "Smooth rides", 90, "e"), ("passengers", "Passengers", 80, "e"))

    def __init__(self, root, k, history):
        win = self.win = tk.Toplevel(root)
        win.title("Service history")
        win.configure(bg=BG)
        st = ttk.Style(root)
        st.configure("History.Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, borderwidth=0,
                     rowheight=round(22 * k))
        st.configure("History.Treeview.Heading", background=TRACK, foreground=MUTED, font=(FONT, 9, "bold"),
                     borderwidth=0, relief="flat")
        st.map("History.Treeview", background=[("selected", "#3a414d")], foreground=[("selected", FG)])
        st.map("History.Treeview.Heading", background=[("active", TRACK)])
        outer = ttk.Frame(win, padding=(16, 12))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Services driven to the end", style="Head.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Points, on time and the distance from the marker come from the game's own save; "
                              "smooth rides and passengers from the passenger comfort watch. Each service's best "
                              "run is in green.", style="Sub.TLabel", wraplength=round(860 * k),
                  justify="left").pack(anchor="w", pady=(2, 10))
        frame = ttk.Frame(outer)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=[c[0] for c in self.COLUMNS], show="headings", style="History.Treeview",
                            height=18)
        for key, title, width, anchor in self.COLUMNS:
            tree.heading(key, text=title, anchor=anchor)
            tree.column(key, width=round(width * k), anchor=anchor, stretch=key in ("route", "train"))
        bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        tree.tag_configure("best", foreground=GOOD)
        best = {}
        for run in history.runs:
            if run.get("points") is not None and run["points"] > best.get(run["service"], (None, -1))[1]:
                best[run["service"]] = (run["id"], run["points"])
        for run in reversed(history.runs):
            tree.insert("", "end", values=self.cells(run),
                        tags=("best",) if best.get(run["service"], (None,))[0] == run["id"] else ())
        if not history.runs:
            ttk.Label(outer, text="None yet: a service is kept here once you reach its last stop.",
                      style="Sub.TLabel").pack(anchor="w", pady=(8, 0))

    @staticmethod
    def cells(run):
        def when(text):
            try:
                return datetime.datetime.fromisoformat(text).strftime("%d %b %Y %H:%M")
            except (TypeError, ValueError):
                return text or ""
        return (when(run.get("finished")), run.get("route", ""), run["service"], pretty_train(run.get("train")),
                "-" if run.get("points") is None else f"{run['points']:,}", f"{run['on_time']} of {run['stops']}",
                f"{run['accuracy']:.1f} m",
                "-" if run.get("comfort") is None else str(whole(run["comfort"])),
                f"{run['smooth']} of {run['rides']}" if run.get("rides") else "-",
                run.get("passengers", "-"))


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
        self.tracker = tsw_stops.StopTracker(self.log)
        self.speed_watch = tsw_speed.SpeedWatch()
        self.comfort = tsw_comfort.ComfortWatch(self.log)
        self.map_watch = tsw_livemap.MapWatch(self.log)
        self.map_shown = False        # the route map is shown (its button or key toggles it)
        self.map_key_down = False     # its key was down last time round
        self.at_station = None        # service you're stopped at a station of, for the ride report
        self.at_name = None           # ... and that station
        self.at_first = False         # ... and it's the service's first stop
        self.ride = None              # ride report for that station
        self.restores = 0             # times the comfort went back to a game save's, as of that station
        self.history = tsw_history.History()
        self.history_window = None
        self.look_raw = 0.0           # twist position, +1 = full right
        self.stick = None
        self.next_stick_try = 0.0
        self.assigning = None         # settings key waiting for a joystick button press
        self.held = {name: False for name in CAB_BUTTONS.values()}   # cab buttons held down now
        self.stop_held = set()        # what's holding the next stop up now: "button", "key"
        self.fg_pid, self.fg_ok = None, False   # process with the focus, and whether the next stop key counts there
        self.raw_y = 0.0
        self.y = 0.0
        self.y_filter = core.AxisFilter()
        self.follow = core.HandleFollower()   # moves the handle after the stick at the train's handle speed
        self.hs_train = None          # train the handle speed setting shows ("" = not in a train)
        self._hs_quiet = False        # the slider is being set to the train's value, not by you
        self.rev_s = 0.0              # slider position, +1 = forward end
        self.rev_zone = None
        self.frame = 0
        self.k = root.winfo_fpixels("1i") / 96.0     # display scaling for canvas drawing

        pygame.init()
        self._style()
        self._build()
        root.update_idletasks()       # shows this window first, so the overlays hand the focus back to it
        self.dwell_overlay = DwellOverlay(root, self.k)
        self.schedule_overlay = ScheduleOverlay(root, self.k)
        self.schedule_overlay.under = (self.dwell_overlay,)
        self.summary_overlay = SummaryOverlay(root, self.k)
        self.summary_overlay.under = (self.dwell_overlay,)
        self.overlay = StopOverlay(root, self.k)
        self.speed_overlay = SpeedOverlay(root, self.k)
        self.speed_overlay.under = (self.overlay,)
        self.comfort_overlay = ComfortOverlay(root, self.k)
        self.comfort_overlay.under = (self.overlay, self.speed_overlay)
        self.map_overlay = MapOverlay(root, self.k, self.s["map_opacity"], self._map_view(), self.s["map_corner"])
        self._refresh_toggle()
        self._refresh_button_label()
        root.attributes("-topmost", bool(self.s["on_top"]))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.bridge.start()
        self.look.start()
        self.tracker.start()
        self.speed_watch.start()
        self.comfort.start()
        self.map_watch.view, self.map_watch.zoom = self._map_view(), self.s["map_zoom"]
        self.map_watch.auto = self.s["map_auto_zoom"]
        self.map_watch.unit_px = tsw_livemap.UNIT_PX * self.k
        self.map_watch.start()
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
        row = 7 + len(BUTTON_ACTIONS)
        ttk.Label(grid, text="Next stop key", style="Muted.TLabel").grid(row=row, column=0, sticky="w",
                                                                        pady=3, padx=(0, 14))
        key_row = ttk.Frame(grid, style="Panel.TFrame")
        key_row.grid(row=row, column=1, columnspan=3, sticky="w", pady=3)
        self.stop_key_combo = ttk.Combobox(key_row, state="readonly", width=11, values=[NO_KEY, *STOP_KEYS])
        self.stop_key_combo.set(self.s["stop_key"] or NO_KEY)
        self.stop_key_combo.pack(side="left")
        self.stop_key_combo.bind("<<ComboboxSelected>>", self.on_stop_key)
        ttk.Label(key_row, text="hold in the game to show the next stop",
                  style="Muted.TLabel").pack(side="left", padx=(8, 0))
        row += 1
        ttk.Label(grid, text="Route map key", style="Muted.TLabel").grid(row=row, column=0, sticky="w",
                                                                        pady=3, padx=(0, 14))
        map_key_row = ttk.Frame(grid, style="Panel.TFrame")
        map_key_row.grid(row=row, column=1, columnspan=3, sticky="w", pady=3)
        self.map_key_combo = ttk.Combobox(map_key_row, state="readonly", width=11, values=[NO_KEY, *MAP_KEYS])
        self.map_key_combo.set(self.s["map_key"] or NO_KEY)
        self.map_key_combo.pack(side="left")
        self.map_key_combo.bind("<<ComboboxSelected>>", self.on_map_key)
        ttk.Label(map_key_row, text="press in the game to show / hide the map (F7 also steps the destination back)",
                  style="Muted.TLabel").pack(side="left", padx=(8, 0))
        row += 1
        ttk.Label(grid, text="Route map view", style="Muted.TLabel").grid(row=row, column=0, sticky="w",
                                                                         pady=3, padx=(0, 14))
        map_row = ttk.Frame(grid, style="Panel.TFrame")
        map_row.grid(row=row, column=1, columnspan=3, sticky="w", pady=3)
        ttk.Label(map_row, text="See-through", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.map_opacity_scale = ttk.Scale(map_row, from_=1.0, to=0.3, length=round(110 * self.k),
                                           value=float(self.s["map_opacity"]), command=self.on_map_opacity)
        self.map_opacity_scale.pack(side="left")
        self.map_opacity_scale.bind("<ButtonRelease-1>", lambda e: save_settings(self.s))
        self.map_opacity_label = ttk.Label(map_row, text="", style="Panel.TLabel", width=5)
        self.map_opacity_label.pack(side="left", padx=(6, 12))
        self.on_map_opacity(self.s["map_opacity"])
        ttk.Label(map_row, text="Size", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.map_size_scale = ttk.Scale(map_row, from_=0.15, to=0.6, length=round(110 * self.k),
                                        value=float(self.s["map_window"]), command=self.on_map_size)
        self.map_size_scale.pack(side="left")
        self.map_size_scale.bind("<ButtonRelease-1>", self.on_map_size_done)
        self.map_size_label = ttk.Label(map_row, text="", style="Panel.TLabel", width=12)
        self.map_size_label.pack(side="left", padx=(6, 12))
        ttk.Label(map_row, text="Where", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.map_corner_combo = ttk.Combobox(map_row, state="readonly", width=12, values=MAP_CORNERS)
        self.map_corner_combo.set(self.s["map_corner"])
        self.map_corner_combo.pack(side="left")
        self.map_corner_combo.bind("<<ComboboxSelected>>", self.on_map_corner)
        row += 1
        ttk.Label(grid, text="Route map zoom", style="Muted.TLabel").grid(row=row, column=0, sticky="w",
                                                                         pady=3, padx=(0, 14))
        zoom_row = ttk.Frame(grid, style="Panel.TFrame")
        zoom_row.grid(row=row, column=1, columnspan=3, sticky="w", pady=3)
        self.map_auto_var = tk.BooleanVar(value=self.s["map_auto_zoom"])
        ttk.Checkbutton(zoom_row, text="By speed", variable=self.map_auto_var, style="Panel.TCheckbutton",
                        command=self.on_map_auto_zoom).pack(side="left", padx=(0, 12))
        ttk.Label(zoom_row, text="Closer", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.map_zoom_scale = ttk.Scale(zoom_row, from_=0, to=len(tsw_livemap.ZOOMS) - 1, length=round(110 * self.k),
                                        value=tsw_livemap.ZOOMS.index(self.s["map_zoom"]), command=self.on_map_zoom)
        self.map_zoom_scale.pack(side="left")
        self.map_zoom_scale.bind("<ButtonRelease-1>", self.on_map_zoom_done)
        ttk.Label(zoom_row, text="Further", style="Muted.TLabel").pack(side="left", padx=(6, 10))
        self.map_zoom_label = ttk.Label(zoom_row, text="", style="Panel.TLabel")
        self.map_zoom_label.pack(side="left")
        self.on_map_size(self.s["map_window"])

        bottom = ttk.Frame(setp, style="Panel.TFrame")
        bottom.pack(fill="x", pady=(8, 0))
        self.top_var = tk.BooleanVar(value=bool(self.s["on_top"]))
        ttk.Checkbutton(bottom, text="Keep this window on top", variable=self.top_var,
                        style="Panel.TCheckbutton", command=self.on_top).pack(side="left")
        self.speed_var = tk.BooleanVar(value=bool(self.s["speed_popup"]))
        ttk.Checkbutton(bottom, text="Show the limit when speeding", variable=self.speed_var,
                        style="Panel.TCheckbutton", command=self.on_speed_popup).pack(side="left", padx=(16, 0))
        self.stop_panel_var = tk.BooleanVar(value=bool(self.s["stop_panel"]))
        ttk.Checkbutton(bottom, text="Show what to do at stops", variable=self.stop_panel_var,
                        style="Panel.TCheckbutton", command=self.on_stop_panel).pack(side="left", padx=(16, 0))
        ttk.Button(bottom, text="Re-detect train", command=self.on_redetect).pack(side="right")
        bottom2 = ttk.Frame(setp, style="Panel.TFrame")
        bottom2.pack(fill="x", pady=(4, 0))
        self.comfort_var = tk.BooleanVar(value=bool(self.s["comfort"]))
        ttk.Checkbutton(bottom2, text="Show passenger comfort", variable=self.comfort_var,
                        style="Panel.TCheckbutton", command=self.on_comfort).pack(side="left")
        ttk.Button(bottom2, text="Service history", command=self.on_history).pack(side="right")

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

    def on_stop_key(self, _event=None):
        key = self.stop_key_combo.get()
        self.s["stop_key"] = key if key in STOP_KEYS else None
        if self.s["stop_key"] and self.s["map_key"] == key:      # one key can't do two things
            self.s["map_key"] = None
            self.map_key_combo.set(NO_KEY)
        save_settings(self.s)
        self.log(f"{key} now shows the next stop while held" if self.s["stop_key"] else "Next stop key cleared")

    def on_map_key(self, _event=None):
        key = self.map_key_combo.get()
        self.s["map_key"] = key if key in MAP_KEYS else None
        if self.s["map_key"] and self.s["stop_key"] == key:
            self.s["stop_key"] = None
            self.stop_key_combo.set(NO_KEY)
        save_settings(self.s)
        self.log(f"{key} now shows and hides the route map" if self.s["map_key"] else "Route map key cleared")

    def on_map_opacity(self, value):
        self.s["map_opacity"] = round(float(value), 2)
        self.map_opacity_label.config(text=f"{round((1 - self.s['map_opacity']) * 100)}%")
        if getattr(self, "map_overlay", None) is not None:
            self.map_overlay.set_opacity(self.s["map_opacity"])

    def on_map_size(self, value):
        self.s["map_window"] = round(float(value), 2)
        w, h = self._map_view()
        self.map_size_label.config(text=f"{w} × {h} px")
        self._show_map_zoom()

    def on_map_size_done(self, _event=None):
        save_settings(self.s)
        self.map_watch.view = self._map_view()
        self.map_overlay.resize(self.map_watch.view)

    def on_map_zoom(self, value):
        self.s["map_zoom"] = tsw_livemap.ZOOMS[round(float(value))]
        if self.s["map_auto_zoom"]:                   # a zoom picked: not by speed any more
            self._set_auto_zoom(False)
        self._show_map_zoom()

    def on_map_zoom_done(self, _event=None):
        self.map_zoom_scale.set(tsw_livemap.ZOOMS.index(self.s["map_zoom"]))
        save_settings(self.s)
        if self.map_watch.zoom != self.s["map_zoom"]:
            self.map_watch.zoom = self.s["map_zoom"]
            self.map_watch.last_check = 0.0           # drawn at that zoom (about 15 s the first time)

    def on_map_auto_zoom(self):
        self._set_auto_zoom(bool(self.map_auto_var.get()))

    def _set_auto_zoom(self, on):
        """The route map zooms by speed (tsw_livemap.speed_zoom), or stays at the zoom picked."""
        self.s["map_auto_zoom"] = on
        self.map_auto_var.set(on)
        save_settings(self.s)
        self.map_watch.auto = on
        if not on:
            self.map_watch.zoom = self.s["map_zoom"]
        self.map_watch.last_check = 0.0
        self._show_map_zoom()

    def _show_map_zoom(self):
        w = self._map_view()[0]
        if self.s["map_auto_zoom"]:
            near, far = (w * z for z in (tsw_livemap.AUTO_ZOOMS[0], tsw_livemap.AUTO_ZOOMS[-1]))
            self.map_zoom_label.config(text=f"by speed: {near / 1000:.1f}-{far / 1000:.1f} km "
                                            f"({near / 1609.344:.1f}-{far / 1609.344:.1f} mi) across")
            return
        across = w * self.s["map_zoom"]
        self.map_zoom_label.config(text=f"{self.s['map_zoom']:g} m a pixel: {across / 1000:.1f} km "
                                        f"({across / 1609.344:.1f} mi) across")

    def on_map_corner(self, _event=None):
        self.s["map_corner"] = self.map_corner_combo.get()
        save_settings(self.s)
        self.map_overlay.corner = self.s["map_corner"]
        self.map_overlay._place()

    def _map_view(self):
        """The route map's size on the screen, (width, height) in pixels: its share of the screen's width."""
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = int(sw * self.s["map_window"]) // 4 * 4
        return w, min(int(w * 0.68) // 4 * 4, int(sh * 0.9))

    def _toggle_map(self):
        """Its joystick button or key shows the route map, or hides it."""
        self.map_shown = not self.map_shown
        self.map_watch.shown = self.map_shown
        self.tracker.shown = bool(self.stop_held) or self.map_shown
        if self.map_shown:
            self.map_watch.wanted = True          # from now on, maps are made as services start
            self.map_watch.last_check = 0.0

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

    def on_speed_popup(self):
        self.s["speed_popup"] = bool(self.speed_var.get())
        save_settings(self.s)

    def on_stop_panel(self):
        self.s["stop_panel"] = bool(self.stop_panel_var.get())
        save_settings(self.s)

    def on_comfort(self):
        self.s["comfort"] = bool(self.comfort_var.get())
        save_settings(self.s)

    def on_history(self):
        if self.history_window is not None and self.history_window.win.winfo_exists():
            self.history_window.win.destroy()           # open it again with the runs kept since
        self.history_window = HistoryWindow(self.root, self.k, self.history)

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
        self.tracker.running = False
        self.speed_watch.running = False
        self.comfort.running = False
        self.map_watch.stop()
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
        self._show_stop("button", False)

    def _show_stop(self, source, down):
        """The next stop shows while its joystick button or its key ("button" / "key") is held."""
        shown = bool(self.stop_held)
        if down:
            self.stop_held.add(source)
        else:
            self.stop_held.discard(source)
        if bool(self.stop_held) == shown:
            return
        self.tracker.shown = bool(self.stop_held) or self.map_shown      # the map says how far it is too
        if self.stop_held:
            self.overlay.show(self.tracker.state, self.speed_watch)
        else:
            self.overlay.hide()

    def _stop_key_held(self):
        """The next stop key is down while the game or this window has the focus (not while typing elsewhere)."""
        return App._key_held(self, STOP_KEYS.get(self.s["stop_key"]))

    def _key_held(self, vk):
        """A key (Windows key code) is down while the game or this window has the focus."""
        if not vk or not ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000:
            return False
        pid = autostart.foreground_pid()
        if pid != self.fg_pid:
            self.fg_pid, self.fg_ok = pid, pid == os.getpid() or autostart.is_game(pid)
        return self.fg_ok

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
                elif ev.button == self.s["stop_button"]:
                    self._show_stop("button", down)
                elif down and ev.button == self.s["map_button"]:
                    self._toggle_map()
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
    def _ride(self, stop):
        """The ride report for the station you're stopped at (tsw_stops.StopTracker.stop): it follows the ride as
        the judging catches up with the stop, and stays as it was once you've left; leaving starts the next ride.
        A game save loaded since you got to the station takes you away from it without leaving it."""
        if self.comfort.restores != self.restores:
            self.restores, self.at_station, self.ride = self.comfort.restores, None, None
        if stop and stop["phase"] != "departed":
            self.at_station, self.at_name, self.at_first = stop["service"], stop["station"], bool(stop.get("first"))
            self.ride = self.comfort.report(stop["service"], self.at_first)
        elif self.at_station is not None:
            self.comfort.new_leg(self.at_station, self.at_name, self.at_first, self.restores)
            self.at_station = None
        return self.ride if stop else None

    def tick(self):
        self.frame += 1
        try:
            self._sync_handle_speed()
            self._poll_joystick()
            self._show_stop("key", self._stop_key_held())
            map_key = self._key_held(MAP_KEYS.get(self.s["map_key"]))
            if map_key and not self.map_key_down:
                self._toggle_map()
            self.map_key_down = map_key
            self._update_status()
            self._draw()
            stop = self.tracker.stop
            ride = self._ride(stop)
            run = self.history.finish(stop, self.comfort.service(stop["service"], stop["station"])) \
                if stop and stop["phase"] == "end" else None
            at_stop = stop if self.s["stop_panel"] else None
            if at_stop:
                self.dwell_overlay.show(at_stop, self.tracker.state, self.speed_watch.imperial,
                                        ride if self.s["comfort"] else None)
                if run:                                      # after it: these go under that panel
                    self.summary_overlay.show(run, self.history.best_before(run), self.history.count(run["service"]))
                    self.schedule_overlay.hide()
                else:
                    self.schedule_overlay.show(at_stop)
                    self.summary_overlay.hide()
            else:
                self.dwell_overlay.hide()
                self.schedule_overlay.hide()
                self.summary_overlay.hide()
            if self.overlay.shown:
                self.overlay.update(self.tracker.state, self.speed_watch)
            speeding = self.speed_watch.state if self.s["speed_popup"] else None
            if speeding:
                self.speed_overlay.show(speeding, self.speed_watch.imperial)
            else:
                self.speed_overlay.hide()
            coach = self.comfort.coach if self.s["comfort"] else None
            if coach:
                self.comfort_overlay.show(coach)             # after the speeding popup: it goes under that
            else:
                self.comfort_overlay.hide()
            raised = False                           # the map goes under the panels
            self.map_watch.speed = self.bridge.speed     # for the zoom by speed
            if self.map_shown:
                raised = not self.map_overlay.shown
                self.map_overlay.show(self.map_watch, self.tracker.state, self.speed_watch.imperial)
            else:
                self.map_overlay.hide()
            raised = self.map_overlay.keep_on_top() or raised
            for overlay in (self.dwell_overlay, self.schedule_overlay, self.summary_overlay, self.overlay,
                            self.speed_overlay, self.comfort_overlay):
                if raised:
                    overlay.raise_()
                else:
                    overlay.keep_on_top()
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
