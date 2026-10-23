#!/usr/bin/env python3
"""生成 boot.scr（u-boot 启动脚本，uImage 格式）。
放在 BOOT FAT32 分区根目录，U-Boot distro boot 会自动查找并执行。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkimage import make_script

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "build/boot/boot.scr")

BOOT_SCRIPT = r"""# M8 Lite (Allwinner A20) U-Boot boot script
# Auto-generated. U-Boot distro boot scans mmc0 partitions for /boot.scr

echo "[M8] M8-Lite A20 boot script"

# ---- kernel cmdline ----
# console=tty0          boot messages on HDMI (no serial port on this stick)
# root=/dev/mmcblk0p2   ext2-format root partition (mounted by ext4 driver)
# rootwait              wait for SD card init
# vt.global_cursor_default=0   hide cursor (avoid flicker in games)
setenv bootargs "console=tty0 root=/dev/mmcblk0p2 rootwait rootfstype=ext4 vt.global_cursor_default=0"

# ---- load addresses (u-boot sunxi defaults, safe for 512MB/1GB RAM) ----
# kernel_addr_r=0x42000000  fdt_addr_r=0x43000000  ramdisk_addr_r=0x43300000

echo "[M8] loading zImage ..."
load mmc 0:1 ${kernel_addr_r} zImage

echo "[M8] loading board.dtb ..."
load mmc 0:1 ${fdt_addr_r} board.dtb

echo "[M8] loading uInitrd ..."
load mmc 0:1 ${ramdisk_addr_r} uInitrd

echo "[M8] bootz ..."
bootz ${kernel_addr_r} ${ramdisk_addr_r}:${filesize} ${fdt_addr_r}

echo "[M8] boot failed, dropping to U-Boot shell"
"""


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    scr = make_script(BOOT_SCRIPT, "M8-Lite-A20")
    with open(OUT, "wb") as f:
        f.write(scr)
    print("wrote %s (%d bytes, script %d bytes)" % (OUT, len(scr), len(BOOT_SCRIPT)))


if __name__ == "__main__":
    main()
