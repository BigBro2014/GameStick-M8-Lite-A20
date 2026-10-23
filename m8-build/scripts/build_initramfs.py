#!/usr/bin/env python3
"""构建 uInitrd：busybox + 启动关键内核模块 + init 脚本，cpio(newc)+gzip+uImage。
initramfs 职责：加载 sunxi-mmc/ext4 模块 → 挂载 mmcblk0p2 (ext2 根) → switch_root。"""
import os, sys, io, gzip, struct, re, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from debtools import extract_deb
from mkimage import make_ramdisk, IH_COMP_GZIP

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _detect_kver():
    """从模块树自动识别内核版本（Debian 索引更新后版本号会变）；M8_KVER 可强制指定"""
    cands = [os.path.basename(d) for d in glob.glob(os.path.join(BASE, "build/kernel/lib/modules", "*"))
             if os.path.isdir(d)]
    return os.environ.get("M8_KVER") or (sorted(cands)[-1] if cands else "6.1.0-50-armmp")


KVER = _detect_kver()
MODDIR = os.path.join(BASE, "build/kernel/lib/modules", KVER)
OUT_DIR = os.path.join(BASE, "build/boot")
os.makedirs(OUT_DIR, exist_ok=True)

# 启动关键模块（含依赖闭包）
# sunxi-mmc: 真机 TF 卡   virtio_*: QEMU 冒烟测试用（真机上无 virtio 设备，加载无害）
BOOT_MODULES = ["sunxi-mmc", "ext4", "virtio_blk", "virtio_pci", "virtio_mmio"]


def resolve_modules(names):
    """读取 .ko 的 depends= 字段，返回按依赖顺序排列的模块路径列表"""
    all_ko = {}
    for root, dirs, files in os.walk(MODDIR):
        for f in files:
            if f.endswith(".ko"):
                name = f[:-3]
                all_ko[name] = os.path.join(root, f)
                all_ko[name.replace("-", "_")] = os.path.join(root, f)

    def deps_of(ko_path):
        with open(ko_path, "rb") as fh:
            data = fh.read()
        m = re.search(rb"depends=([^\x00]*)", data)
        if not m:
            return []
        return [d.strip() for d in m.group(1).decode().split(",") if d.strip()]

    ordered, seen = [], set()

    def visit(name):
        if name in seen:
            return
        seen.add(name)
        ko = all_ko.get(name)
        if ko is None:
            print("  [warn] module not found: %s" % name)
            return
        for d in deps_of(ko):
            visit(d)
        ordered.append((name, ko))

    for n in names:
        visit(n)
    return ordered


# ---------- cpio newc ----------
def cpio_newc(entries):
    """entries: [(name, mode, is_symlink, target_or_None, data_or_None)]"""
    out = io.BytesIO()
    ino = 1000

    def hdr(name, mode, filesize, nlink=1, rdevmaj=0, rdevmin=0):
        nonlocal ino
        ino += 1
        nb = name.encode() + b"\x00"
        fields = [b"070701"]
        for v in (ino, mode, 0, 0, nlink, 0, filesize, 0, 0, rdevmaj, rdevmin, len(nb), 0):
            fields.append(b"%08X" % v)
        h = b"".join(fields)
        # newc 标准（内核 gen_init_cpio / buffer-format.rst）：数据起点为
        # "110B 头 + 名字（含 NUL）" 后对齐到 4 —— 只需补 0~3 字节。
        # 110 % 4 == 2，绝不能把"头部对齐量"和"名字对齐量"相加（namesize%4∈{1,2}
        # 时会多写 4 字节，内核解包器从该条目起整体错位）。
        pad = (4 - (len(h) + len(nb)) % 4) % 4
        out.write(h + nb + b"\x00" * pad)

    for name, mode, is_link, target, data in entries:
        if is_link:
            tb = target.encode() if isinstance(target, str) else target
            hdr(name, mode, len(tb), nlink=1)
            out.write(tb)
            out.write(b"\x00" * ((4 - len(tb) % 4) % 4))
        else:
            hdr(name, mode, len(data or b""), nlink=1)
            if data:
                out.write(data)
                out.write(b"\x00" * ((4 - len(data) % 4) % 4))
    hdr("TRAILER!!!", 0, 0, nlink=1)
    return out.getvalue()


def main():
    # 1. busybox
    bb_dir = os.path.join(BASE, "build/busybox-static")
    if not os.path.isdir(bb_dir):
        os.makedirs(bb_dir, exist_ok=True)
        extract_deb(os.path.join(BASE, "downloads/debs/busybox-static_1.35.0-4+deb12u1+b1_armhf.deb"), bb_dir)
    bb_path = os.path.join(bb_dir, "bin/busybox")
    assert os.path.exists(bb_path), "busybox binary missing"
    bb_data = open(bb_path, "rb").read()
    print("busybox: %d bytes" % len(bb_data))

    # 2. 模块
    mods = resolve_modules(BOOT_MODULES)
    print("boot modules (%d):" % len(mods), ", ".join(n for n, _ in mods))

    # 3. init 脚本
    init_sh = """#!/bin/sh
# M8 Lite A20 initramfs init
/bin/busybox mount -t proc proc /proc 2>/dev/null
/bin/busybox mount -t sysfs sys /sys 2>/dev/null
/bin/busybox mount -t devtmpfs dev /dev 2>/dev/null
echo "[initramfs] loading boot modules..."
for m in MODULE_LIST; do
  /bin/busybox insmod /lib/modules/kernel/$m 2>/dev/null || echo "  insmod $m failed"
done
/bin/busybox mkdir -p /sysroot
# 根设备候选：真机 /dev/mmcblk0p2；QEMU virt /dev/vda2；其后兜底扫描
ROOTDEV=""
for d in /dev/mmcblk0p2 /dev/vda2 /dev/mmcblk1p2 /dev/sda2; do
  [ -b "$d" ] || continue
  if /bin/busybox mount -t ext4 "$d" /sysroot 2>/dev/null; then
    if [ -e /sysroot/sbin/init ]; then ROOTDEV="$d"; break; fi
    /bin/busybox umount /sysroot 2>/dev/null
  fi
done
if [ -z "$ROOTDEV" ]; then
  for d in /dev/vda?* /dev/mmcblk?p* /dev/sd??; do
    [ -b "$d" ] || continue
    if /bin/busybox mount -t ext4 "$d" /sysroot 2>/dev/null; then
      if [ -e /sysroot/sbin/init ]; then ROOTDEV="$d"; break; fi
      /bin/busybox umount /sysroot 2>/dev/null
    fi
  done
fi
if [ -n "$ROOTDEV" ]; then
  echo "[initramfs] root found: $ROOTDEV, switching..."
  exec /bin/busybox switch_root /sysroot /sbin/init
fi
echo "[initramfs] root mount FAILED"
echo "[initramfs] try: insmod /lib/modules/kernel/<mod>.ko ; mount -t ext4 /dev/mmcblk0p2 /sysroot"
exec /bin/busybox sh
"""
    # MODULE_LIST 用实际文件名（含 .ko）：模块条目按 basename 扁平存放在
    # /lib/modules/kernel/ 下，insmod 按路径直接打开文件，名字必须与条目一致
    # （裸模块名缺 .ko 后缀会全部 ENOENT）
    init_sh = init_sh.replace("MODULE_LIST", " ".join(os.path.basename(p) for _, p in mods))

    # 4. cpio 条目
    entries = []
    for d in ("bin", "dev", "proc", "sys", "sysroot", "lib", "lib/modules", "lib/modules/kernel"):
        entries.append((d, 0o040755, False, None, None))
    # busybox + 常用 applet 符号链接
    entries.append(("bin/busybox", 0o100755, False, None, bb_data))
    for applet in ("sh", "mount", "umount", "insmod", "lsmod", "modprobe", "mkdir",
                   "sleep", "cat", "echo", "switch_root", "mknod", "ln", "cp", "mv",
                   "rm", "ls", "find", "grep", "vi", "dmesg", "poweroff", "reboot"):
        entries.append(("bin/" + applet, 0o120777, True, "busybox", None))
    entries.append(("init", 0o100755, False, None, init_sh.encode()))
    # 模块（扁平路径 /lib/modules/kernel/xxx.ko）
    for name, path in mods:
        entries.append(("lib/modules/kernel/" + os.path.basename(path),
                        0o100644, False, None, open(path, "rb").read()))

    cpio = cpio_newc(entries)
    print("cpio: %d bytes" % len(cpio))
    gz = gzip.compress(cpio, 6)
    print("gzip: %d bytes" % len(gz))
    uimg = make_ramdisk(gz, "M8-Lite-initrd", comp=IH_COMP_GZIP)

    out = os.path.join(OUT_DIR, "uInitrd")
    with open(out, "wb") as f:
        f.write(uimg)
    print("wrote %s (%d bytes)" % (out, len(uimg)))


if __name__ == "__main__":
    main()
