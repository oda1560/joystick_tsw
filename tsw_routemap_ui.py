"""
Train Sim World 7 route maps - desktop window for tsw_routemap.py.

Pick a route and a journey, or take the service you're driving in the game, and make a printable map of its speed
limits, stations and signals. The map opens in your browser to print. Maps are saved in a folder you choose
(route_maps next to this script to begin with), and the ones made before are listed to open again.

Usage:
    pythonw tsw_routemap_ui.py           (or double-click "Route maps.bat")
"""

import ctypes
import os
import queue
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, ttk

import tsw_joystick as core
import tsw_paks
import tsw_routemap as routemap
from tsw_joystick_ui import BG, PANEL, TRACK, FG, MUTED, GOOD, BAD, FONT, apply_style, panel

HERE = os.path.dirname(os.path.abspath(__file__))
TITLE = "TSW7 Route Maps"
SELECTED = "#3a414d"


def box(parent, title):
    """A titled panel like the bridge window's, for the caller to place."""
    p = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 10))
    ttk.Label(p, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 6))
    return p


def driving_service(library):
    """(route, timetable, Service) of the service you're driving, from the game; a message if there's none."""
    api = core.TSWApi()
    if not api.load_key():
        raise routemap.MapError("No API key - start TSW7 with -HTTPAPI")
    try:
        name = (api.get("DriverAid.PlayerInfo").get("Values") or {}).get("currentServiceName")
        timetable_id = api.get_value("Timetable.VehicleID")
    except Exception:
        raise routemap.MapError("The game isn't reachable - is it running?")
    if not name or name == "None":
        raise routemap.MapError("You're not driving a timetabled service in the game")
    found = library.find_driving(timetable_id, name)
    if not found:
        raise routemap.MapError(f"{name} isn't in the timetables found in the game files")
    return found


class App:
    def __init__(self, root):
        self.root = root
        self.k = root.winfo_fpixels("1i") / 96.0
        self.library = None
        self.route = None               # route whose journeys are listed
        self.groups = []                # its journeys: [((origin, stops), [(timetable, Service)])]
        self.listed = []                # those shown, after the search and short-move filter
        self.routes = []                # route definitions shown, in list order
        self.busy = 0
        self.jobs, self.results = queue.Queue(), queue.Queue()
        apply_style(root)
        self._style()
        self._build()
        threading.Thread(target=self._work, daemon=True).start()
        self.run(self._read_library, self._show_library, "Reading the game files...")
        self._refresh_recent()
        self._poll()

    # ---- layout
    def _style(self):
        st = ttk.Style(self.root)
        st.configure("TEntry", fieldbackground=TRACK, foreground=FG, insertcolor=FG, bordercolor=TRACK,
                     lightcolor=TRACK, darkcolor=TRACK, padding=4)
        st.configure("Vertical.TScrollbar", background=TRACK, troughcolor=PANEL, bordercolor=PANEL,
                     arrowcolor=MUTED, lightcolor=TRACK, darkcolor=TRACK)
        st.map("Vertical.TScrollbar", background=[("active", SELECTED)])
        st.configure("Horizontal.TProgressbar", background=GOOD, troughcolor=TRACK, bordercolor=TRACK,
                     lightcolor=GOOD, darkcolor=GOOD)

    def _list(self, parent, height, width=None):
        """A dark list box with a scroll bar."""
        frame = ttk.Frame(parent, style="Panel.TFrame")
        lb = tk.Listbox(frame, height=height, width=width or 20, bg=TRACK, fg=FG, selectbackground=SELECTED,
                        selectforeground=FG, highlightthickness=0, relief="flat", bd=0, activestyle="none",
                        font=(FONT, 10), exportselection=False)
        bar = ttk.Scrollbar(frame, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=bar.set)
        lb.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        return frame, lb

    def _search(self, parent, hint, on_change):
        row = ttk.Frame(parent, style="Panel.TFrame")
        ttk.Label(row, text=hint, style="Muted.TLabel").pack(side="left", padx=(0, 8))
        var = tk.StringVar()
        var.trace_add("write", lambda *_: on_change())
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
        return row, var

    def _build(self):
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        hdr = ttk.Frame(outer)
        hdr.pack(fill="x")
        ttk.Label(hdr, text=TITLE, style="Head.TLabel").pack(side="left")
        self.drive_btn = tk.Button(hdr, text="▶  Map the service I'm driving", command=self.on_driving,
                                   relief="flat", bd=0, cursor="hand2", font=(FONT, 10, "bold"), padx=14, pady=5,
                                   bg=TRACK, fg=FG, activebackground=SELECTED, activeforeground=FG)
        self.drive_btn.pack(side="right")
        ttk.Label(outer, text="Printable maps of the speed limits along a service, from the game's own route files.",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 10))

        # route and journey side by side
        pick = ttk.Frame(outer)
        pick.pack(fill="both", expand=True, pady=(0, 10))
        rp = box(pick, "ROUTE")
        rp.pack(side="left", fill="both", padx=(0, 10))
        row, self.route_find = self._search(rp, "Find", self._fill_routes)
        row.pack(fill="x", pady=(0, 6))
        frame, self.route_list = self._list(rp, 18, 34)
        frame.pack(fill="both", expand=True)
        self.route_list.bind("<<ListboxSelect>>", self.on_route)

        jp = box(pick, "JOURNEY")
        jp.pack(side="left", fill="both", expand=True)
        top = ttk.Frame(jp, style="Panel.TFrame")
        top.pack(fill="x", pady=(0, 6))
        row, self.journey_find = self._search(top, "Find a station or service", self._fill_journeys)
        row.pack(side="left", fill="x", expand=True)
        self.short_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Short moves", variable=self.short_var, style="Panel.TCheckbutton",
                        command=self._fill_journeys).pack(side="left", padx=(12, 0))
        frame, self.journey_list = self._list(jp, 12, 64)
        frame.pack(fill="both", expand=True)
        self.journey_list.bind("<<ListboxSelect>>", self.on_journey)
        self.journey_list.bind("<Double-Button-1>", lambda e: self.on_make())
        self.stops_label = ttk.Label(jp, text="", style="Muted.TLabel", justify="left",
                                     wraplength=round(560 * self.k))
        self.stops_label.pack(fill="x", pady=(8, 4))
        srow = ttk.Frame(jp, style="Panel.TFrame")
        srow.pack(fill="x")
        ttk.Label(srow, text="Service", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.service_combo = ttk.Combobox(srow, state="readonly", width=28)
        self.service_combo.pack(side="left")
        self.service_note = ttk.Label(srow, text="", style="Muted.TLabel")
        self.service_note.pack(side="left", padx=(10, 0))

        # making a map, and the ones made
        mp = panel(outer, "MAP")
        act = ttk.Frame(mp, style="Panel.TFrame")
        act.pack(fill="x")
        self.make_btn = tk.Button(act, text="MAKE MAP", command=self.on_make, relief="flat", bd=0, cursor="hand2",
                                  font=(FONT, 11, "bold"), padx=18, pady=5)
        self.make_btn.pack(side="left")
        self.status = ttk.Label(act, text="", style="Panel.TLabel")
        self.status.pack(side="left", padx=(14, 0))
        self.progress = ttk.Progressbar(act, mode="indeterminate", length=round(140 * self.k))
        self.progress.pack(side="right")
        dest = ttk.Frame(mp, style="Panel.TFrame")
        dest.pack(fill="x", pady=(10, 0))
        ttk.Label(dest, text="Save to", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.folder_label = ttk.Label(dest, text="", style="Panel.TLabel")
        self.folder_label.pack(side="left")
        self.default_btn = ttk.Button(dest, text="Default", command=self.on_default_folder)
        self.default_btn.pack(side="right")
        ttk.Button(dest, text="Change...", command=self.on_change_folder).pack(side="right", padx=(0, 6))
        ttk.Label(mp, text="MADE BEFORE  (double-click to open)", style="Section.TLabel").pack(anchor="w",
                                                                                         pady=(10, 4))
        rec = ttk.Frame(mp, style="Panel.TFrame")
        rec.pack(fill="x")
        frame, self.recent_list = self._list(rec, 5)
        frame.pack(side="left", fill="x", expand=True)
        self.recent_list.bind("<Double-Button-1>", lambda e: self.on_open())
        btns = ttk.Frame(rec, style="Panel.TFrame")
        btns.pack(side="left", fill="y", padx=(10, 0))
        ttk.Button(btns, text="Open", command=self.on_open).pack(fill="x")
        ttk.Button(btns, text="Folder", command=self.on_folder).pack(fill="x", pady=(6, 0))

        lp = panel(outer, "LOG")
        self.logtext = tk.Text(lp, height=4, bg=PANEL, fg=MUTED, relief="flat", bd=0, font=("Consolas", 9),
                               wrap="word", state="disabled", highlightthickness=0)
        self.logtext.pack(fill="x")
        self._refresh_buttons()

    # ---- background work, one job at a time
    def run(self, job, done, message):
        """Runs job() off the window's thread, then done(result) on it."""
        self.busy += 1
        self._say(message)
        self._refresh_buttons()
        self.jobs.put((job, done))

    def _work(self):
        while True:
            job, done = self.jobs.get()
            try:
                self.results.put(("done", done, job()))
            except routemap.MapError as e:
                self.results.put(("error", None, str(e)))
            except Exception as e:
                with open(os.path.join(HERE, "routemap_error.log"), "a", encoding="utf-8") as f:
                    traceback.print_exc(file=f)
                self.results.put(("error", None, f"{type(e).__name__}: {e}"))

    def say(self, text):
        """From the job: how it's going."""
        self.results.put(("say", None, text))

    def _poll(self):
        try:
            while True:
                kind, done, value = self.results.get_nowait()
                if kind == "say":
                    self._say(value)
                    self.log(value)
                    continue
                self.busy -= 1
                if kind == "error":
                    self._say(value, BAD)
                    self.log(value)
                else:
                    self._say("")
                    if done:
                        done(value)
                self._refresh_buttons()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _say(self, text, colour=FG):
        self.status.config(text=text, foreground=colour)

    def log(self, text):
        self.logtext.config(state="normal")
        self.logtext.insert("end", text + "\n")
        self.logtext.see("end")
        self.logtext.config(state="disabled")

    def _refresh_buttons(self):
        ready = self.library is not None and not self.busy
        can_make = ready and self.journey_list.curselection() != ()
        self.make_btn.config(state="normal" if can_make else "disabled",
                             bg=GOOD if can_make else TRACK, fg="#0b1a12" if can_make else MUTED,
                             activebackground=GOOD, activeforeground="#0b1a12", disabledforeground=MUTED)
        self.drive_btn.config(state="normal" if ready else "disabled", disabledforeground=MUTED)
        if self.busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    # ---- routes and journeys
    def _read_library(self):
        return routemap.Library(tsw_paks.GameFiles())

    def _show_library(self, library):
        self.library = library
        self.log(f"{len(library.routes)} routes with timetables found")
        self._fill_routes()

    def _fill_routes(self):
        if not self.library:
            return
        key = self.route_find.get().strip().lower()
        names = self.library.names
        self.routes = sorted((r for r in self.library.routes if key in names[r].lower()), key=lambda r: names[r])
        self.route_list.delete(0, "end")
        for r in self.routes:
            self.route_list.insert("end", names[r])
        if self.route in self.routes:
            i = self.routes.index(self.route)
            self.route_list.selection_set(i)
            self.route_list.see(i)

    def on_route(self, _event=None):
        sel = self.route_list.curselection()
        if not sel or self.routes[sel[0]] == self.route:
            return
        self.select_route(self.routes[sel[0]])

    def select_route(self, route, then=None):
        """Lists a route's journeys (read off the window's thread), then calls then()."""
        self.route = route
        self.groups, self.listed = [], []
        self._fill_journeys()

        def done(groups):
            if self.route == route:
                self.groups = groups
                self._fill_journeys()
                if then:
                    then()
        self.run(lambda: routemap.journeys(self.library.services(route)), done,
                 f"Reading the timetables of {self.library.names[route]}...")

    def _fill_journeys(self):
        key = self.journey_find.get().strip().lower()
        groups = self.groups if self.short_var.get() else routemap.main_journeys(self.groups)

        def matches(group):
            (origin, stops), members = group
            words = [origin, *stops] + [s.name for _, s in members] + [s.number for _, s in members]
            return not key or any(key in w.lower() for w in words)
        self.listed = [g for g in groups if matches(g)]
        self.journey_list.delete(0, "end")
        for g in self.listed:
            self.journey_list.insert("end", routemap.journey_label(g, most=4).replace(" -> ", "  →  "))
        self.on_journey()

    def on_journey(self, _event=None):
        sel = self.journey_list.curselection()
        if not sel:
            self.stops_label.config(text="Pick a journey." if self.groups else "")
            self.service_combo.config(values=[])
            self.service_combo.set("")
            self.service_note.config(text="")
        else:
            (origin, stops), members = self.listed[sel[0]]
            self.stops_label.config(text="Calls at:  " + "  ·  ".join(stops))
            self.service_combo.config(values=[s.name for _, s in members])
            self.service_combo.current(0)
            self.service_note.config(text=f"{len(members)} services make this journey" if len(members) > 1 else "")
        self._refresh_buttons()

    def select_journey(self, service_name):
        """Shows the journey a service makes, with that service picked."""
        self.journey_find.set("")
        self.short_var.set(not any(s.name == service_name for g in routemap.main_journeys(self.groups)
                                   for _, s in g[1]))
        self._fill_journeys()
        for i, (_, members) in enumerate(self.listed):
            names = [s.name for _, s in members]
            if service_name in names:
                self.journey_list.selection_clear(0, "end")
                self.journey_list.selection_set(i)
                self.journey_list.see(i)
                self.on_journey()
                self.service_combo.current(names.index(service_name))
                return

    # ---- maps
    def on_make(self):
        sel = self.journey_list.curselection()
        if not sel or self.busy or not self.library:
            return
        members = self.listed[sel[0]][1]
        timetable, service = members[max(self.service_combo.current(), 0)]
        self._make(self.route, timetable, service, [s.name for _, s in members])

    def _make(self, route, timetable, service, also):
        library = self.library
        self.run(lambda: routemap.make_map(library, route, timetable, service, also, say=self.say),
                 self._made, f"Making the map of {service.name}...")

    def _made(self, path):
        self.log(f"Made: {os.path.basename(path)}")
        self._refresh_recent()
        os.startfile(path)

    def on_driving(self):
        library = self.library
        if not library or self.busy:
            return

        def found(result):
            route, timetable, service = result
            self.log(f"You're driving {service.name} on {library.names[route]}")
            self.route_find.set("")
            self._fill_routes()
            if route in self.routes:
                i = self.routes.index(route)
                self.route_list.selection_clear(0, "end")
                self.route_list.selection_set(i)
                self.route_list.see(i)

            def listed():
                self.select_journey(service.name)
                group = next((g for g in self.groups if any(s.name == service.name for _, s in g[1])), None)
                also = [s.name for _, s in group[1]] if group else []
                self._make(route, timetable, service, also)
            if self.route == route and self.groups:
                listed()
            else:
                self.select_route(route, then=listed)
        self.run(lambda: driving_service(library), found, "Asking the game what you're driving...")

    # ---- the maps folder
    def _show_folder(self):
        folder = routemap.map_folder()
        default = os.path.normcase(folder) == os.path.normcase(routemap.OUT_DIR)
        text = folder if len(folder) <= 72 else folder[:28] + " ... " + folder[-40:]
        self.folder_label.config(text=text + ("    (default)" if default else ""))
        self.default_btn.state(["disabled"] if default else ["!disabled"])

    def on_change_folder(self):
        current = routemap.map_folder()
        folder = filedialog.askdirectory(parent=self.root, title="Where to save route maps",
                                         initialdir=current if os.path.isdir(current) else HERE, mustexist=False)
        if folder:
            self._set_folder(folder)

    def on_default_folder(self):
        self._set_folder(None)

    def _set_folder(self, folder):
        try:
            routemap.set_map_folder(folder)
        except OSError as e:
            self.log(f"Couldn't save the folder setting: {e}")
            return
        self.log(f"Maps are saved in {routemap.map_folder()} from now on")
        self._show_folder()
        self._refresh_recent()

    def _refresh_recent(self):
        """The maps in the folder (not other web pages that may be there), newest first."""
        folder = routemap.map_folder()

        def is_map(name):
            try:
                path = os.path.join(folder, name)
                if os.path.getsize(path) > 4_000_000:
                    return False
                with open(path, "rb") as f:
                    return b"tsw_routemap.py" in f.read()
            except OSError:
                return False
        try:
            files = [f for f in os.listdir(folder) if f.lower().endswith(".html") and is_map(f)]
            files.sort(key=lambda f: os.path.getmtime(os.path.join(folder, f)), reverse=True)
        except OSError:
            files = []
        self.recent_folder, self.recent = folder, files
        self.recent_list.delete(0, "end")
        for f in files:
            self.recent_list.insert("end", f[:-len(".html")])
        self._show_folder()

    def on_open(self):
        sel = self.recent_list.curselection()
        if sel:
            os.startfile(os.path.join(self.recent_folder, self.recent[sel[0]]))

    def on_folder(self):
        folder = routemap.map_folder()
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)
        except OSError as e:
            self.log(f"Can't open {folder}: {e.strerror or e}")


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.title(TITLE)
    App(root)
    root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # pythonw has no console: leave a trace next to the script
        with open(os.path.join(HERE, "routemap_error.log"), "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        raise
