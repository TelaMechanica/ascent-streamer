"""Summarize Annex-B headers without printing camera image data."""
from collections import Counter
import json
from pathlib import Path
import re
import sys


class Bits:
    def __init__(self, data):
        self.data = ''.join(f'{b:08b}' for b in data)
        self.pos = 0

    def bits(self, n):
        if self.pos + n > len(self.data):
            raise ValueError('Truncated header')
        value = int(self.data[self.pos:self.pos + n], 2) if n else 0
        self.pos += n
        return value

    def ue(self):
        zeros = 0
        while not self.bits(1):
            zeros += 1
            if zeros > 31:
                raise ValueError('Invalid Exp-Golomb code')
        return (1 << zeros) - 1 + self.bits(zeros)

    def se(self):
        value = self.ue()
        return (value + 1) // 2 if value & 1 else -value // 2


def sps(nal):
    rbsp = re.sub(b'\x00\x00\x03', b'\x00\x00', nal[1:])
    bits = Bits(rbsp)
    out = {}
    try:
        out['profile'] = bits.bits(8)
        out['constraints'] = bits.bits(8)
        out['level'] = bits.bits(8)
        out['id'] = bits.ue()
        out['separate_colour_plane'] = 0
        if out['profile'] in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            out['chroma_format'] = chroma = bits.ue()
            if chroma == 3:
                out['separate_colour_plane'] = bits.bits(1)
            out['luma_depth_minus8'] = bits.ue()
            out['chroma_depth_minus8'] = bits.ue()
            bits.bits(1)
            if bits.bits(1):
                for i in range(8 if chroma != 3 else 12):
                    if bits.bits(1):
                        last = following = 8
                        for j in range(16 if i < 6 else 64):
                            if following:
                                following = (last + bits.se() + 256) % 256
                            last = following or last
        out['log2_frame_num_minus4'] = bits.ue()
        out['poc_type'] = poc = bits.ue()
        if poc == 0:
            out['log2_poc_minus4'] = bits.ue()
        elif poc == 1:
            bits.bits(1)
            bits.se()
            bits.se()
            for i in range(bits.ue()):
                bits.se()
        out['refs'] = bits.ue()
        out['gaps_in_frame_num_allowed'] = bits.bits(1)
        out['width_mbs_minus1'] = bits.ue()
        out['height_map_units_minus1'] = bits.ue()
        out['frame_mbs_only'] = bits.bits(1)
    except ValueError as error:
        out['error'] = str(error)
    return out


def continuity(nals):
    """Report reference-frame-number steps; candidates require codec context.

    Field pictures and separate colour planes are excluded. Non-reference pictures
    do not advance the reference counter. MMCO5 and redundant-picture semantics
    are not decoded, so discontinuities alone are not a universal corruption test.
    """
    sequences, pictures, previous = {}, {}, {}
    counts, steps, events = Counter(), Counter(), []
    for index, nal in enumerate(nals):
        kind = nal[0] & 31
        if kind == 7:
            header = sps(nal)
            if 'error' not in header:
                if header['id'] in sequences and sequences[header['id']] != header:
                    previous.pop(header['id'], None)
                    counts['sequence_parameter_changes'] += 1
                sequences[header['id']] = header
            continue
        if kind not in (1, 5, 8):
            continue
        # Headers fit here for the supported diagnostic subset; never turn a
        # multi-megabyte slice payload into a binary string.
        bits = Bits(re.sub(b'\x00\x00\x03', b'\x00\x00', nal[1:129]))
        try:
            if kind == 8:
                pps_id, sps_id = bits.ue(), bits.ue()
                pictures[pps_id] = sps_id
                continue
            first_mb, slice_type, pps_id = bits.ue(), bits.ue() % 5, bits.ue()
            counts['picture_slices'] += 1
            if first_mb:
                counts['additional_slices'] += 1
                continue
            counts['pictures'] += 1
            counts['slice_type_'+str(slice_type)] += 1
            header = sequences.get(pictures.get(pps_id))
            if header is None:
                counts['missing_parameter_sets'] += 1
                continue
            if not header['frame_mbs_only'] or header['separate_colour_plane']:
                counts['unsupported_picture_layout'] += 1
                continue
            width = header['log2_frame_num_minus4'] + 4
            if not 4 <= width <= 16:
                raise ValueError('Invalid frame_num width')
            number = bits.bits(width)
            if not nal[0] & 96:
                counts['non_reference_pictures'] += 1
                continue
            key = pictures[pps_id]
            counts['reference_pictures'] += 1
            if kind == 5:
                counts['idr_pictures'] += 1
                previous[key] = number
                continue
            if key in previous:
                step = (number-previous[key]) % (1 << width)
                steps[step] += 1
                if step != 1 and len(events) < 12:
                    events.append(dict(nal_index=index, frame_num=number, previous=previous[key],
                                       step=step, slice_type=slice_type,
                                       gaps_allowed=header['gaps_in_frame_num_allowed']))
            previous[key] = number
        except (ValueError, KeyError):
            counts['header_errors'] += 1
    return dict(counts=dict(counts), reference_steps=dict(steps), first_discontinuities=events,
                interpretation='Reference counter candidates; field pictures excluded; MMCO5 and redundant pictures not decoded')


def main(path):
    data = Path(path).read_bytes()
    nals = [n for n in re.split(b'\x00\x00\x00?\x01', data) if n]
    headers = list(dict.fromkeys(n for n in nals if (n[0] & 31) in (7, 8)))
    print(json.dumps(dict(bytes=len(data), nal_types=dict(Counter(n[0] & 31 for n in nals)),
                          continuity=continuity(nals),
                          headers=[dict(type=n[0] & 31, hex=n.hex(),
                                        fields=sps(n) if (n[0] & 31) == 7 else None)
                                   for n in headers]), indent=2))


if __name__ == '__main__':
    main(sys.argv[1])
