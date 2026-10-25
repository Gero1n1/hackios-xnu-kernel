"""Repack the OrangeFox recovery boot image with a freshly built kernel.

Keeps the original ramdisk, kernel load addresses, cmdline and page size;
the device tree travels with the kernel (Image.gz-dtb), so the original
kernel's DTB bytes are carried over to the new one, wherever they live
(inside or after the gzip stream).

usage: repack-recovery.py <original-recovery.img> <new-Image.gz> <out.img>
"""
import gzip
import hashlib
import struct
import sys
import zlib

ORIG, NEWKERNEL, OUT = sys.argv[1], sys.argv[2], sys.argv[3]

img = open(ORIG, "rb").read()
assert img[:8] == b"ANDROID!", img[:8]
(kernel_size, kernel_addr, ramdisk_size, ramdisk_addr, second_size,
 second_addr, tags_addr, page_size, dt_size, unused) = struct.unpack_from("<10I", img, 8)
cmdline = img[64:576].rstrip(b"\0")

pages = lambda n: (n + page_size - 1) // page_size
off = page_size
old_kernel = img[off:off + kernel_size]; off += pages(kernel_size) * page_size
ramdisk = img[off:off + ramdisk_size]; off += pages(ramdisk_size) * page_size

# Find the original DTB: inside the gzip stream or appended after it.
d = zlib.decompressobj(16 + zlib.MAX_WBITS)
old_image = d.decompress(old_kernel)
dtb_magic = b"\xd0\x0d\xfe\xed"
if d.unused_data:
    dtb = d.unused_data
    print(f"dtb after the gzip stream: {len(dtb)} bytes")
elif dtb_magic in old_image:
    i = old_image.index(dtb_magic)
    dtb = old_image[i:]
    print(f"dtb inside the gzip stream at image offset {i}: {len(dtb)} bytes")
else:
    sys.exit("no dtb found in the original kernel")

new_kern = open(NEWKERNEL, "rb").read()
if new_kern[:2] == b"\x1f\x8b" and dtb_magic not in new_kern:
    # New kernel is gzipped: keep the dtb wherever the original kept it.
    if d.unused_data:
        new_kernel = new_kern + dtb
    else:
        new_image = gzip.decompress(new_kern)
        new_kernel = gzip.compress(new_image + dtb, 6)
else:
    new_kernel = new_kern + dtb

out = bytearray()
out += b"ANDROID!"
out += struct.pack("<10I", len(new_kernel), kernel_addr, len(ramdisk),
                   ramdisk_addr, 0, second_addr, tags_addr, page_size, dt_size, 0)
out += b"\0" * 16            # name
out += cmdline + b"\0" * (512 - len(cmdline))
out += b"\0" * 32            # id
out += b"\0" * 1024          # extra cmdline
out += b"\0" * (-len(out) % page_size)
out += new_kernel + b"\0" * (pages(len(new_kernel)) * page_size - len(new_kernel))
out += ramdisk + b"\0" * (pages(len(ramdisk)) * page_size - len(ramdisk))
if dt_size:
    out += img[off + pages(second_size) * page_size:][:dt_size]

open(OUT, "wb").write(out)
print(f"{OUT}: {len(out)} bytes, kernel {len(new_kernel)}, ramdisk {len(ramdisk)}")
print("sha256:", hashlib.sha256(out).hexdigest())
