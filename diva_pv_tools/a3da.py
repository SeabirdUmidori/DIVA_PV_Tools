"""A3DA camera files: the text property format Project DIVA PVs use for their camera.

Decoded from shipped files rather than guessed, because the two conventions that exist in the
wild differ and both load:

  sega (2009-2014 vanilla)     sparse keys, view_point.fov (+fov_is_horizontal=1)
  3dpv (2026 ports)            one key per frame, view_point.focal_length + camera_aperture_w/h

Per channel, exactly one of these three shapes appears:

  <path>.type=0                                  constant zero, no value line
  <path>.type=1 <path>.value=<v>                 constant
  <path>.type=2|3 <path>.max=<frames> <path>.key.length=<n> <n> keys
                                                 2 = linear, 3 = smooth (and then the shipped
                                                 Sega files add a per-key tangent, which the
                                                 3DPV files leave out)

and a key is either

  <path>.key.<i>.data=(<frame>,<value>[,<tangent>])  + .type=1   ordinary sample
  <path>.key.<i>.data=<frame>                        + .type=0   sample whose value is 0

The whole file is one java Properties dump, so every line is sorted by key as a string and
the header is "#A3DA__________" plus a "#<ctime>" line.

usage: python -m diva_pv_tools.a3da <file.a3da>            # summary
       python -m diva_pv_tools.a3da <file.a3da> <channel>  # dump one channel's keys
"""
import re
import sys
import time

KEY_RE = re.compile(r"^(.*)\.key\.(\d+)\.(data|type)$")
HEADER = "#A3DA__________"


class Channel:
    """One animated or constant property channel."""

    def __init__(self, kind=0, const=None, max_frames=None, kind_anim=3):
        self.kind = kind                # 0 zero, 1 constant, 2/3 animated
        self.const = const
        self.max_frames = max_frames
        self.kind_anim = kind_anim
        self.keys = []                  # [(frame, value)]
        self.tangent = {}               # frame -> verbatim 3rd field, only Sega files carry it
        self.ktype = {}                 # frame -> the declared key type, preserved on rewrite

    @property
    def animated(self):
        return self.kind in (2, 3)

    def add(self, frame, value):
        self.keys.append((int(frame), float(value)))

    def at(self, frame):
        """Linear read of the channel at a frame (constants answer immediately)."""
        if not self.animated:
            return float(self.const or 0.0)
        ks = self.keys
        if not ks:
            return 0.0
        if frame <= ks[0][0]:
            return ks[0][1]
        if frame >= ks[-1][0]:
            return ks[-1][1]
        lo, hi = 0, len(ks) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if ks[mid][0] <= frame:
                lo = mid
            else:
                hi = mid
        (f0, v0), (f1, v1) = ks[lo], ks[hi]
        return v0 if f1 == f0 else v0 + (v1 - v0) * (frame - f0) / (f1 - f0)

    def frames(self):
        return [f for f, _ in self.keys]

    def values(self):
        return [v for _, v in self.keys]


class A3DA:
    def __init__(self):
        self.channels = {}          # dotted path -> Channel
        self.props = {}             # everything else, kept verbatim
        self.order = []

    # ---- reading -------------------------------------------------------
    @classmethod
    def read(cls, path):
        got = cls()
        pending = {}
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            name, _, raw = line.partition("=")
            m = KEY_RE.match(name)
            if m:
                slot = pending.setdefault(m.group(1), {})
                slot.setdefault(int(m.group(2)), {})[m.group(3)] = raw
                continue
            got.props[name] = raw
        for path_, slots in pending.items():
            ch = Channel()
            head = got.props.pop(path_ + ".type", None)
            ch.kind_anim = int(head) if head is not None else 3
            ch.kind = ch.kind_anim
            ch.max_frames = int(got.props.pop(path_ + ".max", 0) or 0)
            got.props.pop(path_ + ".key.length", None)
            for i in sorted(slots):
                raw = slots[i].get("data", "")
                kind = int(slots[i].get("type", "1"))
                if kind == 0:
                    f = int(float(raw))
                    ch.keys.append((f, 0.0))
                else:
                    parts = raw.strip("()").split(",")
                    f, v = int(float(parts[0])), float(parts[1])
                    if len(parts) > 2:
                        ch.tangent[f] = ",".join(p.strip() for p in parts[2:])
                    ch.keys.append((f, v))
                ch.ktype[f] = kind
            got.channels[path_] = ch
        for name, raw in list(got.props.items()):
            if name.endswith(".type"):
                stem = name[:-len(".type")]
                if stem not in got.channels and raw == "1":
                    ch = Channel(kind=1, const=float(got.props.get(stem + ".value", 0.0)))
                    got.channels[stem] = ch
                    got.props.pop(stem + ".value", None)
                elif stem not in got.channels and raw == "0":
                    got.channels[stem] = Channel(kind=0, const=0.0)
        return got

    def channel(self, path):
        return self.channels.setdefault(path, Channel(kind=0, const=0.0))

    # ---- writing -------------------------------------------------------
    def write(self, path, stamp=None):
        """Emit a java-Properties-shaped file: every line sorted by property name."""
        out = [HEADER, "#" + (stamp or time.strftime("%a %b %d %H:%M:%S %Y"))]
        lines = list(self.props)
        rows = {}
        for name in lines:
            rows[name] = self.props[name]
        for name, ch in self.channels.items():
            if not ch.animated:
                rows["%s.type" % name] = str(ch.kind)
                if ch.kind == 1:
                    rows["%s.value" % name] = num(ch.const)
                continue
            rows["%s.type" % name] = str(ch.kind_anim)
            rows["%s.max" % name] = str(ch.max_frames)
            rows["%s.key.length" % name] = str(len(ch.keys))
            for i, (f, v) in enumerate(sorted(ch.keys, key=lambda k: k[0])):
                tan = ch.tangent.get(f)
                if ch.ktype.get(f) == 0 and tan is None:
                    rows["%s.key.%d.data" % (name, i)] = str(f)
                    rows["%s.key.%d.type" % (name, i)] = "0"
                else:
                    body = "%d,%s" % (f, num(v))
                    if tan is not None:
                        body += "," + tan
                    rows["%s.key.%d.data" % (name, i)] = "(%s)" % body
                    rows["%s.key.%d.type" % (name, i)] = "2" if tan is not None else "1"
        for name in sorted(rows):
            out.append("%s=%s" % (name, rows[name]))
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(out) + "\n")
        return len(out)


def num(v):
    """Shipped files carry 7-9 significant digits; 10 digits: the shipped files print that many."""
    return "%.10g" % v


SCAFFOLD = {
    "_.converter.version": "20050823",
    "_.property.version": "20050706",
    "camera_root.length": "1",
    "play_control.begin": "0",
    "play_control.fps": "60",
}
CONST_SCAFFOLD = [
    ("camera_root.0.interest.rot.%s" % a, 0) for a in "xyz"
] + [
    ("camera_root.0.interest.scale.%s" % a, 1) for a in "xyz"
] + [
    ("camera_root.0.rot.%s" % a, 0) for a in "xyz"
] + [
    ("camera_root.0.trans.%s" % a, 0) for a in "xyz"
] + [
    ("camera_root.0.scale.%s" % a, 1) for a in "xyz"
] + [
    ("camera_root.0.view_point.rot.%s" % a, 0) for a in "xyz"
] + [
    ("camera_root.0.view_point.scale.%s" % a, 1) for a in "xyz"
] + [
    ("camera_root.0.interest.visibility", 1),
    ("camera_root.0.view_point.visibility", 1),
    ("camera_root.0.visibility", 1),
]


def new(file_name, size, aspect=1.77778):
    """A file skeleton matching the shipped ones, so only the six real tracks need filling."""
    got = A3DA()
    got.props = dict(SCAFFOLD)
    got.props["_.file_name"] = file_name
    got.props["play_control.size"] = str(size)
    got.props["camera_root.0.view_point.aspect"] = num(aspect)
    for name, value in CONST_SCAFFOLD:
        got.channels[name] = Channel(kind=1 if value else 0, const=float(value))
    return got


def main(argv):
    got = A3DA.read(argv[0])
    if len(argv) > 1:
        ch = got.channels[argv[1]]
        print("%s kind=%d keys=%d" % (argv[1], ch.kind, len(ch.keys)))
        for f, v in ch.keys:
            print("  %6d %14.6f" % (f, v))
        return 0
    print("%s  props=%d channels=%d" % (argv[0], len(got.props), len(got.channels)))
    for k in sorted(got.props):
        print("  %-40s %s" % (k, got.props[k]))
    for name in sorted(got.channels):
        ch = got.channels[name]
        span = "%d..%d" % (ch.keys[0][0], ch.keys[-1][0]) if ch.keys else "-"
        print("  %-46s kind=%d const=%-10s keys=%-6d %s"
              % (name, ch.kind, ch.const if not ch.animated else "-", len(ch.keys), span))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
