#!/usr/bin/env python3
"""FAT32 模块测试：格式化/目录/长名/大文件/目录簇扩展/读回校验"""
import os, sys, hashlib, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fat32 import Fat32

IMG = "D:/Projects/COM++/m8-build/build/fat32_test.img"
os.makedirs(os.path.dirname(IMG), exist_ok=True)
SIZE = 128 * 1024 * 1024
OFFSET = 1 * 1024 * 1024  # 模拟分区在镜像中的偏移

# 预创建镜像文件
with open(IMG, "wb") as f:
    f.truncate(OFFSET + SIZE)

fs = Fat32(IMG, OFFSET, SIZE, label="BOOT")
fs.format()
print("format ok: spc=%d clusters=%d fat_size=%d" % (fs.spc, fs.clusters, fs.fat_size))

# ---- 目录 ----
fs.mkdir("boot")
fs.mkdir("boot/grub")
fs.mkdir("games")
fs.mkdir("a-very-long-directory-name-here")
fs.mkdir("a-very-long-directory-name-here/sub")
print("mkdir ok")

# ---- 文件 ----
fs.write_file("boot/uEnv.txt", data=b"bootargs=console=ttyS0,115200 root=/dev/mmcblk0p2 rootwait\n")
fs.write_file("boot/zImage", data=b"\x1f\x8b\x08" + bytes(1000))  # 假内核
fs.write_file("boot/board.dtb", data=b"\xd0\x0d\xfe\xed" + bytes(5000))
fs.write_file("boot/grub/grub.cfg", data=b"menuentry 'M8' { linux /boot/zImage }\n" * 10)
fs.write_file("games/readme.txt", data=b"put your roms here\n")
fs.write_file("empty.bin", data=b"")
fs.write_file("MixedCase.TXT", data=b"case test\n")
print("write ok")

# 长名文件（LFN）
long_data = b"LFN test payload " * 100
fs.write_file("a-very-long-directory-name-here/a-really-long-file-name-with.dots", data=long_data)

# 大文件：跨多个簇（>4MB）
big = bytes(random.Random(42).randbytes(4 * 1024 * 1024 + 12345))
fs.write_file("games/big.rom", data=big)

# 一个目录写 40 个小文件，测试目录簇链扩展
for i in range(40):
    fs.write_file("games/many/f%03d.rom" % i, data=b"rom-%03d" % i) if False else None
fs.mkdir("games/many")
for i in range(40):
    fs.write_file("games/many/f%03d.rom" % i, data=b"rom-%03d-data" % i)
print("many files ok")

# ---- 读回校验 ----
def check(path, expect):
    got = fs.read_file(path)
    assert got is not None, "read failed: " + path
    assert got == expect, "content mismatch: %s (%d vs %d bytes)" % (path, len(got), len(expect))
    return len(got)

n1 = check("boot/uEnv.txt", b"bootargs=console=ttyS0,115200 root=/dev/mmcblk0p2 rootwait\n")
n2 = check("boot/zImage", b"\x1f\x8b\x08" + bytes(1000))
n3 = check("boot/grub/grub.cfg", b"menuentry 'M8' { linux /boot/zImage }\n" * 10)
n4 = check("games/readme.txt", b"put your roms here\n")
n5 = check("empty.bin", b"")
n6 = check("MixedCase.TXT", b"case test\n")
n7 = check("a-very-long-directory-name-here/a-really-long-file-name-with.dots", long_data)
n8 = check("games/big.rom", big)
for i in range(40):
    check("games/many/f%03d.rom" % i, b"rom-%03d-data" % i)
print("readback ok: %d files verified, big=%d bytes" % (8 + 40, n8))

# ---- 列目录 ----
for kind, p, size in fs.list_all():
    print("  %s %-70s %8d" % (kind, p, size))

fs.close()
print("ALL FAT32 TESTS PASSED")

# ---- 磁盘重挂载校验（防止只验内存：重新解析 BPB+FAT 再读回） ----
fs.close()
fs2 = Fat32(IMG, 1024 * 1024, 128 * 1024 * 1024).mount()
def check2(path, expect):
    got = fs2.read_file(path)
    assert got is not None, "remount read failed: " + path
    assert got == expect, "remount content mismatch: %s" % path
check2("boot/uEnv.txt", b"bootargs=console=ttyS0,115200 root=/dev/mmcblk0p2 rootwait\n")
check2("boot/zImage", b"\x1f\x8b\x08" + bytes(1000))
check2("boot/board.dtb", b"\xd0\x0d\xfe\xed" + bytes(5000))
check2("boot/grub/grub.cfg", b"menuentry 'M8' { linux /boot/zImage }\n" * 10)
check2("games/readme.txt", b"put your roms here\n")
check2("empty.bin", b"")
check2("MixedCase.TXT", b"case test\n")
check2("a-very-long-directory-name-here/a-really-long-file-name-with.dots", long_data)
check2("games/big.rom", big)
for i in range(40):
    check2("games/many/f%03d.rom" % i, b"rom-%03d-data" % i)
print("REMOUNT VERIFICATION PASSED (BPB/FAT parsed from disk, %d files)" % (8 + 40))
