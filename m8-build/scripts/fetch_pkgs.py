#!/usr/bin/env python3
"""解析目标软件包集合并下载所有 .deb"""
import os, sys, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from debtools import parse_packages, resolve, http_get

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DL = os.path.join(BASE, "downloads", "debs")
os.makedirs(DL, exist_ok=True)

INDEX = os.path.join(BASE, "downloads", "Packages-armhf")
MIRROR = "https://deb.debian.org/debian/"

TARGETS = [
    # 系统基础
    "base-files", "base-passwd", "bash", "dash", "coreutils", "sed", "grep",
    "gzip", "tar", "xz-utils", "util-linux", "mount", "e2fsprogs", "kmod",
    "exfatprogs", "busybox-static", "nano", "firmware-realtek", "alsa-utils",
    "libc-bin", "hostname", "netbase", "ifupdown",
    # 游戏
    "retroarch",
    # 内核
    "linux-image-armmp",
]

def main():
    pkgs = parse_packages(INDEX)
    print("index: %d packages" % len(pkgs))
    print("resolving %d targets..." % len(TARGETS))
    resolved = resolve(pkgs, TARGETS, verbose=False)
    print("resolved closure: %d packages" % len(resolved))
    # 写清单
    with open(os.path.join(BASE, "downloads", "pkglist.txt"), "w") as f:
        for name in sorted(resolved):
            st = resolved[name]
            f.write("%s %s %s %s\n" % (name, st.get("Version"), st.get("Filename"), st.get("Size")))
    total = sum(int(resolved[n].get("Size", 0)) for n in resolved)
    print("total download: %.1f MB" % (total / 1e6))
    # 下载（跳过已存在且大小匹配的）
    todo = []
    for name in sorted(resolved):
        st = resolved[name]
        fn = st["Filename"]
        dest = os.path.join(DL, os.path.basename(fn))
        if os.path.exists(dest) and os.path.getsize(dest) == int(st.get("Size", -1)):
            continue
        todo.append((MIRROR + fn, dest, int(st.get("Size", 0))))
    print("to download: %d files" % len(todo))
    from concurrent.futures import ThreadPoolExecutor
    def job(t):
        url, dest, size = t
        try:
            http_get(url, dest, retries=6, timeout=180, min_size=size)
            return (dest, True)
        except Exception as e:
            return (dest, str(e))
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for dest, r in ex.map(job, todo):
            if r is True:
                ok += 1
            else:
                fail += 1
                print("FAIL", dest, r)
    print("done: %d ok, %d failed" % (ok, fail))

if __name__ == "__main__":
    main()
