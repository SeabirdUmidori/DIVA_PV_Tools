# Based on: https://github.com/korenkonder/PD_Tool/blob/master/KKdMainLib/Mot.cs

import struct

def align4(f):
    pos = f.tell()
    padding = (4 - (pos % 4)) % 4
    if padding:
        f.write(b'\x00' * padding)

def write_header(f, mot_headers):
    f.seek(0)
    for mot_header in mot_headers:
        f.write(struct.pack('<I', mot_header['KeySetOffset']))
        f.write(struct.pack('<I', mot_header['KeySetTypesOffset']))
        f.write(struct.pack('<I', mot_header['KeySetDataOffset']))
        f.write(struct.pack('<I', mot_header['BoneInfoOffset']))
    f.write(struct.pack('<I', 0))
    f.write(struct.pack('<I', 0))
    f.write(struct.pack('<I', 0))
    f.write(struct.pack('<I', 0))

def write_keyset_types(f, keyset_types):
    i = 0
    while i < len(keyset_types):
        word_value = 0
        for bit_index in range(8):
            if i + bit_index < len(keyset_types):
                keyset_type = keyset_types[i + bit_index]
                word_value |= (keyset_type & 0b11) << (bit_index * 2)
        f.write(struct.pack('<H', word_value))
        i += 8
    align4(f)

def write_keyset_data(f, export_data):
    for index, keyset in enumerate(export_data):
        if keyset is None:
            #print(f"Entry {index}: None keyset")
            continue  
        
        keyset_type = keyset.type
        keyset_values = keyset.values

        if keyset_type == 1:
            
            #print(f"Entry {index}: Writing a static keyset.")
            f.write(struct.pack('<f', keyset_values[0]))

        elif keyset_type == 2:
            
            key_count = len(keyset_values)
            # one pack per column rather than one per key: ~2.6x on a full-length keyset,
            # and it is the same bytes
            f.write(struct.pack('<H', key_count))
            f.write(struct.pack('<%dH' % key_count, *[int(frame) for frame, _value in keyset_values]))
            align4(f)  
            f.write(struct.pack('<%df' % key_count, *[float(value) for _frame, value in keyset_values]))

        elif keyset_type == 3:
            key_count = len(keyset_values)
            f.write(struct.pack('<H', key_count))

            f.write(struct.pack('<%dH' % key_count, *[int(frame) for frame, _v, _t in keyset_values]))
            align4(f)

            flat = []
            for _frame, value, tangent in keyset_values:
                flat.append(float(value))
                flat.append(float(tangent))
            f.write(struct.pack('<%df' % len(flat), *flat))
            #align4(f)

        else:
            pass

    align4(f)

def write_bone_info(f, bone_info):
    for bone_index in bone_info:
        f.write(struct.pack('<H', bone_index))
    f.write(struct.pack('<H', 0))
    align4(f)

def write_mot_bin(output_path, export_data, bone_info, frame_count):
    with open(output_path, 'wb') as f:
        mot_count = 1
        header_size = (mot_count + 1) * 16
        f.seek(header_size)

        mot_headers = []
        mot_header = {}
        mot_header['KeySetOffset'] = f.tell()
        high_bits = 1
        keyset_count = len(export_data)
        f.write(struct.pack('<H', (high_bits << 14) | (keyset_count & 0x3FFF)))
        f.write(struct.pack('<H', frame_count))

        mot_header['KeySetTypesOffset'] = f.tell()
        keyset_types = []
        for keyset in export_data:
            if keyset is None:
                keyset_types.append(0)
            else:
                keyset_type = keyset.type
                keyset_types.append(keyset_type)
        write_keyset_types(f, keyset_types)

        mot_header['KeySetDataOffset'] = f.tell()
        write_keyset_data(f, export_data)

        mot_header['BoneInfoOffset'] = f.tell()
        write_bone_info(f, bone_info)

        mot_headers.append(mot_header)
        write_header(f, mot_headers)


def write_mot_bin_units(output_path, export_data, bone_info, frame_count, group=8):
    """`write_mot_bin` as a generator of units, so the write can be interrupted and reported.

    Even a full-length set (168 linear keysets, ~2 million keys, 12 MB) writes in well under a
    second, but one call is still a gap in the export.  The cost per keyset is a couple of
    milliseconds, so the unit is one *group* of keysets and the whole write becomes ~21 units of
    that size - small enough not to be felt, and few enough that the per-unit overhead does not matter.

    Two changes make a keyset cheap, and both are in the direction of doing less per key rather than
    running less often:

      * the frame column is written with **one** `struct.pack` instead of one per key, and the value
        column with another, for the same bytes at a fraction of the cost.
      * the units are yielded *between* keysets, so the file is open across several scheduler ticks.
        That is safe here and only here: the target is a `.part` file that nothing reads, the header is
        written last, and a cancel removes it.

    The caller must exhaust this generator; abandoning it leaves an open file.  `Operation.abort`
    handles the rest by removing the temp path.

    Byte-identical to `write_mot_bin`, the original writer.
    """
    with open(output_path, 'wb') as f:
        mot_count = 1
        header_size = (mot_count + 1) * 16
        f.seek(header_size)

        mot_headers = []
        mot_header = {}
        mot_header['KeySetOffset'] = f.tell()
        high_bits = 1
        keyset_count = len(export_data)
        f.write(struct.pack('<H', (high_bits << 14) | (keyset_count & 0x3FFF)))
        f.write(struct.pack('<H', frame_count))

        mot_header['KeySetTypesOffset'] = f.tell()
        write_keyset_types(f, [0 if k is None else k.type for k in export_data])

        mot_header['KeySetDataOffset'] = f.tell()
        for start in range(0, len(export_data), max(1, group)):
            batch = export_data[start:start + max(1, group)]
            def _emit(_batch=batch, _f=f):
                write_keyset_data(_f, _batch)
            yield _emit
        align4(f)

        mot_header['BoneInfoOffset'] = f.tell()
        write_bone_info(f, bone_info)

        mot_headers.append(mot_header)
        write_header(f, mot_headers)
