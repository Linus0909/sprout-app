#!/usr/bin/env python3
"""Generates the Sprout app icons (pure stdlib, no Pillow needed).
Rasterizes the same leaf mark used in the app's logo as a filled icon.
Run once: python3 make_icons.py
"""
import os
import struct
import zlib

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'icons')
BRAND = (47, 111, 94)      # #2F6F5E
LEAF = (255, 255, 255)     # white


def bezier_y(t, p0, p1, p2, p3):
    mt = 1 - t
    return mt**3 * p0 + 3 * mt**2 * t * p1 + 3 * mt * t**2 * p2 + t**3 * p3


def build_right_boundary():
    # Right half of the leaf outline: (20,4) -> (20,56), control pts (34,16),(34,40)
    samples = []
    n = 2000
    for i in range(n + 1):
        t = i / n
        x = bezier_y(t, 20, 34, 34, 20)
        y = bezier_y(t, 4, 16, 40, 56)
        samples.append((y, x))
    samples.sort()
    return samples


def xr_at(y, samples):
    # linear interpolation lookup, samples sorted by y
    lo, hi = 0, len(samples) - 1
    if y <= samples[0][0]:
        return samples[0][1]
    if y >= samples[-1][0]:
        return samples[-1][1]
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if samples[mid][0] < y:
            lo = mid
        else:
            hi = mid
    y0, x0 = samples[lo]
    y1, x1 = samples[hi]
    if y1 == y0:
        return x0
    f = (y - y0) / (y1 - y0)
    return x0 + (x1 - x0) * f


def write_png(path, size, draw_pixel):
    def chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            r, g, b, a = draw_pixel(x, y)
            raw += bytes((r, g, b, a))
    idat = zlib.compress(bytes(raw), 9)
    with open(path, 'wb') as f:
        f.write(sig)
        f.write(chunk(b'IHDR', ihdr))
        f.write(chunk(b'IDAT', idat))
        f.write(chunk(b'IEND', b''))


def make_icon(path, size, rounded=False):
    samples = build_right_boundary()
    pad = size * 0.18
    scale = (size - 2 * pad) / 60.0  # leaf viewBox height is 60
    off_x = (size - 40 * scale) / 2.0
    off_y = pad
    vein_half_width = max(1.0, size * 0.012)

    def draw_pixel(px, py):
        cx, cy = px + 0.5, py + 0.5
        if rounded:
            r = size / 2.0
            if (cx - r) ** 2 + (cy - r) ** 2 > r * r:
                return (0, 0, 0, 0)
        lx = (cx - off_x) / scale
        ly = (cy - off_y) / scale
        if 4 <= ly <= 56:
            xr = xr_at(ly, samples)
            xl = 40 - xr
            if xl <= lx <= xr:
                if abs(lx - 20) <= vein_half_width / scale and 8 <= ly <= 52:
                    return (*BRAND, 255)
                return (*LEAF, 255)
        return (*BRAND, 255)

    write_png(path, size, draw_pixel)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    make_icon(os.path.join(OUT_DIR, 'icon-180.png'), 180)
    make_icon(os.path.join(OUT_DIR, 'icon-192.png'), 192)
    make_icon(os.path.join(OUT_DIR, 'icon-512.png'), 512)
    make_icon(os.path.join(OUT_DIR, 'favicon-32.png'), 32)
    # App Store icon: 1024x1024, no transparency (Apple rejects icons with
    # alpha), so this one is drawn opaque.
    make_icon(os.path.join(OUT_DIR, 'icon-1024-appstore.png'), 1024)
    print('Icons written to', OUT_DIR)


if __name__ == '__main__':
    main()
