#!/usr/bin/env python3
"""M8 Lite (Allwinner A20) 游戏系统镜像组装器。

磁盘布局（512B 扇区）：
  [0, 2048)        MBR + 保留区（SPL/U-Boot 组合镜像写在 8KB 偏移处）
  p1  @2048        FAT32 "BOOT"   256MB(小卡 128MB)   boot.scr/uEnv.txt/zImage/board.dtb/uInitrd
  p2               ext2  "ROOT"   1GB(小卡 768MB)     Debian rootfs（ext4 驱动以 ext2 模式挂载）
  p3               FAT32 "GAMES"  剩余                README + roms/ + saves/ + states/

用法：
  python assemble_image.py                     # 默认 8GB 镜像
  python assemble_image.py --size 4G           # 4GB 卡
  python assemble_image.py --size 16G --rootfs D:/.../build/rootfs2
"""
import os, sys, struct, hashlib, time, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fat32 import Fat32
from ext2 import Ext2, EXT2_ROOT_INO

BASE = "D:/Projects/COM++/m8-build"
BUILD = os.path.join(BASE, "build")
UBOOT = os.path.join(BUILD, "uboot/usr/lib/u-boot/A20-OLinuXino-Lime2/u-boot-sunxi-with-spl.bin")
BOOT_SRC = os.path.join(BUILD, "boot")

# 打包时跳过的目录（RGUI 菜单不需要，省 ~440MB）
EXCLUDE_DIRS = {
    "usr/share/fonts",
    "usr/share/libretro/assets/wallpapers",
}

# /dev 静态设备节点（devtmpfs 挂载前的兜底）
DEVICES = [
    ("console", 0o020600, 5 << 8 | 1),
    ("null",    0o020666, 1 << 8 | 3),
    ("tty",     0o020666, 5 << 8 | 0),
    ("tty0",    0o020620, 4 << 8 | 0),
    ("mmcblk0",   0o020660, 179 << 8 | 0),
    ("mmcblk0p1", 0o020660, 179 << 8 | 1),
    ("mmcblk0p2", 0o020660, 179 << 8 | 2),
    ("mmcblk0p3", 0o020660, 179 << 8 | 3),
]

ROM_DIRS = ["nes", "snes", "gb", "gbc", "gba", "genesis", "sega32x", "segacd",
            "mastersystem", "gamegear", "atari2600", "atari7800", "atari5200",
            "atarilynx", "arcade", "neogeo", "neogeocd", "psx", "pcengine",
            "wonderswan", "virtualboy", "ngp", "3do", "ports", "dos", "scummvm",
            "amiga", "c64", "zxspectrum", "amstradcpc", "intellivision",
            "odyssey2", "vectrex", "gameandwatch", "pokemini"]

README = """M8 Lite (Allwinner A20) 游戏系统 - GAMES 分区
==============================================

这里是 ROM / 存档分区。往 roms/ 下的对应目录拷贝游戏文件即可，
插回游戏棒，RetroArch 菜单里选择 "Load Content" 浏览。

【目录说明】
  roms/      各机种 ROM 目录（按主机分类）
  saves/     游戏存档（SRM）
  states/    即时存档/读档

【手柄操作】
  进入菜单      按住 Select + 按 X
  菜单内移动    方向键
  确认          A
  返回          B
  加载游戏     Load Content -> 浏览到 roms/ 目录
  退出游戏     Select + Start（再按一次退出 RetroArch 到命令行）
  关机          Select + Start 退出到命令行后输入 poweroff

【第一次启动】
  系统首次启动时会自动加载驱动并打开 RetroArch 菜单，约 20-40 秒。
  若 GAMES 分区未格式化，系统会自动格式化为 exFAT（首次约 10 秒）。

【从电脑拷贝 ROM】
  本分区为 FAT32，Windows 可直接读写。单个文件不要超过 4GB。
  拷完记得先在电脑上"安全弹出硬件"，再拔卡。

【没有声音/画面？】
  参见随附《M8-Lite-A20-重建指南.html》的故障排查表。
"""

UENV_TXT = """# M8 Lite (Allwinner A20) U-Boot environment reference
# If boot.scr auto-boot fails, type these at the U-Boot console:
#   setenv bootargs "console=tty0 root=/dev/mmcblk0p2 rootwait rootfstype=ext4 vt.global_cursor_default=0"
#   load mmc 0:1 ${kernel_addr_r} zImage
#   load mmc 0:1 ${fdt_addr_r} board.dtb
#   load mmc 0:1 ${ramdisk_addr_r} uInitrd
#   bootz ${kernel_addr_r} ${ramdisk_addr_r}:${filesize} ${fdt_addr_r}
"""

SECTOR = 512
MB = 1024 * 1024
GB = 1024 * 1024 * 1024


def parse_size(s):
    s = s.strip().upper()
    if s.endswith("G"):
        return int(float(s[:-1]) * GB)
    if s.endswith("M"):
        return int(float(s[:-1]) * MB)
    return int(s)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f%s" % (n, unit)
        n /= 1024
    return "%.1fTB" % n


def mbr_entry(flag, ptype, start, count):
    e = bytearray(16)
    e[0] = flag
    e[1:4] = b"\xfe\xff\xff"
    e[4] = ptype
    e[5:8] = b"\xfe\xff\xff"
    struct.pack_into("<I", e, 8, start)
    struct.pack_into("<I", e, 12, count)
    return bytes(e)


def file_mode(rel):
    if rel.startswith(("bin/", "sbin/", "usr/bin/", "usr/sbin/")):
        return 0o100755
    if rel in ("etc/init.d/rcS", "etc/init.d/rcK"):
        return 0o100755
    if rel.endswith(".sh"):
        return 0o100755
    return 0o100644


def load_links(path):
    """读取符号链接清单；真实文件优先（先添加者胜），sbin/init 强制指向 busybox"""
    links = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or "\t" not in line:
                    continue
                p, t = line.rstrip("\n").split("\t", 1)
                if p and p not in links:
                    links[p] = t
    links["sbin/init"] = "../bin/busybox"
    return links


def pack_rootfs(fs, rootfs_dir, links_manifest):
    links = load_links(links_manifest)
    ino_map = {"": EXT2_ROOT_INO}
    added = set()
    n_files = n_dirs = n_links = n_bytes = 0
    skipped = []

    def rel_of(p):
        r = os.path.relpath(p, rootfs_dir).replace("\\", "/")
        return "" if r == "." else r

    for dirpath, dirnames, filenames in os.walk(rootfs_dir):
        dirnames.sort()
        rel_dir = rel_of(dirpath)
        parent = ino_map.get(rel_dir)
        if parent is None:
            continue
        # 子目录
        for d in list(dirnames):
            rel = (rel_dir + "/" + d) if rel_dir else d
            if rel in EXCLUDE_DIRS:
                dirnames.remove(d)
                skipped.append(rel)
                continue
            mode = 0o1777 if rel in ("tmp", "var/tmp") else 0o040755
            ino_map[rel] = fs.add_dir(parent, d, mode)
            added.add(rel)
            n_dirs += 1
        # 文件
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            rel = (rel_dir + "/" + f) if rel_dir else f
            if any(rel == e or rel.startswith(e + "/") for e in EXCLUDE_DIRS):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            n_bytes += size
            fs.add_file(parent, f, full, file_mode(rel))
            added.add(rel)
            n_files += 1

    # /dev 静态节点
    dev_ino = ino_map.get("dev")
    if dev_ino is None:
        dev_ino = fs.add_dir(EXT2_ROOT_INO, "dev", 0o040755)
    for name, mode, rdev in DEVICES:
        if "dev/" + name not in added:
            fs.add_special(dev_ino, name, mode, rdev)

    # 符号链接（清单顺序；已存在真实文件的路径跳过）
    for rel in sorted(links):
        if rel in added:
            continue
        parent_rel, _, name = rel.rpartition("/")
        parent = ino_map.get(parent_rel)
        if parent is None:
            continue
        fs.add_symlink(parent, name, links[rel])
        added.add(rel)
        n_links += 1

    print("rootfs packed: %d dirs, %d files (%s), %d symlinks" %
          (n_dirs, n_files, human(n_bytes), n_links))
    if skipped:
        print("excluded: %s" % ", ".join(skipped))
    return n_bytes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="8G", help="镜像总大小，如 8G / 4G / 16384M")
    ap.add_argument("--out", default=os.path.join(BUILD, "m8-lite-a20.img"))
    ap.add_argument("--rootfs", default=os.environ.get("M8_ROOTFS_DIR", os.path.join(BUILD, "rootfs")))
    ap.add_argument("--boot-mb", type=int, default=0, help="BOOT 分区 MB（0=自动）")
    ap.add_argument("--root-mb", type=int, default=0, help="ROOT 分区 MB（0=自动）")
    args = ap.parse_args()

    total = parse_size(args.size)
    total_sec = total // SECTOR
    if total < 2 * GB:
        print("!! 镜像至少需要 2GB")
        sys.exit(1)

    # ---- 分区规划 ----
    if args.boot_mb:
        boot_sec = args.boot_mb * MB // SECTOR
    else:
        boot_sec = (256 if total >= 4 * GB else 128) * MB // SECTOR
    if args.root_mb:
        root_sec = args.root_mb * MB // SECTOR
    else:
        root_sec = (1024 if total >= 4 * GB else 768) * MB // SECTOR

    p1_start = 2048
    p2_start = p1_start + boot_sec
    p3_start = p2_start + root_sec
    p3_sec = total_sec - p3_start
    if p3_sec < 64 * MB // SECTOR:
        print("!! GAMES 分区太小：%s。换大卡或减小 --root-mb" % human(p3_sec * SECTOR))
        sys.exit(1)

    print("镜像: %s (%d 扇区)" % (args.out, total_sec))
    print("  p1 BOOT  @%-9d %8s  FAT32" % (p1_start, human(boot_sec * SECTOR)))
    print("  p2 ROOT  @%-9d %8s  ext2 " % (p2_start, human(root_sec * SECTOR)))
    print("  p3 GAMES @%-9d %8s  FAT32" % (p3_start, human(p3_sec * SECTOR)))

    if not os.path.exists(UBOOT):
        print("!! 缺少 u-boot: %s" % UBOOT)
        sys.exit(1)
    if not os.path.isdir(args.rootfs):
        print("!! 缺少 rootfs: %s" % args.rootfs)
        sys.exit(1)

    t0 = time.time()

    # ---- 1. 稀疏镜像 + MBR + U-Boot ----
    with open(args.out, "wb") as f:
        f.truncate(total)
    with open(args.out, "r+b") as f:
        mbr = bytearray(512)
        mbr[446:462] = mbr_entry(0x80, 0x0C, p1_start, boot_sec)
        mbr[462:478] = mbr_entry(0x00, 0x83, p2_start, root_sec)
        mbr[478:494] = mbr_entry(0x00, 0x0C, p3_start, p3_sec)
        mbr[510:512] = b"\x55\xaa"
        f.write(mbr)
        f.seek(8192)
        uboot = open(UBOOT, "rb").read()
        f.write(uboot)
        f.flush()
        os.fsync(f.fileno())
    print("[1/4] MBR + U-Boot 写入完成 (%s @8KB)" % human(len(uboot)))

    # ---- 2. FAT32 BOOT ----
    boot = Fat32(args.out, p1_start * SECTOR, boot_sec * SECTOR, label="BOOT", max_spc=8)
    boot.format()
    for name in ("boot.scr", "zImage", "board.dtb", "uInitrd", "uEnv.txt"):
        src = os.path.join(BOOT_SRC, name)
        if not os.path.exists(src):
            if name == "uEnv.txt":
                boot.write_file(name, data=UENV_TXT.encode("ascii"))
                print("  BOOT:/%-12s %10s (auto)" % (name, human(len(UENV_TXT))))
                continue
            print("  !! 缺少启动文件: %s" % src)
            sys.exit(1)
        data = open(src, "rb").read()
        boot.write_file(name, data=data)
        print("  BOOT:/%-12s %10s" % (name, human(len(data))))
    boot.close()
    print("[2/4] BOOT 分区完成 (簇大小 %dKB)" % (boot.spc * 512 // 1024))

    # ---- 3. ext2 ROOT ----
    root = Ext2(args.out, p2_start * SECTOR, root_sec * SECTOR,
                block_size=1024, label="ROOT", bytes_per_inode=8192)
    root.format()
    used = pack_rootfs(root, args.rootfs, os.path.join(BUILD, "rootfs.links"))
    root.flush()
    root.close()
    if used > root_sec * SECTOR * 0.9:
        print("!! 警告: rootfs %s 超过 ROOT 分区 90%%" % human(used))
    print("[3/4] ROOT 分区完成 (%d 块组, %s 数据)" % (root.groups, human(used)))

    # ---- 4. FAT32 GAMES ----
    games = Fat32(args.out, p3_start * SECTOR, p3_sec * SECTOR, label="GAMES", max_spc=32)
    games.format()
    games.write_file("README.txt", data=README.encode("utf-8"))
    games.mkdir("roms")
    for d in ROM_DIRS:
        games.mkdir("roms/" + d)
    games.mkdir("saves")
    games.mkdir("states")
    games.close()
    print("[4/4] GAMES 分区完成 (簇大小 %dKB, %d 个 roms 目录)" %
          (games.spc * 512 // 1024, len(ROM_DIRS) + 1))

    size_on_disk = os.path.getsize(args.out)
    print("\n完成: %s (文件系统内 %s, 用时 %.0fs)" %
          (args.out, human(used), time.time() - t0))
    print("镜像已就绪，可在 WSL 中运行 wsl-test.sh 验证，或用 Rufus/Etcher 刷入 TF 卡。")


if __name__ == "__main__":
    main()
