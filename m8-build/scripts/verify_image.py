#!/usr/bin/env python3
"""M8 Lite 镜像独立验证：不信任写入器的读回校验。

1. MBR 分区表 + SPL 魔数
2. FAT32 BOOT：读回启动文件并比对 SHA256
3. ext2 ROOT：独立读取器遍历目录树，核对关键文件（内容哈希/符号链接/权限）
4. FAT32 GAMES：列目录
"""
import os, sys, struct, hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fat32 import Fat32

SECTOR = 512
FAIL = []


def ok(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def sha(b):
    return hashlib.sha256(b).hexdigest()[:16]


# ---------------- ext2 独立读取器（仿 test_ext2.py，不依赖写入器） ----------------
class Ext2Reader:
    def __init__(self, path, offset):
        self.f = open(path, "rb")
        self.off = offset
        sb = self._rd(1024, 1024)
        (self.inodes_count, self.blocks_count, self.r_blocks, self.free_blocks,
         self.free_inodes, self.first_data_block) = struct.unpack_from("<IIIIII", sb, 0)
        self.log_bs = struct.unpack_from("<i", sb, 24)[0]
        self.bs = 1024 << self.log_bs
        self.bpg = struct.unpack_from("<I", sb, 32)[0]
        self.ipg = struct.unpack_from("<I", sb, 40)[0]
        self.magic = struct.unpack_from("<H", sb, 56)[0]
        self.inode_size = struct.unpack_from("<H", sb, 88)[0]
        self.groups = (self.blocks_count - 1 + self.bpg - 1) // self.bpg
        self.gdt = []
        gdt_blk = 2 if self.bs == 1024 else 1
        gdt_raw = self._rd(gdt_blk * self.bs, self.groups * 32)
        for g in range(self.groups):
            bb, ib, it = struct.unpack_from("<III", gdt_raw, g * 32)
            self.gdt.append((bb, ib, it))

    def _rd(self, pos, n):
        self.f.seek(self.off + pos)
        return self.f.read(n)

    def inode(self, ino):
        g = (ino - 1) // self.ipg
        idx = (ino - 1) % self.ipg
        _, _, itab = self.gdt[g]
        raw = self._rd(itab * self.bs + idx * self.inode_size, 128)
        mode, uid, size = struct.unpack_from("<HHI", raw, 0)
        links = struct.unpack_from("<H", raw, 26)[0]
        blocks = struct.unpack_from("<15I", raw, 40)
        return {"mode": mode, "size": size, "links": links, "blocks": blocks, "raw": raw}

    def block(self, b):
        return self._rd(b * self.bs, self.bs)

    def read_file(self, ino):
        st = self.inode(ino)
        if (st["mode"] & 0xF000) == 0xA000:  # symlink
            if st["size"] <= 60:  # 快速符号链接：目标存放于 i_block 区
                return st["raw"][40:40 + st["size"]]
            return self.block(st["blocks"][0])[:st["size"]]
        data = b""
        per = self.bs // 4
        for b in st["blocks"][:12]:
            if b:
                data += self.block(b)
        if len(data) < st["size"]:
            ind = st["blocks"][12]
            if ind:
                for b in struct.unpack("<%dI" % per, self.block(ind)):
                    if b:
                        data += self.block(b)
        if len(data) < st["size"]:
            dbl = st["blocks"][13]
            if dbl:
                for s in struct.unpack("<%dI" % per, self.block(dbl)):
                    if s:
                        for b in struct.unpack("<%dI" % per, self.block(s)):
                            if b:
                                data += self.block(b)
        return data[:st["size"]]

    def listdir(self, ino):
        st = self.inode(ino)
        out = []
        data = self.read_file(ino)
        off = 0
        while off + 8 <= len(data):
            ino2, rec, nlen, ftype = struct.unpack_from("<IHBB", data, off)
            if rec < 12 or off + rec > len(data):
                break
            name = data[off + 8: off + 8 + nlen]
            out.append((ino2, name.decode("utf-8", "replace"), ftype, rec))
            off += rec
        return out

    def walk(self, ino=2, prefix=""):
        """yield (path, ino, ftype)"""
        for ino2, name, ftype, _ in self.listdir(ino):
            if name in (".", ".."):
                continue
            p = prefix + "/" + name
            yield p, ino2, ftype
            if ftype == 2:
                for item in self.walk(ino2, p):
                    yield item


def main():
    img = sys.argv[1] if len(sys.argv) > 1 else "D:/Projects/COM++/m8-build/build/m8-test-2g.img"
    rootfs_src = sys.argv[2] if len(sys.argv) > 2 else os.environ.get(
        "M8_ROOTFS_DIR", "D:/Projects/COM++/m8-build/build/rootfs")
    boot_src = "D:/Projects/COM++/m8-build/build/boot"
    print("验证镜像: %s" % img)

    with open(img, "rb") as f:
        head = f.read(1024 * 1024)

    # ---- 1. MBR ----
    print("\n[1] MBR / SPL")
    ok(head[510:512] == b"\x55\xaa", "MBR 签名 0x55AA")
    parts = []
    for i in range(4):
        e = head[446 + i * 16: 446 + (i + 1) * 16]
        if e[4] == 0:
            continue
        start, count = struct.unpack_from("<II", e, 8)
        parts.append((i + 1, e[4], start, count))
    ok(len(parts) == 3, "3 个分区 (实际 %d)" % len(parts))
    for n, t, s, c in parts:
        print("     p%d type=0x%02x start=%d size=%dMB" % (n, t, s, c * 512 // 1048576))
    ok(head[8196:8204] == b"eGON.BT0", "SPL eGON.BT0 @ 8KB+4")
    ok(head[8208:8224].find(b"sun7i") >= 0 or b"sun7i" in head[8192:16384], "SPL 板名含 sun7i")

    # ---- 2. FAT32 BOOT ----
    print("\n[2] FAT32 BOOT 分区")
    p1 = [p for p in parts if p[0] == 1][0]
    boot = Fat32(img, p1[2] * SECTOR, p1[3] * SECTOR, label="BOOT").mount()
    listing = {k: (kind, size) for kind, k, size in boot.list_all()}
    for name in ("boot.scr", "zImage", "board.dtb", "uInitrd", "uEnv.txt"):
        entries = [v for k, v in listing.items() if k.upper().endswith("/" + name.upper())]
        ok(bool(entries), "BOOT:/%s 存在" % name)
        if entries:
            data = boot.read_file([k for k in listing if k.upper().endswith("/" + name.upper())][0])
            src = os.path.join(boot_src, name)
            if os.path.exists(src):
                want = open(src, "rb").read()
                ok(sha(data) == sha(want), "  %s 内容一致 (%d bytes)" % (name, len(data)))

    # ---- 3. ext2 ROOT ----
    print("\n[3] ext2 ROOT 分区")
    p2 = [p for p in parts if p[0] == 2][0]
    r = Ext2Reader(img, p2[2] * SECTOR)
    ok(r.magic == 0xEF53, "超级块魔数 0xEF53")
    print("     %d 块 / %d inode / %d 组 / 块大小 %d" %
          (r.blocks_count, r.inodes_count, r.groups, r.bs))

    # 建立路径->ino 映射并统计
    path2ino = {}
    nf = nd = nl = 0
    for p, ino, ftype in r.walk():
        path2ino[p] = (ino, ftype)
        if ftype == 2:
            nd += 1
        elif ftype == 7:
            nl += 1
        else:
            nf += 1
    print("     遍历: %d 文件, %d 目录, %d 符号链接" % (nf, nd, nl))
    ok(nf > 10000, "文件数量合理 (%d)" % nf)
    ok(nl > 500, "符号链接已创建 (%d)" % nl)

    # 关键文件内容校验
    checks = [
        ("/usr/bin/retroarch", os.path.join(rootfs_src, "usr/bin/retroarch")),
        ("/bin/busybox", os.path.join(rootfs_src, "bin/busybox")),
        ("/etc/init.d/rcS", os.path.join(rootfs_src, "etc/init.d/rcS")),
        ("/etc/fstab", os.path.join(rootfs_src, "etc/fstab")),
        ("/root/.config/retroarch/retroarch.cfg", os.path.join(rootfs_src, "root/.config/retroarch/retroarch.cfg")),
        ("/etc/inittab", os.path.join(rootfs_src, "etc/inittab")),
    ]
    for vpath, lpath in checks:
        e = path2ino.get(vpath)
        ok(e is not None and e[1] == 1, "%s 存在且为普通文件" % vpath)
        if e and e[1] == 1 and os.path.exists(lpath):
            want = open(lpath, "rb").read()
            got = r.read_file(e[0])
            ok(got == want, "  内容一致 (%d bytes, sha %s)" % (len(got), sha(got)))

    # 符号链接目标
    for vpath, want_target in [("/sbin/init", "../bin/busybox"),
                               ("/bin/sh", "dash"),
                               ("/etc/os-release", "../usr/lib/os-release")]:
        e = path2ino.get(vpath)
        if e and e[1] == 7:
            got = r.read_file(e[0]).decode("utf-8", "replace")
            ok(got == want_target, "%s -> %s" % (vpath, got))
        else:
            ok(False, "%s 应为符号链接" % vpath)

    # 权限抽查
    for vpath, want_mode in [("/usr/bin/retroarch", 0o755), ("/etc/init.d/rcS", 0o755),
                             ("/tmp", 0o1777), ("/bin/busybox", 0o755)]:
        e = path2ino.get(vpath)
        if e:
            mode = r.inode(e[0])["mode"]
            ok((mode & 0o7777) == want_mode, "%s 权限 %o (期望 %o)" % (vpath, mode & 0o7777, want_mode))

    # 设备节点
    for vpath in ("/dev/null", "/dev/console", "/dev/mmcblk0p2"):
        e = path2ino.get(vpath)
        ok(e is not None and e[1] in (3, 4), "%s 设备节点存在" % vpath)

    # 内核模块与核心
    ok(any(p.startswith("/lib/modules/6.1.0-50-armmp/kernel/drivers/mmc/host/sunxi-mmc.ko") for p in path2ino),
       "sunxi-mmc.ko 在镜像中")
    ncores = sum(1 for p in path2ino if p.startswith("/usr/lib/libretro/cores/") and p.endswith(".so"))
    ok(ncores >= 40, "libretro 核心 %d 个" % ncores)
    ninfo = sum(1 for p in path2ino if p.startswith("/usr/share/libretro/info/") and p.endswith(".info"))
    ok(ninfo >= 40, "核心 info 元数据 %d 个" % ninfo)

    # ---- 4. FAT32 GAMES ----
    print("\n[4] FAT32 GAMES 分区")
    p3 = [p for p in parts if p[0] == 3][0]
    games = Fat32(img, p3[2] * SECTOR, p3[3] * SECTOR, label="GAMES").mount()
    glisting = {k: (kind, size) for kind, k, size in games.list_all()}
    names = [k.split("/")[-1].upper() for k in glisting]
    ok("README.TXT" in names, "GAMES:/README.txt 存在")
    ok(any(n == "ROMS" for n in names), "GAMES:/roms 目录存在")
    ok(any(n == "SAVES" for n in names), "GAMES:/saves 目录存在")
    roms = [k for k in glisting if "/ROMS/" in k.upper()]
    ok(len(roms) >= 30, "roms 子目录 %d 个" % len(roms))

    print("\n===== 结果: %s =====" % ("全部通过" if not FAIL else "%d 项失败" % len(FAIL)))
    for m in FAIL:
        print("  !! " + m)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
