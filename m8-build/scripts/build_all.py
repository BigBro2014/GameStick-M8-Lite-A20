#!/usr/bin/env python3
"""M8 Lite (Allwinner A20) 游戏系统镜像 —— 一键构建脚本。

完整复现 build/m8-lite-a20.img 的全部步骤。每个步骤幂等可重跑：
下载类步骤断点续传/已存在则跳过，本地生成类步骤覆盖旧产物。

用法：
  python scripts/build_all.py                    # 完整构建（默认 8G 镜像）
  python scripts/build_all.py --size 4G          # 4GB 小卡
  python scripts/build_all.py --no-download      # 跳过全部网络下载（用已有缓存）
  python scripts/build_all.py --from rootfs      # 只跑 rootfs 及其后的步骤
  python scripts/build_all.py --steps dtb initrd zimage assemble verify
  python scripts/build_all.py --clean            # 用全新 rootfs 目录重新解包 282 个 deb

步骤（按序）：
  index    下载 Debian bookworm armhf Packages 索引（仅缺失时）
  pkgs     解析并下载 282 个 .deb 依赖闭包（~330MB）
  uboot    下载 u-boot-sunxi deb（Ubuntu ports）并解出 A20 SPL+U-Boot
  cores    下载 41 个 libretro armhf nightly 核心（~136MB）
  info     下载 41 个核心 .info 元数据
  kernel   解出 linux-image-armmp deb（zImage + 898 DTB + 模块树）
  busybox  解出 busybox-static deb
  bootscr  生成 boot.scr（U-Boot 启动脚本，仅 ASCII）
  dtb      给 sun7i-a20-mk808c.dtb 打补丁（USB 主机模式 + 板载声卡）
  initrd   构建 uInitrd（busybox + 启动模块 + init）
  zimage   拷贝 zImage 到 build/boot/
  rootfs   解包 282 deb + 模块元数据 + 核心 + 手柄配置 + 系统配置
  assemble 组装镜像（MBR + FAT32 BOOT + ext2 ROOT + FAT32 GAMES）
  verify   读回校验（MBR/SPL/SHA256/ext2 全文件遍历）

单独重跑某一步的脚本（等价命令）见同目录《构建手册》或 BUILD.md。
"""
import argparse
import glob
import lzma
import os
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(BASE, "scripts")
sys.path.insert(0, SCRIPTS)
from debtools import http_get, extract_deb  # noqa: E402

PY = sys.executable
DEFAULT_IMAGE = os.path.join(BASE, "build", "m8-lite-a20.img")

UBOOT_URL = ("https://ports.ubuntu.com/ubuntu-ports/pool/universe/u/u-boot/"
             "u-boot-sunxi_2021.01+dfsg-3ubuntu0~20.04.5_armhf.deb")
PACKAGES_XZ = "https://deb.debian.org/debian/dists/bookworm/main/binary-armhf/Packages.xz"
DTB_SRC = "sun7i-a20-mk808c.dtb"  # 换板型改这里（见手册"定制指南"）

STEPS = ["index", "pkgs", "uboot", "cores", "info", "kernel", "busybox",
         "bootscr", "dtb", "initrd", "zimage", "rootfs", "assemble", "verify"]
DOWNLOAD_STEPS = ("index", "pkgs", "uboot", "cores", "info")


def run(script, *args, env=None, timeout=3600):
    """调用 scripts/ 下的构建脚本，失败即中止"""
    cmd = [PY, os.path.join(SCRIPTS, script)] + [str(a) for a in args]
    print(">>>", " ".join(os.path.basename(c) if i == 0 else c for i, c in enumerate(cmd)))
    e = dict(os.environ)
    if env:
        e.update(env)
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=BASE, env=e, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SystemExit("step timeout (%ds): %s" % (timeout, script))
    if r.returncode != 0:
        raise SystemExit("step failed (exit %d): %s" % (r.returncode, " ".join(cmd)))
    print("<<< ok (%.0fs)" % (time.time() - t0))


# ---------- 下载类步骤 ----------

def step_index():
    idx = os.path.join(BASE, "downloads", "Packages-armhf")
    if os.path.exists(idx):
        print("index exists: %s (%.1f MB)" % (idx, os.path.getsize(idx) / 1e6))
        return
    xz = idx + ".xz"
    print("downloading Debian bookworm armhf Packages index ...")
    http_get(PACKAGES_XZ, xz, retries=5, timeout=600)
    with open(xz, "rb") as f, open(idx, "wb") as out:
        out.write(lzma.decompress(f.read()))
    os.remove(xz)
    print("index ready: %.1f MB" % (os.path.getsize(idx) / 1e6))


def step_pkgs():
    run("fetch_pkgs.py")


def step_uboot():
    dest = os.path.join(BASE, "downloads", "debs", "u-boot-sunxi_2021.01_armhf.deb")
    binp = os.path.join(BASE, "build", "uboot", "usr", "lib", "u-boot",
                        "A20-OLinuXino-Lime2", "u-boot-sunxi-with-spl.bin")
    if not os.path.exists(binp):
        if not os.path.exists(dest):
            print("downloading u-boot-sunxi (Ubuntu ports, bookworm 源没有此包) ...")
            http_get(UBOOT_URL, dest, retries=5, timeout=600)
        extract_deb(dest, os.path.join(BASE, "build", "uboot"))
    print("u-boot binary:", binp)


def step_cores():
    run("fetch_cores.py")


def step_info():
    run("fetch_info_only.py")


# ---------- 本地解包步骤 ----------

def step_kernel():
    kdir = os.path.join(BASE, "build", "kernel")
    if glob.glob(os.path.join(kdir, "boot", "vmlinuz-*")):
        print("kernel already extracted:", kdir)
        return
    debs = sorted(glob.glob(os.path.join(BASE, "downloads", "debs", "linux-image-*_armhf.deb")))
    if not debs:
        raise SystemExit("linux-image deb missing (run 'pkgs' step first)")
    extract_deb(debs[-1], kdir)
    print("extracted:", os.path.basename(debs[-1]))


def step_busybox():
    bdir = os.path.join(BASE, "build", "busybox-static")
    if os.path.exists(os.path.join(bdir, "bin", "busybox")):
        print("busybox already extracted:", bdir)
        return
    debs = sorted(glob.glob(os.path.join(BASE, "downloads", "debs", "busybox-static_*_armhf.deb")))
    if not debs:
        raise SystemExit("busybox-static deb missing (run 'pkgs' step first)")
    extract_deb(debs[-1], bdir)
    print("extracted:", os.path.basename(debs[-1]))


# ---------- 启动文件生成 ----------

def step_bootscr():
    run("make_bootscr.py")


def step_dtb():
    srcs = glob.glob(os.path.join(BASE, "build", "kernel", "usr", "lib",
                                  "linux-image-*", DTB_SRC))
    if not srcs:
        raise SystemExit("DTB not found: %s (run 'kernel' step first)" % DTB_SRC)
    run("dtbpatch.py", srcs[0], os.path.join(BASE, "build", "boot", "board.dtb"))


def step_initrd():
    run("build_initramfs.py")


def step_zimage():
    vms = sorted(glob.glob(os.path.join(BASE, "build", "kernel", "boot", "vmlinuz-*")))
    if not vms:
        raise SystemExit("vmlinuz not found (run 'kernel' step first)")
    dst = os.path.join(BASE, "build", "boot", "zImage")
    shutil.copy2(vms[0], dst)
    print("zImage:", dst, "(%.1f MB)" % (os.path.getsize(dst) / 1e6))


# ---------- rootfs / 组装 / 验证 ----------

def make_step_rootfs(rootfs_dir):
    def step_rootfs():
        env = {"M8_ROOTFS_DIR": rootfs_dir}
        args = []
        if os.path.isdir(rootfs_dir) and os.listdir(rootfs_dir):
            # 已解包过：跳过 282 deb 解包，只重做模块元数据/核心/手柄配置/系统配置
            args.append("--finish")
        run("build_rootfs.py", *args, env=env)
    return step_rootfs


def make_step_assemble(out, rootfs_dir, size):
    def step_assemble():
        run("assemble_image.py", "--size", size, "--out", out, "--rootfs", rootfs_dir)
    return step_assemble


def make_step_verify(out, rootfs_dir):
    def step_verify():
        run("verify_image.py", out, rootfs_dir)
    return step_verify


def main():
    ap = argparse.ArgumentParser(description="M8 Lite A20 游戏系统镜像一键构建")
    ap.add_argument("--size", default="8G", help="镜像总大小：8G / 4G / 16384M")
    ap.add_argument("--out", default=DEFAULT_IMAGE, help="输出镜像路径")
    ap.add_argument("--rootfs-dir", default=None,
                    help="rootfs 树目录（默认 build/rootfs2，存在则走 --finish 快速路径）")
    ap.add_argument("--no-download", action="store_true", help="跳过全部网络下载步骤")
    ap.add_argument("--clean", action="store_true",
                    help="使用全新 rootfs 目录重新解包（本环境禁止 rm -rf，旧目录需手动清理）")
    ap.add_argument("--from", dest="from_step", default=None, help="从某一步开始（含）")
    ap.add_argument("--steps", nargs="+", default=None, help="只跑指定步骤（按固定顺序）")
    args = ap.parse_args()

    rootfs_dir = args.rootfs_dir or os.path.join(BASE, "build", "rootfs2")
    if args.clean:
        n = 2
        while os.path.isdir(os.path.join(BASE, "build", "rootfs%d" % n)):
            n += 1
        rootfs_dir = os.path.join(BASE, "build", "rootfs%d" % n)
        print("clean mode: fresh rootfs dir:", rootfs_dir)

    steps = list(STEPS)
    if args.no_download:
        steps = [s for s in steps if s not in DOWNLOAD_STEPS]
    if args.from_step:
        if args.from_step not in STEPS:
            raise SystemExit("unknown step: %s (choose from %s)" % (args.from_step, "/".join(STEPS)))
        steps = [s for s in steps if STEPS.index(s) >= STEPS.index(args.from_step)]
    if args.steps:
        unknown = [s for s in args.steps if s not in STEPS]
        if unknown:
            raise SystemExit("unknown steps: %s" % unknown)
        steps = [s for s in STEPS if s in args.steps]

    funcs = {
        "index": step_index, "pkgs": step_pkgs, "uboot": step_uboot,
        "cores": step_cores, "info": step_info, "kernel": step_kernel,
        "busybox": step_busybox, "bootscr": step_bootscr, "dtb": step_dtb,
        "initrd": step_initrd, "zimage": step_zimage,
        "rootfs": make_step_rootfs(rootfs_dir),
        "assemble": make_step_assemble(args.out, rootfs_dir, args.size),
        "verify": make_step_verify(args.out, rootfs_dir),
    }

    print("plan   :", " -> ".join(steps))
    print("rootfs :", rootfs_dir)
    print("image  :", args.out, "(%s)" % args.size)
    t0 = time.time()
    for i, s in enumerate(steps, 1):
        print("\n[%d/%d] ==== %s ====" % (i, len(steps), s))
        funcs[s]()
    print("\nALL DONE in %.0fs -> %s" % (time.time() - t0, args.out))


if __name__ == "__main__":
    main()
