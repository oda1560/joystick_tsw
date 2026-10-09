"""
Your points for the service you're driving, from the game's checkpoint save.

The game's API doesn't give the score, but the game saves a checkpoint each time a timetable step is done (on
arriving at a stop and on leaving it): Saved/SaveGames/<id>/TSWCheckpointSaveGame_*.sav. In it, compressed, is
the score record: CurrentHUDScore (the points) and, per service, the stops made so far (due and arrival time, how
far from the stop marker). Seen on Airedale-Wharfedale 2P27: 900 points after arriving at Frizinghall, 1150 after
leaving on time.

    points = Points()
    points.read(services, game_seconds)   # None, or {"points": 1150, "change": 250 (since the save before),
                                          #           "late": -55.0 (seconds, at the last stop), "off": 6.1 (m)}
                                          # `services`: the names the service you're driving may go by
"""

import glob
import os
import struct
import zlib

import tsw_joystick as core
import tsw_paks

SAVE_DIR = os.path.join(os.path.dirname(os.path.dirname(core.KEY_FILE)), "SaveGames")
SAVE_GLOB = "TSWCheckpointSaveGame_*.sav"
CHUNK_TAG = 0x9E2A83C1            # Unreal's compressed chunk tag
STALE_SECONDS = 3 * 3600          # a save made more than this game time ago is from another run


class _Save(tsw_paks.Package):
    """A save game file (GVAS): tagged properties with names written out in full."""

    def __init__(self, data):
        r = tsw_paks.Reader(data)
        if r.read(4) != b"GVAS":
            raise ValueError("not a save game")
        r.i32(), r.i32()
        r.u16(), r.u16(), r.u16(), r.u32(), r.fstr()
        r.i32()
        for _ in range(r.i32()):
            r.read(20)
        r.fstr()
        self.names, self.imports, self.exports, self._props, self._keep = [], [], [], {}, None
        self.name, self._data, self._start = "save", data, r.tell()

    def fname(self, r):
        return r.fstr()

    def _value(self, r, typ, tag, size):
        if typ in tsw_paks.OBJECT_PROPS or typ in ("SoftObjectProperty", "SoftClassProperty"):
            return r.read(size)
        return super()._value(r, typ, tag, size)

    def _array(self, r, inner, size):
        if inner == "ByteProperty":                  # the compressed data, as bytes
            return r.read(r.i32())
        if inner in ("NameProperty", "StrProperty"):
            return [r.fstr() for _ in range(r.i32())]
        return super()._array(r, inner, size)

    def read(self, keep):
        self._keep = keep
        try:
            return self._tagged(tsw_paks.Reader(self._data[self._start:]))
        finally:
            self._keep = None


class _Data(tsw_paks.Package):
    """The save's unpacked data: tagged properties, each name a 4-byte index into the save's name table (-1:
    None)."""

    def __init__(self, data, names):
        self.names, self.imports, self.exports, self._props, self._keep = names, [], [], {}, None
        self.name, self._data = "save data", data

    def fname(self, r):
        i = r.i32()
        if i == -1:
            return "None"
        if not 0 <= i < len(self.names):
            raise ValueError("bad name index")
        return self.names[i]

    def _value(self, r, typ, tag, size):
        if typ in tsw_paks.OBJECT_PROPS or typ in ("SoftObjectProperty", "SoftClassProperty"):
            return r.read(size)
        return super()._value(r, typ, tag, size)

    def tagged_at(self, pos):
        r = tsw_paks.Reader(self._data)
        r.seek(pos)
        return self._tagged(r)


def _unpack(raw):
    """Unreal's chunked zlib compression."""
    out, pos = [], 0
    while pos < len(raw):
        tag, chunk = struct.unpack_from("<qq", raw, pos)
        if tag != CHUNK_TAG:
            raise ValueError("not compressed data")
        _, total = struct.unpack_from("<qq", raw, pos + 16)
        pos += 32
        sizes = [struct.unpack_from("<qq", raw, pos + 16 * i)[0] for i in range((total + chunk - 1) // chunk)]
        pos += 16 * len(sizes)
        for size in sizes:
            out.append(zlib.decompress(raw[pos:pos + size]))
            pos += size
    return b"".join(out)


def read_save(path):
    """(service, game seconds when saved, score record) from a checkpoint save."""
    with open(path, "rb") as f:
        save = _Save(f.read())
    top = save.read({"NameTable": None, "CompressedSaveData": None, "CurrentTimetableService": None,
                     "TODSettings": {"LocalDateTime": None}})
    names = top["NameTable"]["Names"]
    index = {n: i for i, n in enumerate(names)}
    data = _unpack(top["CompressedSaveData"])
    pos = data.find(struct.pack("<ii", index["CurrentHUDScore"], index["IntProperty"]))
    if pos < 0:
        raise ValueError("no score in the save")
    saved = (top.get("TODSettings") or {}).get("LocalDateTime")
    return top.get("CurrentTimetableService"), saved / 1e7 if saved else None, _Data(data, names).tagged_at(pos)


class Points:
    """Reads the newest checkpoint save again whenever the game writes it."""

    def __init__(self):
        self.seen = None              # (path, modified) of the save read last
        self.save = None              # (service, game seconds, record) from it
        self.before = None            # points in the save before that, for the change

    def read(self, services, game_seconds):
        try:
            path = max(glob.glob(os.path.join(SAVE_DIR, "*", SAVE_GLOB)), key=os.path.getmtime)
            seen = (path, os.path.getmtime(path))
        except (ValueError, OSError):
            return None
        if seen != self.seen:
            self.seen = seen
            try:
                save = read_save(path)
            except (OSError, ValueError, KeyError, IndexError, TypeError, struct.error, zlib.error):
                save = None                 # half written, say: the one before stands until the game next writes
            if save is not None:
                if self.save and self.save[0] == save[0]:
                    self.before = self.save[2].get("CurrentHUDScore")
                else:
                    self.before = None
                self.save = save
        if self.save is None:
            return None
        name, saved, record = self.save
        if name not in services or saved is None or game_seconds is None or \
                not 0 <= game_seconds - saved < STALE_SECONDS:
            return None                     # another service, or another run of it
        out = {"points": record.get("CurrentHUDScore"), "change": None, "late": None, "off": None}
        if self.before is not None and out["points"] is not None:
            out["change"] = out["points"] - self.before
        stops = [s for st in record.get("ServiceStatistics") or [] for s in st.get("RecordedStops") or []]
        if stops:
            out["late"] = (stops[-1]["ArrivalTime"] - stops[-1]["DueTime"]) / 1e7
            out["off"] = stops[-1].get("StopAccuracy", 0) / 100
        return out
