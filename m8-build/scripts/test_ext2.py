#!/usr/bin/env python3
"""ext2 模块测试：格式化/多级目录/大文件(双间接)/符号链接/读回校验/结构一致性"""
import os, sys, struct, hashlib, random, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ext2 import Ext2, EXT2_ROOT_INO, LOST_FOUND_INO

IMG = "D:/Projects/COM++/m8-build/build/ext2_test.img"
os.makedirs(os.path.dirname(IMG), exist_ok=True)
SIZE = 16 * 1024 * 1024
BS = 1024

with open(IMG, "wb") as f:
    f.truncate(SIZE)

fs = Ext2(IMG, 0, SIZE, block_size=BS, label="ROOT")
fs.format()

# ---------- 写入 ----------
boot = fs.add_dir(EXT2_ROOT_INO, "boot")
games = fs.add_dir(EXT2_ROOT_INO, "games")
lib = fs.add_dir(EXT2_ROOT_INO, "lib")
fs.add_dir(lib, "sub")
fs.add_symlink(EXT2_ROOT_INO, "link-to-boot", "boot")
fs.add_symlink(EXT2_ROOT_INO, "long-link", "a-very-long-symlink-target-path-that-exceeds-sixty-bytes-xxxxxxxxxxxxxxxxxxxxx")
fs.add_data(EXT2_ROOT_INO, "hello.txt", b"hello ext2 world\n", 0o100644)
# 小文件（直接块）
fs.add_data(games, "small.rom", b"A" * 100)
# 中等文件（跨一级间接：>12KB）
mid = bytes(random.Random(1).randbytes(40 * 1024))
fs.add_data(games, "mid.rom", mid)
# 大文件（跨二级间接：>268KB）
big = bytes(random.Random(2).randbytes(500 * 1024 + 777))
fs.add_data(games, "big.rom", big)
# 多文件挤一个目录（触发目录多块）
for i in range(200):
    fs.add_data(games, "file%03d.dat" % i, b"data-%03d" % i)
# 可执行权限文件
fs.add_data(boot, "run.sh", b"#!/bin/sh\necho hi\n", 0o100755)
fs.flush()
fs.close()
print("write ok")

# ---------- 读取验证（独立实现，不复用写入器逻辑） ----------
class Reader:
    def __init__(self, path):
        self.f = open(path, "rb")
        sb = self._rd(1024, 1024)
        (self.inodes_count, self.blocks_count, self.r_blocks, self.free_blocks,
         self.free_inodes, self.first_data_block) = struct.unpack_from("<IIIIII", sb, 0)
        self.log_bs = struct.unpack_from("<i", sb, 24)[0]
        self.bs = 1024 << self.log_bs
        self.bpg, self.ipg = struct.unpack_from("<II", sb, 32)
        self.magic = struct.unpack_from("<H", sb, 56)[0]
        self.first_ino = struct.unpack_from("<I", sb, 84)[0]
        self.inode_size = struct.unpack_from("<H", sb, 88)[0]
        self.feat_incompat, self.feat_ro = struct.unpack_from("<II", sb, 96)
        self.groups = (self.blocks_count - 1 + self.bpg - 1) // self.bpg
        self.label = sb[120:136].rstrip(b"\x00").decode()
        self.gdt = []
        gdt_blk = 2 if self.bs == 1024 else 1   # 1K块: 块0=引导, 块1=超级块, 块2=GDT
        gdt_raw = self._rd(gdt_blk * self.bs, self.groups * 32)
        for g in range(self.groups):
            bb, ib, it = struct.unpack_from("<III", gdt_raw, g * 32)
            self.gdt.append((bb, ib, it))

    def _rd(self, pos, n):
        self.f.seek(pos)
        return self.f.read(n)

    def inode(self, ino):
        g = (ino - 1) // self.ipg
        idx = (ino - 1) % self.ipg
        _, _, itab = self.gdt[g]
        raw = self._rd(itab * self.bs + idx * self.inode_size, 128)
        mode, uid, size = struct.unpack_from("<HHI", raw, 0)
        links = struct.unpack_from("<H", raw, 26)[0]
        blocks = struct.unpack_from("<15I", raw, 40)
        return {"mode": mode, "size": size, "links": links, "blocks": blocks,
                "raw": raw}

    def block(self, b):
        return self._rd(b * self.bs, self.bs)

    def read_file(self, ino):
        st = self.inode(ino)
        data = b""
        # 直接块
        for b in st["blocks"][:12]:
            if b == 0:
                continue
            data += self.block(b)
        if len(data) < st["size"]:
            # 一级间接
            per = self.bs // 4
            ind = st["blocks"][12]
            if ind:
                ptrs = struct.unpack("<%dI" % per, self.block(ind))
                for b in ptrs:
                    if b:
                        data += self.block(b)
            if len(data) < st["size"]:
                # 二级间接
                dbl = st["blocks"][13]
                if dbl:
                    subs = struct.unpack("<%dI" % per, self.block(dbl))
                    for s in subs:
                        if s:
                            ptrs = struct.unpack("<%dI" % per, self.block(s))
                            for b in ptrs:
                                if b:
                                    data += self.block(b)
        return data[:st["size"]]

    def readdir(self, ino):
        st = self.inode(ino)
        out = []
        # 目录块链：直接+间接（测试目录都不大，直接扫全部块）
        blocks = list(st["blocks"][:12])
        per = self.bs // 4
        for ind in (st["blocks"][12],):
            if ind:
                blocks += [b for b in struct.unpack("<%dI" % per, self.block(ind)) if b]
        for b in blocks:
            if not b:
                continue
            blk = self.block(b)
            off = 0
            while off < self.bs:
                ino_e, rec, nlen, ftype = struct.unpack_from("<IHBB", blk, off)
                if rec < 12:
                    break
                name = blk[off + 8:off + 8 + nlen].decode("utf-8", "replace")
                if ino_e and name not in (".", ".."):
                    out.append((name, ino_e, ftype))
                off += rec
        return out

    def readlink(self, ino):
        st = self.inode(ino)
        if st["size"] <= 60:
            return st["raw"][40:40 + st["size"]].decode()
        return self.block(st["blocks"][0])[:st["size"]].decode()

r = Reader(IMG)
print("sb: magic=%04x bs=%d groups=%d inodes=%d label=%r incompat=%x ro=%x"
      % (r.magic, r.bs, r.groups, r.inodes_count, r.label, r.feat_incompat, r.feat_ro))
assert r.magic == 0xEF53
assert r.label == "ROOT"
assert r.feat_ro & 1, "sparse_super not set"

def find(path):
    ino = EXT2_ROOT_INO
    if path == "/":
        return ino
    for part in path.strip("/").split("/"):
        for name, i, ft in r.readdir(ino):
            if name == part:
                ino = i
                break
        else:
            raise AssertionError("not found: " + path + " (at " + part + ")")
    return ino

def check_file(path, expect, mode=None):
    ino = find(path)
    st = r.inode(ino)
    got = r.read_file(ino)
    assert got == expect, "%s: content mismatch (%d vs %d)" % (path, len(got), len(expect))
    if mode is not None:
        assert (st["mode"] & 0o7777) == mode, "%s: mode %o != %o" % (path, st["mode"] & 0o7777, mode)
    print("  OK %-40s %8d bytes mode=%o" % (path, len(got), st["mode"] & 0o7777))

check_file("/hello.txt", b"hello ext2 world\n")
check_file("/games/small.rom", b"A" * 100)
check_file("/games/mid.rom", mid)
check_file("/games/big.rom", big)
for i in range(200):
    ino = find("/games/file%03d.dat" % i)
    assert r.read_file(ino) == b"data-%03d" % i, "file%03d mismatch" % i
print("  OK /games/file000..199.dat (200 files)")
check_file("/boot/run.sh", b"#!/bin/sh\necho hi\n", 0o755)

# 符号链接
ino = find("/link-to-boot")
assert r.inode(ino)["mode"] & 0xF000 == 0xA000, "not a symlink"
assert r.readlink(ino) == "boot", r.readlink(ino)
print("  OK /link-to-boot -> boot")
ino = find("/long-link")
assert r.readlink(ino) == "a-very-long-symlink-target-path-that-exceeds-sixty-bytes-xxxxxxxxxxxxxxxxxxxxx"
print("  OK /long-link (slow symlink)")

# 目录结构与链接数
assert r.inode(find("/games"))["links"] == 2, "games links=%d (expect 2: self + parent entry)" % r.inode(find("/games"))["links"]
assert r.inode(find("/lib"))["links"] == 3, "lib links=%d (expect 3: self + parent + subdir)" % r.inode(find("/lib"))["links"]
root = r.inode(EXT2_ROOT_INO)
print("  OK dir links: games=%d lib=%d root=%d" % (r.inode(find("/games"))["links"], r.inode(find("/lib"))["links"], root["links"]))
assert root["links"] == 5

# lost+found 存在且非空
ino = find("/lost+found")
assert r.inode(ino)["size"] == 16 * 1024
print("  OK /lost+found (16KB)")

# 位图与超级块一致性
used_blocks = 0
for g in range(r.groups):
    bb, ib, it = r.gdt[g]
    bm = r.block(bb)
    first = 1 + g * r.bpg   # 1K块时组0从块1开始
    for j in range(min(r.bpg, r.blocks_count - first)):
        b = first + j
        if (bm[j // 8] >> (j % 8)) & 1:
            used_blocks += 1
assert used_blocks + r.free_blocks == r.blocks_count, "block count mismatch: %d + %d != %d" % (used_blocks, r.free_blocks, r.blocks_count)
used_inodes = 0
for g in range(r.groups):
    bb, ib, it = r.gdt[g]
    bm = r.block(ib)
    for j in range(r.ipg):
        if (bm[j // 8] >> (j % 8)) & 1:
            used_inodes += 1
assert used_inodes + r.free_inodes == r.inodes_count, "inode count mismatch"
print("  OK bitmap consistency: %d blocks used, %d inodes used" % (used_blocks, used_inodes))

print("ALL EXT2 TESTS PASSED")
