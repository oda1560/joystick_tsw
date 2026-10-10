"""
Reads train definitions straight from Train Sim World's installed .pak files, without the game running.

Only as much of Unreal Engine 4.26's formats as is needed to find a rail vehicle's cab controls and their
settings: pak archives (version 11, unencrypted, Zlib) and cooked packages with tagged properties.

    files = GameFiles()                                  # finds the TSW7 install through Steam
    for cls, packages in files.vehicle_classes().items(): ...   # "RVM_..._C" names, as the game reports them
    comps = files.components(cls, packages[0])           # {name: Component} incl. inherited ones
"""

import glob
import io
import os
import re
import struct
import threading
import zlib

PAK_MAGIC = 0x5A6F12E1
STEAM_LIBRARIES = [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
GAME_DIR = os.path.join("steamapps", "common", "Train Sim World 7", "WindowsNoEditor")


class Reader(io.BytesIO):
    def i8(self): return struct.unpack("<b", self.read(1))[0]
    def u8(self): return self.read(1)[0]
    def i16(self): return struct.unpack("<h", self.read(2))[0]
    def u16(self): return struct.unpack("<H", self.read(2))[0]
    def i32(self): return struct.unpack("<i", self.read(4))[0]
    def u32(self): return struct.unpack("<I", self.read(4))[0]
    def i64(self): return struct.unpack("<q", self.read(8))[0]
    def u64(self): return struct.unpack("<Q", self.read(8))[0]
    def f32(self): return struct.unpack("<f", self.read(4))[0]
    def f64(self): return struct.unpack("<d", self.read(8))[0]

    def fstr(self):
        n = self.i32()
        if n == 0:
            return ""
        if n < 0:
            return self.read(-n * 2).decode("utf-16-le").rstrip("\0")
        if n > 1 << 20:
            raise ValueError("bad string length")
        return self.read(n).decode("latin-1").rstrip("\0")


# ---------------------------------------------------------------- pak archives
class Pak:
    def __init__(self, path):
        self.path = path
        self._f = None
        self._lock = threading.Lock()   # one file handle: the bridge and the stop tracker read from two threads
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400))
            tail = f.read()
            i = tail.rfind(struct.pack("<I", PAK_MAGIC))
            if i < 17:
                raise ValueError("not a pak file")
            if tail[i - 1]:
                raise ValueError("encrypted pak index")
            version, off, isz = struct.unpack("<IQQ", tail[i + 4:i + 24])
            if version < 10:
                raise ValueError(f"pak version {version} not supported")
            f.seek(off)
            r = Reader(f.read(isz))
            self.mount = r.fstr().replace("../../../", "")
            r.i32()
            r.u64()
            if r.u32():
                r.read(8 + 8 + 20)
            if not r.u32():
                raise ValueError("pak has no directory index")
            fdi_off, fdi_size = r.i64(), r.i64()
            r.read(20)
            self._encoded = r.read(r.i32())
            self._unencoded = [self._full_entry(r) for _ in range(r.i32())]
            f.seek(fdi_off)
            d = Reader(f.read(fdi_size))
        self.files = {}
        for _ in range(d.i32()):
            directory = d.fstr()
            for _ in range(d.i32()):
                name = d.fstr()
                self.files[(self.mount + directory + name).lstrip("/")] = d.i32()

    @staticmethod
    def _full_entry(r):
        off, size, usize = r.i64(), r.i64(), r.i64()
        method = r.u32()
        r.read(20)
        blocks = [(r.i64(), r.i64()) for _ in range(r.u32())] if method else []
        r.read(1 + 4)
        return off, usize, method, blocks, None

    def _entry(self, pos):
        if pos < 0:
            return self._unencoded[-pos - 1]
        r = Reader(self._encoded)
        r.seek(pos)
        v = r.u32()
        if (v & 0x3f) == 0x3f:
            r.u32()
        nblocks, method = (v >> 6) & 0xffff, (v >> 23) & 0x3f
        if v & (1 << 22):
            raise ValueError("encrypted file")
        off = r.u32() if v & (1 << 31) else r.u64()
        usize = r.u32() if v & (1 << 30) else r.u64()
        size = (r.u32() if v & (1 << 29) else r.u64()) if method else usize
        header = 8 + 8 + 8 + 4 + 20 + 1 + 4
        blocks = []
        if method:
            header += 4 + 16 * nblocks
            start = header
            for _ in range(nblocks):
                b = size if nblocks == 1 else r.u32()
                blocks.append((start, start + b))
                start += b
        return off, usize, method, blocks, header

    def read(self, name):
        off, usize, method, blocks, header = self._entry(self.files[name])
        with self._lock:
            if self._f is None:
                self._f = open(self.path, "rb")
            f = self._f
            if not method:
                f.seek(off + (header or 53))
                return f.read(usize)
            raw = []
            for a, b in blocks:
                f.seek(off + a)             # block offsets are relative to the entry
                raw.append(f.read(b - a))
        return b"".join(zlib.decompress(r) for r in raw)


# ---------------------------------------------------------------- cooked packages
# structs saved as raw bytes rather than as tagged properties: name -> reader
def _vec(n):
    return lambda r, p: [round(r.f32(), 5) for _ in range(n)]


NATIVE_STRUCTS = {
    "Vector": _vec(3), "Vector2D": _vec(2), "Vector4": _vec(4), "Rotator": _vec(3), "Quat": _vec(4),
    "LinearColor": _vec(4), "Plane": _vec(4),
    "Color": lambda r, p: list(r.read(4)),
    "Guid": lambda r, p: r.read(16).hex(),
    "IntPoint": lambda r, p: [r.i32(), r.i32()],
    "IntVector": lambda r, p: [r.i32(), r.i32(), r.i32()],
    "Box": lambda r, p: [_vec(3)(r, p), _vec(3)(r, p), r.u8()],
    "Box2D": lambda r, p: [_vec(2)(r, p), _vec(2)(r, p), r.u8()],
    "DateTime": lambda r, p: r.i64(), "Timespan": lambda r, p: r.i64(),
    "FrameNumber": lambda r, p: r.i32(),
    "GameplayTag": lambda r, p: p.fname(r),
    "GameplayTagContainer": lambda r, p: [p.fname(r) for _ in range(r.i32())],
    "SoftObjectPath": lambda r, p: [p.fname(r), r.fstr()],
    "SoftClassPath": lambda r, p: [p.fname(r), r.fstr()],
    "PerPlatformFloat": lambda r, p: (r.u8(), r.f32())[1],
    "PerPlatformInt": lambda r, p: (r.u8(), r.i32())[1],
    "RichCurveKey": lambda r, p: dict(zip(("Interp", "Tangent", "Weight"), r.read(3)),
                                      **dict(zip(("Time", "Value", "ArriveTangent", "ArriveWeight",
                                                  "LeaveTangent", "LeaveWeight"),
                                                 [round(r.f32(), 6) for _ in range(6)]))),
    "SimpleCurveKey": lambda r, p: {"Time": r.f32(), "Value": r.f32()},
    "FloatRange": lambda r, p: [(r.u8(), r.f32()), (r.u8(), r.f32())],
    "SpeedQuantity": lambda r, p: round(r.f32(), 5),          # m/s
    "Int32Range": lambda r, p: [(r.u8(), r.i32()), (r.u8(), r.i32())],
}
SCALARS = {"Int8Property": "i8", "Int16Property": "i16", "IntProperty": "i32", "Int64Property": "i64",
           "UInt16Property": "u16", "UInt32Property": "u32", "UInt64Property": "u64",
           "FloatProperty": "f32", "DoubleProperty": "f64"}
OBJECT_PROPS = ("ObjectProperty", "ClassProperty", "InterfaceProperty", "WeakObjectProperty")


class Ref(str):
    """A reference to another object, shown as its name; .package / .name / .outer locate it."""

    def __new__(cls, text, package=None, name=None, outer=None):
        s = super().__new__(cls, text)
        s.package, s.name, s.outer = package, name, outer
        return s


class Raw:
    def __init__(self, typ, size):
        self.typ, self.size = typ, size

    def __repr__(self):
        return f"<{self.typ} {self.size}b>"


class Package:
    def __init__(self, name, uasset, uexp):
        self.name = name
        r = Reader(uasset)
        r.u32()
        if r.i32() != -4:
            r.i32()
        r.i32(), r.i32()
        for _ in range(r.i32()):
            r.read(20)
        self.header_size = r.i32()
        r.fstr()
        self.flags = r.u32()
        if self.flags & 0x2000:
            raise ValueError("unversioned properties not supported")
        ncount, noff = r.i32(), r.i32()
        r.i32(), r.i32()
        ecount, eoff, icount, ioff = r.i32(), r.i32(), r.i32(), r.i32()
        r.seek(noff)
        self.names = []
        for _ in range(ncount):
            self.names.append(r.fstr())
            r.read(4)
        r.seek(ioff)
        self.imports = []
        for _ in range(icount):
            r.read(8)
            cls, outer, name = self.fname(r), r.i32(), self.fname(r)
            self.imports.append({"class": cls, "outer": outer, "name": name})
        r.seek(eoff)
        self.exports = []
        for _ in range(ecount):
            cls, sup, tmpl, outer = r.i32(), r.i32(), r.i32(), r.i32()
            name = self.fname(r)
            r.u32()
            size, off = r.i64(), r.i64()
            r.read(4 * 3 + 16 + 4 + 4 * 2 + 4 * 5)
            self.exports.append({"class": cls, "super": sup, "template": tmpl, "outer": outer, "name": name,
                                 "size": size, "offset": off})
        self._uexp = uexp
        self._props = {}
        self._keep = None

    def fname(self, r):
        i, n = r.i32(), r.i32()
        if not 0 <= i < len(self.names):
            raise ValueError("bad name index")
        s = self.names[i]
        return s if n == 0 else f"{s}_{n - 1}"

    def export(self, name, outer=None):
        for i, e in enumerate(self.exports):
            if e["name"] == name and (outer is None or self.ref(e["outer"]) == outer):
                return i + 1
        return None

    def ref(self, index):
        """Ref for an import (<0) or export (>0) index, or None."""
        if index > 0:
            e = self.exports[index - 1]
            return Ref(e["name"], self.name, e["name"], self.ref(e["outer"]) if e["outer"] else None)
        if index < 0:
            imp = self.imports[-index - 1]
            if imp["class"] == "Package":
                return Ref(imp["name"], imp["name"], None)
            outer = self.ref(imp["outer"]) if imp["outer"] else None
            pkg = outer
            while pkg is not None and pkg.name is not None:
                pkg = pkg.outer
            return Ref(imp["name"], pkg.package if pkg is not None else None, imp["name"],
                       outer if outer is not None and outer.name is not None else None)
        return None

    def class_name(self, index):
        e = self.exports[index - 1]
        return self.ref(e["class"]) or ""

    def properties(self, index, keep=None):
        """Tagged properties of an export, as {name: value} (nested structs as dicts). `keep` reads only some of
        them, much faster on big timetables: {name: None for all of it, or a `keep` for its own properties}."""
        if keep is not None:
            return self._read(index, keep)
        if index not in self._props:
            self._props[index] = self._read(index, None)
        return self._props[index]

    def _read(self, index, keep):
        e = self.exports[index - 1]
        a = e["offset"] - self.header_size
        self._keep = keep
        try:
            return self._tagged(Reader(self._uexp[a:a + e["size"]]))
        except Exception:
            return {}
        finally:
            self._keep = None

    # -------- tagged properties
    def _tagged(self, r):
        out = {}
        while True:
            name = self.fname(r)
            if name == "None":
                return out
            typ = self.fname(r)
            size, index = r.i32(), r.i32()
            tag = {}
            if typ == "StructProperty":
                tag["struct"] = self.fname(r)
                r.read(16)
            elif typ == "BoolProperty":
                tag["bool"] = bool(r.u8())
            elif typ in ("ByteProperty", "EnumProperty"):
                tag["enum"] = self.fname(r)
            elif typ in ("ArrayProperty", "SetProperty"):
                tag["inner"] = self.fname(r)
            elif typ == "MapProperty":
                tag["key"], tag["value"] = self.fname(r), self.fname(r)
            if r.u8():
                r.read(16)
            start = r.tell()
            keep = self._keep
            if keep is not None and name not in keep:
                r.seek(start + size)
                continue
            self._keep = keep[name] if keep is not None else None
            try:
                value = self._value(r, typ, tag, size)
            except Exception:
                value = Raw(typ, size)
            finally:
                self._keep = keep
            if r.tell() != start + size:
                r.seek(start + size)
                if not isinstance(value, Raw) and typ not in ("BoolProperty",):
                    value = Raw(typ, size)
            out[name if index == 0 else f"{name}[{index}]"] = value

    def _value(self, r, typ, tag, size):
        if typ == "BoolProperty":
            return tag["bool"]
        if typ in SCALARS:
            v = getattr(r, SCALARS[typ])()
            return round(v, 6) if isinstance(v, float) else v
        if typ == "ByteProperty":
            return r.u8() if size == 1 else self.fname(r)
        if typ in ("EnumProperty", "NameProperty"):
            return self.fname(r)
        if typ == "StrProperty":
            return r.fstr()
        if typ == "TextProperty":
            return self._text(r)
        if typ in OBJECT_PROPS:
            return self.ref(r.i32())
        if typ in ("SoftObjectProperty", "SoftClassProperty"):
            return [self.fname(r), r.fstr()]
        if typ == "StructProperty":
            return self._struct(r, tag["struct"])
        if typ == "ArrayProperty":
            return self._array(r, tag["inner"], size)
        if typ == "SetProperty":
            r.i32()
            return self._array(r, tag["inner"], size - 4)
        if typ == "MapProperty":
            for _ in range(r.i32()):
                self._element(r, tag["key"], None)
            out = []
            for _ in range(r.i32()):
                out.append((self._element(r, tag["key"], None), self._element(r, tag["value"], None)))
            return out
        r.read(size)
        return Raw(typ, size)

    def _text(self, r):
        r.u32()
        kind = r.i8()
        if kind == -1:
            return r.fstr() if r.u32() else ""
        if kind == 0:
            r.fstr(), r.fstr()
            return r.fstr()
        if kind == 11:
            return f"{self.fname(r)}:{r.fstr()}"
        raise ValueError("text kind")

    def _struct(self, r, struct):
        if struct in NATIVE_STRUCTS:
            return NATIVE_STRUCTS[struct](r, self)
        return self._tagged(r)

    def _element(self, r, typ, struct):
        if typ == "StructProperty":
            return self._struct(r, struct) if struct else self._tagged(r)
        if typ == "BoolProperty":
            return bool(r.u8())
        if typ == "ByteProperty":
            return r.u8()
        return self._value(r, typ, {}, 0)

    def _array(self, r, inner, size):
        n = r.i32()
        if inner == "StructProperty":
            self.fname(r), self.fname(r)
            r.i32(), r.i32()
            struct = self.fname(r)
            r.read(16)
            if r.u8():
                r.read(16)
            return [self._struct(r, struct) for _ in range(n)]
        if inner == "ByteProperty" and n and (size - 4) // n == 8:
            return [self.fname(r) for _ in range(n)]
        return [self._element(r, inner, None) for _ in range(n)]


# ---------------------------------------------------------------- the game's files
class Component:
    """A component of a rail vehicle: its class, the built-in (C++) class that is based on, and its settings,
    inherited ones included."""

    def __init__(self, name, cls, base, props):
        self.name, self.cls, self.base, self.props = name, cls, base, props

    def __repr__(self):
        return f"{self.name} ({self.cls})"


def find_game_dir():
    libraries = list(STEAM_LIBRARIES)
    for steam in STEAM_LIBRARIES:
        try:
            with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), encoding="utf-8") as f:
                libraries += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
        except OSError:
            pass
    for lib in libraries:
        d = os.path.join(lib, GAME_DIR)
        if os.path.isdir(d):
            return d
    return None


class GameFiles:
    def __init__(self, game_dir=None):
        self.game_dir = game_dir or find_game_dir()
        if not self.game_dir:
            raise FileNotFoundError("Train Sim World 7 install not found")
        content = os.path.join(self.game_dir, "TS2Prototype", "Content")
        paths = sorted(glob.glob(os.path.join(content, "Paks", "*.pak"))
                       + glob.glob(os.path.join(content, "DLC", "*.pak")))
        self.paks, self.errors, self.where = [], [], {}
        for path in paths:
            try:
                pak = Pak(path)
            except (OSError, ValueError) as e:
                self.errors.append((os.path.basename(path), str(e)))
                continue
            self.paks.append(pak)
            for name in pak.files:
                self.where[name] = pak          # patch paks (_P) sort after the ones they patch
        self.roots = {"Game": "TS2Prototype/Content/", "Engine": "Engine/Content/"}
        for name in self.where:
            m = re.match(r"(.*/Plugins/(?:.*/)?([^/]+)/Content/)", name)
            if m:
                self.roots.setdefault(m.group(2), m.group(1))
        self._packages = {}
        self._components = {}

    def file_of(self, package):
        """'/Game/Core/X' -> 'TS2Prototype/Content/Core/X' (without extension), or None."""
        m = re.match(r"/([^/]+)/(.*)", package or "")
        if not m or m.group(1) not in self.roots:
            return None
        return self.roots[m.group(1)] + m.group(2)

    def package(self, package):
        """Loaded Package for a name like '/Game/Core/X', or None (C++ / missing)."""
        if package not in self._packages:
            path = self.file_of(package)
            pkg = None
            if path and path + ".uasset" in self.where:
                try:
                    pkg = Package(package, self.where[path + ".uasset"].read(path + ".uasset"),
                                  self.where[path + ".uexp"].read(path + ".uexp")
                                  if path + ".uexp" in self.where else b"")
                except Exception:
                    pkg = None
            self._packages[package] = pkg
        return self._packages[package]

    def vehicle_classes(self):
        """{'RVM_..._C': [package name, ...]} for every rail vehicle blueprint in the game files. A few names
        are used by more than one pack, for trains that differ (RVM_TFW_Class150_DMSL_C is Cardiff City
        Commuter's Class 150 and the TfW Class 142 pack's, with different reversers): those list each one."""
        out = {}
        for name in self.where:
            m = re.search(r"/(RVM_[^/]+)\.uasset$", name)
            if not m:
                continue
            for pkg, root in self.roots.items():
                if name.startswith(root):
                    out.setdefault(m.group(1) + "_C", []).append(f"/{pkg}/{name[len(root):-len('.uasset')]}")
        return out

    def matching_package(self, cls, packages, names):
        """Of the packages a vehicle class is in (vehicle_classes()), the one for the train the game lists with
        these component names: the one with the fewest components that aren't in both. Where every pack has
        the same components, their cab controls are the same too (checked for each such name, 2026-10-10)."""
        if len(packages) == 1 or not names:
            return packages[0]
        live = {n.lower() for n in names}
        return min(packages, key=lambda p: len(live ^ {n.lower() for n in self.components(cls, p)}))

    # -------- objects and inheritance
    def resolve(self, ref):
        """(Package, export index) of an object reference, or (None, None) if it lives in C++."""
        if ref is None or ref.package is None:
            return None, None
        pkg = self.package(ref.package)
        if pkg is None:
            return None, None
        return pkg, pkg.export(ref.name, ref.outer.name if ref.outer is not None else None)

    def properties(self, pkg, index, depth=0):
        """An object's settings merged over those of its archetype (template), recursively."""
        e = pkg.exports[index - 1]
        merged = {}
        if e["template"] and depth < 20:
            tp, ti = self.resolve(pkg.ref(e["template"]))
            if ti:
                merged = dict(self.properties(tp, ti, depth + 1))
        merged.update(pkg.properties(index))
        return merged

    def native_class(self, pkg, index, depth=0):
        """The C++ class at the root of an object's archetype chain, e.g. 'PushButtonComponent' for a
        component of a blueprint class based on it."""
        e = pkg.exports[index - 1]
        template = pkg.ref(e["template"]) if e["template"] else None
        if template is None:
            return pkg.class_name(index)
        tp, ti = self.resolve(template)
        if ti is None or depth > 20:
            return template[len("Default__"):] if template.startswith("Default__") else pkg.class_name(index)
        return self.native_class(tp, ti, depth + 1)

    def component(self, name, pkg, index):
        return Component(name, pkg.class_name(index), self.native_class(pkg, index),
                         self.properties(pkg, index))

    def class_chain(self, cls, package):
        """[(Package, class export index), ...] from the class itself up through its blueprint parents."""
        chain = []
        pkg = self.package(package)
        index = pkg.export(cls) if pkg else None
        while pkg is not None and index:
            chain.append((pkg, index))
            parent = pkg.ref(pkg.exports[index - 1]["super"])
            pkg, index = self.resolve(parent)
        return chain

    def components(self, cls, package):
        """{name: Component} for a vehicle class, in the order the game creates them (and lists them):
        components made in C++, then those added in each blueprint from the base one down. Each one's
        settings are as overridden by the most derived blueprint."""
        if (cls, package) in self._components:
            return self._components[(cls, package)]
        chain = list(reversed(self.class_chain(cls, package)))
        if not chain:
            return {}
        templates = {}                     # name -> (Package, template export index)
        overrides = {}
        for pkg, ci in chain:
            for i, e in enumerate(pkg.exports, 1):
                if e["outer"] != ci:
                    continue
                kind = pkg.class_name(i)
                if kind == "SimpleConstructionScript":
                    for node in pkg.properties(i).get("AllNodes") or []:
                        np, ni = self.resolve(node)
                        if not ni:
                            continue
                        props = np.properties(ni)
                        name, template = props.get("InternalVariableName"), props.get("ComponentTemplate")
                        tp, ti = self.resolve(template) if isinstance(template, Ref) else (None, None)
                        if name and ti:
                            templates.setdefault(str(name), (tp, ti))
                elif kind == "InheritableComponentHandler":
                    for rec in pkg.properties(i).get("Records") or []:
                        key, template = rec.get("ComponentKey") or {}, rec.get("ComponentTemplate")
                        tp, ti = self.resolve(template) if isinstance(template, Ref) else (None, None)
                        if key.get("SCSVariableName") and ti:
                            overrides[str(key["SCSVariableName"])] = (tp, ti)
        found = {}
        pkg, ci = chain[-1]                # C++ components, with the most derived blueprint's settings
        cdo = pkg.export("Default__" + pkg.exports[ci - 1]["name"])
        for i, e in enumerate(pkg.exports, 1):
            if cdo and e["outer"] == cdo and pkg.class_name(i).endswith("Component"):
                found[e["name"]] = self.component(e["name"], pkg, i)
        for name, (tp, ti) in templates.items():
            tp, ti = overrides.get(name, (tp, ti))
            found.setdefault(name, self.component(name, tp, ti))
        self._components[(cls, package)] = found
        return found

    def pack_of(self, package):
        """Name of the .pak a package comes from, e.g. 'BRClass380' (DLC) or 'Base game'."""
        path = self.file_of(package)
        pak = self.where.get(path + ".uasset") if path else None
        if pak is None:
            return "?"
        name = os.path.splitext(os.path.basename(pak.path))[0]
        name = re.sub(r"^TS2Prototype-WindowsNoEditor-?", "", name)
        return name or "Base game"
