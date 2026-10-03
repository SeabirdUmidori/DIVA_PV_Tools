"""FArC archive reader/writer matching the layout Adult Len's own archives ship.

All integers big-endian:
    0x00 char[4]  magic: "FArC" = members gzip-compressed, "FArc" = stored raw
    0x04 uint32   entry table length + 4, measured from offset 8
    0x08 uint32   data alignment
    0x0C ...      per member: NUL-terminated name, uint32 offset,
                  uint32 stored size, uint32 uncompressed size
    data          each member at its own offset, next member aligned

The bundled FarcPack writes 28-byte member records where Armoire writes 12; the
archives are packed here with Armoire's record size so the result matches his byte for byte.
"""

import gzip
import os
import struct
import zlib


class Entry:
    def __init__(self, name, offset, stored, original, data=None):
        self.name = name
        self.offset = offset
        self.stored = stored
        self.original = original
        self.data = data


def read(path):
    with open(path, "rb") as fh:
        blob = fh.read()
    magic = blob[:4]
    if magic not in (b"FArC", b"FArc"):
        raise ValueError(f"{path}: not a FArC archive ({magic!r})")
    compressed = magic == b"FArC"
    table_end, alignment = struct.unpack_from(">II", blob, 4)

    # Compressed archives carry offset+stored+original per member; plain ones carry
    # offset+stored only, with the rest of the table padded by 0x78.
    fields = 3 if compressed else 2
    entries = []
    pos = 12
    while pos < 8 + table_end:
        end = blob.index(b"\x00", pos)
        name = blob[pos:end].decode("utf-8")
        nums = struct.unpack_from(f">{fields}I", blob, end + 1)
        original = nums[2] if compressed else nums[1]
        entries.append(Entry(name, nums[0], nums[1], original))
        pos = end + 1 + 4 * fields

    for e in entries:
        raw = blob[e.offset:e.offset + e.stored]
        e.data = zlib.decompress(raw, 16 + zlib.MAX_WBITS) if compressed else raw
        if len(e.data) != e.original:
            raise ValueError(f"{path}:{e.name} size mismatch {len(e.data)} != {e.original}")
    return compressed, alignment, entries


def unpack(src, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    _compressed, _alignment, entries = read(src)
    for e in entries:
        with open(os.path.join(out_dir, e.name), "wb") as fh:
            fh.write(e.data)
    return [e.name for e in entries]


def write(path, members, compressed=True, alignment=16):
    """members: sequence of (name, bytes)."""
    stored = [gzip.compress(data, mtime=0) if compressed else data for _, data in members]
    record = 12 if compressed else 8
    table_len = sum(len(name.encode("utf-8")) + 1 + record for name, _ in members)
    data_start = ((8 + 4 + table_len) + alignment - 1) // alignment * alignment

    offsets = []
    pos = data_start
    for chunk in stored:
        offsets.append(pos)
        pos = (pos + len(chunk) + alignment - 1) // alignment * alignment

    out = bytearray()
    out += b"FArC" if compressed else b"FArc"
    out += struct.pack(">II", table_len + 4, alignment)
    for (name, data), off, chunk in zip(members, offsets, stored):
        out += name.encode("utf-8") + b"\x00" + struct.pack(">III", off, len(chunk), len(data))
    out += b"\x00" * (data_start - len(out))
    for off, chunk in zip(offsets, stored):
        assert len(out) == off, f"member wants {off}, stream at {len(out)}"
        out += chunk
        out += b"\x00" * ((-len(out)) % alignment)

    with open(path, "wb") as fh:
        fh.write(out)
    return bytes(out)


def pack_dir(src_dir, dst, order=None, compressed=True, alignment=16):
    names = sorted(os.listdir(src_dir)) if order is None else order
    members = [(n, open(os.path.join(src_dir, n), "rb").read()) for n in names]
    return write(dst, members, compressed, alignment)
