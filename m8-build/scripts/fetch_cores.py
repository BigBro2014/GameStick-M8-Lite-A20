#!/usr/bin/env python3
"""从 libretro 官方构建服务器下载选定的 armhf 核心，并获取对应的 .info 元数据"""
import os, sys, zipfile, urllib.request, urllib.error, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from debtools import http_get

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORES_DIR = os.path.join(BASE, "downloads", "cores")
INFO_DIR = os.path.join(BASE, "downloads", "coreinfo")
os.makedirs(CORES_DIR, exist_ok=True)
os.makedirs(INFO_DIR, exist_ok=True)

BUILD = "https://buildbot.libretro.com/nightly/linux/armhf/latest/%s_libretro.so.zip"
INFO = "https://raw.githubusercontent.com/libretro/libretro-core-info/master/%s_libretro.info"

# 为 A20 (双核A7 1.2GHz + Mali-400MP2) 精选的核心集
CORES = [
    "fceumm", "nestopia",                      # NES
    "snes9x2010", "bsnes_mercury_performance", # SNES
    "gambatte", "sameboy",                     # GB/GBC
    "mgba",                                    # GBA
    "genesis_plus_gx", "picodrive",            # MD/32X/CD
    "stella2014", "prosystem",                 # Atari 2600/7800
    "atari800",                                # Atari 800/5200
    "fbneo", "mame2003_plus",                  # 街机
    "pcsx_rearmed", "opera",                   # PS1 / 3DO
    "mednafen_pce_fast", "mednafen_wswan", "mednafen_vb",
    "mednafen_ngp", "mednafen_lynx", "mednafen_supergrafx",
    "prboom", "ecwolf", "tyrquake",            # FPS
    "dosbox_svn",                              # DOS
    "scummvm",                                 # 冒险游戏
    "puae",                                    # Amiga
    "vice_x64", "fuse", "cap32",               # C64/ZX/CPC
    "freeintv", "o2em", "vecx",                # Intellivision/Odyssey2/Vectrex
    "gw", "pokemini",                          # Game&Watch / Pokemon Mini
    "neocd",                                   # Neo Geo CD
    "xrick", "reminiscence", "nxengine", "mrboom",
]

def main():
    from concurrent.futures import ThreadPoolExecutor
    def dl_core(c):
        zip_path = os.path.join(CORES_DIR, c + ".zip")
        so_path = os.path.join(CORES_DIR, c + "_libretro.so")
        if not os.path.exists(so_path):
            http_get(BUILD % c, zip_path, retries=6, timeout=240)
            with zipfile.ZipFile(zip_path) as z:
                names = [n for n in z.namelist() if n.endswith(".so")]
                data = z.read(names[0])
            with open(so_path, "wb") as f:
                f.write(data)
            os.remove(zip_path)
        return c, len(open(so_path, "rb").read())
    def dl_info(c):
        info_path = os.path.join(INFO_DIR, c + "_libretro.info")
        if not os.path.exists(info_path):
            try:
                http_get(INFO % c, info_path, retries=2, timeout=60)
            except Exception as e:
                # 不静默：缺 .info 的核心不会出现在 RetroArch 菜单里，必须可见
                sys.stderr.write("  [warn] info failed: %s (%s)\n" % (c, e))
        return c
    print("downloading %d cores..." % len(CORES))
    with ThreadPoolExecutor(max_workers=3) as ex:
        for c, sz in ex.map(dl_core, CORES):
            print("  %-28s %8.1f KB" % (c, sz / 1024))
    print("downloading core info files...")
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(dl_info, CORES))
    infos = os.listdir(INFO_DIR)
    print("info files: %d/%d" % (len(infos), len(CORES)))
    missing_info = [c for c in CORES
                    if not os.path.exists(os.path.join(INFO_DIR, c + "_libretro.info"))]
    if missing_info:
        print("WARN: %d 个核心缺 .info（不会出现在 RetroArch 菜单）: %s"
              % (len(missing_info), ", ".join(missing_info)))
    total = sum(os.path.getsize(os.path.join(CORES_DIR, f)) for f in os.listdir(CORES_DIR))
    print("total cores size: %.1f MB" % (total / 1e6))

if __name__ == "__main__":
    main()
