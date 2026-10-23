#!/usr/bin/env python3
"""只补下载 .info 元数据文件（核心 .so 已下载完成）"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from debtools import http_get

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INFO_DIR = os.path.join(BASE, "downloads", "coreinfo")
os.makedirs(INFO_DIR, exist_ok=True)

INFO = "https://raw.githubusercontent.com/libretro/libretro-core-info/master/%s_libretro.info"

CORES = [
    "fceumm", "nestopia",
    "snes9x2010", "bsnes_mercury_performance",
    "gambatte", "sameboy",
    "mgba",
    "genesis_plus_gx", "picodrive",
    "stella2014", "prosystem",
    "atari800",
    "fbneo", "mame2003_plus",
    "pcsx_rearmed", "opera",
    "mednafen_pce_fast", "mednafen_wswan", "mednafen_vb",
    "mednafen_ngp", "mednafen_lynx", "mednafen_supergrafx",
    "prboom", "ecwolf", "tyrquake",
    "dosbox_svn",
    "scummvm",
    "puae",
    "vice_x64", "fuse", "cap32",
    "freeintv", "o2em", "vecx",
    "gw", "pokemini",
    "neocd",
    "xrick", "reminiscence", "nxengine", "mrboom",
]

ok, fail = 0, []
for c in CORES:
    info_path = os.path.join(INFO_DIR, c + "_libretro.info")
    if os.path.exists(info_path) and os.path.getsize(info_path) > 0:
        ok += 1
        continue
    try:
        http_get(INFO % c, info_path, retries=3, timeout=90)
        ok += 1
    except Exception as e:
        fail.append((c, str(e)[:60]))

print("info ok: %d / %d" % (ok, len(CORES)))
for c, e in fail:
    print("FAIL", c, e)
