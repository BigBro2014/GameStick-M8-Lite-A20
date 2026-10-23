#!/usr/bin/env python3
"""构建 Debian rootfs 树：
1. 解包全部 282 个 deb 到 build/rootfs/
2. 拷入内核模块树 + 自生成 depmod 元数据
3. 拷入 41 个 libretro 核心
4. 写入系统配置（busybox init / rcS / fstab / retroarch.cfg / 模块加载表）
"""
import os, sys, glob, re, shutil, tarfile, io, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from debtools import extract_deb, http_get

BASE = "D:/Projects/COM++/m8-build"
DEBS = os.path.join(BASE, "downloads/debs")
# 输出目录：默认 build/rootfs，可用环境变量 M8_ROOTFS_DIR 覆盖（避免删除旧树）
ROOTFS = os.environ.get("M8_ROOTFS_DIR", os.path.join(BASE, "build/rootfs"))


def detect_kver():
    """从已解压的内核模块树自动识别版本号（Debian 索引更新后版本号会变）；
    可用 M8_KVER 环境变量强制指定。"""
    cands = [os.path.basename(d) for d in glob.glob(os.path.join(BASE, "build/kernel/lib/modules", "*"))
             if os.path.isdir(d)]
    if cands:
        return os.environ.get("M8_KVER") or sorted(cands)[-1]
    return "6.1.0-50-armmp"  # 回退：内核 deb 尚未解压


KVER = detect_kver()
MODSRC = os.path.join(BASE, "build/kernel/lib/modules", KVER)
CORES = os.path.join(BASE, "downloads/cores")
BUSYBOX = os.path.join(BASE, "build/busybox-static")

RUNTIME_MODULES = [
    # USB 主机栈（2.4G 手柄接收器 + EHCI/OHCI USB 口）
    "phy-sun4i-usb", "sunxi", "musb_hdrc",
    "ehci-hcd", "ehci-platform", "ohci-hcd", "ohci-platform",
    # 输入（HID 手柄）
    "usbhid", "hid-generic", "evdev",
    # HID 专用手柄驱动（hid-generic 兜底之外的标准/品牌手柄；依赖由 resolve_order 排序）
    "ff-memless",                       # 力反馈基类
    "hid-dr",                           # DragonRise —— 廉价 2.4G 手柄/接收器主力
    "hid-sunplus",                      # Sunplus 方案
    "hid-xinmo",                        # Xin-Mo 双人街机摇杆
    "hid-zpff", "hid-gaff", "hid-twinhan", "hid-emsff", "hid-sjoy",
    "hid-redragon",                     # Redragon 游戏手柄
    "led-class-multicolor",             # hid-playstation 依赖
    "hid-sony",                         # PS3
    "hid-playstation",                  # PS4/PS5
    "hid-nintendo",                     # Switch Pro
    "hid-microsoft",                    # MS 手柄
    "hid-steam",                        # Steam Controller
    "joydev",                           # /dev/input/jsX 经典接口
    # USB 大容量存储（插 U 盘拷 ROM 用）
    "scsi_common", "scsi_mod", "crc-t10dif", "crc64-rocksoft", "t10-pi",
    "sd_mod", "usb-storage", "uas",
    # 显示（HDMI/KMS + fbdev 仿真）
    "sun4i-drm", "sun4i-drm-hdmi", "sun4i-backend", "drm", "drm_kms_helper",
    "drm_display_helper", "sun4i-tcon", "sun4i-frontend",
    # 文件系统（GAMES 分区 FAT32/exFAT）
    "vfat", "fat", "exfat",
    # 声卡（模拟音频口；HDMI 音频主线内核不支持）
    "snd-soc-core", "snd-soc-simple-card", "snd-pcm", "snd",
    "sun4i-codec", "sun4i-i2s",
    # 根分区（ext2 格式由 ext4 驱动挂载）
    "ext4", "jbd2", "mbcache",
]


LINKS_MANIFEST = os.path.join(BASE, "build/rootfs.links")


def extract_all():
    debs = sorted(glob.glob(os.path.join(DEBS, "*.deb")))
    print("extracting %d debs..." % len(debs))
    links_f = open(LINKS_MANIFEST, "w", encoding="utf-8")
    links_f.write("# path\ttarget  (symbolic links, created inside the ext2 image)\n")
    total_links = 0
    ok = fail = 0
    try:
        for p in debs:
            try:
                total_links += extract_deb(p, ROOTFS, links_out=links_f)
                ok += 1
            except Exception as e:
                print("  EXTRACT FAIL:", os.path.basename(p), e)
                fail += 1
    finally:
        links_f.close()
    print("extracted: %d ok, %d failed, %d symlinks recorded" % (ok, fail, total_links))


def build_module_resolver():
    """扫描全部 .ko，建立 名称->路径 与 depends 表"""
    name2path = {}
    depends = {}
    aliases = {}
    for root, dirs, files in os.walk(MODSRC):
        for f in files:
            if not f.endswith(".ko"):
                continue
            path = os.path.join(root, f)
            rel = os.path.relpath(path, MODSRC).replace("\\", "/")
            modname = f[:-3]
            name2path[modname] = rel
            name2path[modname.replace("-", "_")] = rel
            with open(path, "rb") as fh:
                data = fh.read()
            m = re.search(rb"depends=([^\x00]*)", data)
            deps = []
            if m:
                deps = [d.strip() for d in m.group(1).decode().split(",") if d.strip()]
            depends[rel] = deps
            for am in re.finditer(rb"alias=([^\x00]*)", data):
                aliases.setdefault(rel, []).append(am.group(1).decode())
    return name2path, depends, aliases


def resolve_order(want, name2path, depends):
    """拓扑排序：返回按依赖顺序排列的 rel path 列表"""
    ordered, seen = [], set()

    def visit(name):
        if name in seen:
            return
        seen.add(name)
        rel = name2path.get(name)
        if rel is None:
            print("  [warn] module missing: %s" % name)
            return
        for d in depends.get(rel, []):
            visit(d)
        ordered.append(rel)

    for n in want:
        visit(n)
    return ordered


def copy_modules(name2path, depends, aliases, ordered):
    dst = os.path.join(ROOTFS, "lib/modules", KVER)
    if os.path.isdir(dst):
        # 内核 deb 已自带完整模块树，原地使用（rmtree 会触发批量删除保护）
        print("module tree already present (from kernel deb)")
    else:
        print("copying module tree...")
        shutil.copytree(MODSRC, dst)

    # modules.dep
    with open(os.path.join(dst, "modules.dep"), "w") as f:
        for rel in sorted(depends):
            deps = depends[rel]
            if deps:
                dep_paths = []
                for d in deps:
                    p = name2path.get(d)
                    if p and p != rel:
                        dep_paths.append(p)
                if dep_paths:
                    f.write("%s: %s\n" % (rel, " ".join(dep_paths)))
                else:
                    f.write("%s:\n" % rel)
            else:
                f.write("%s:\n" % rel)
    # modules.alias
    with open(os.path.join(dst, "modules.alias"), "w") as f:
        for rel in sorted(aliases):
            for a in aliases[rel]:
                f.write("alias %s %s\n" % (a, rel[:-3].replace("-", "_")))
    # modules.symbols（从 depends 反推不够，仅写空表避免 kmod 报警）
    open(os.path.join(dst, "modules.symbols"), "w").close()
    # modules.order / builtin（内核 deb 自带）
    for extra in ("modules.order", "modules.builtin", "modules.builtin.modinfo"):
        src = os.path.join(MODSRC, extra)
        if os.path.exists(src):
            shutil.copy2(src, dst)
    print("depmod metadata written")

    # /etc/modules.load（busybox insmod 按序加载）
    load_list = "\n".join(ordered) + "\n"
    with open(os.path.join(ROOTFS, "etc/modules.load"), "w") as f:
        f.write("# auto-generated: kernel modules in dependency order\n")
        f.write(load_list)
    print("modules.load: %d modules" % len(ordered))
    return ordered


def copy_cores():
    dst = os.path.join(ROOTFS, "usr/lib/libretro/cores")
    os.makedirs(dst, exist_ok=True)
    n = 0
    for so in glob.glob(os.path.join(CORES, "*_libretro.so")):
        shutil.copy2(so, dst)
        n += 1
    print("cores copied: %d" % n)


def copy_info_files():
    """libretro 核心元数据（RetroArch 菜单显示核心名/支持格式必需）"""
    src = os.path.join(BASE, "downloads/coreinfo")
    dst = os.path.join(ROOTFS, "usr/share/libretro/info")
    os.makedirs(dst, exist_ok=True)
    n = 0
    for f in glob.glob(os.path.join(src, "*_libretro.info")):
        shutil.copy2(f, dst)
        n += 1
    print("info files copied: %d" % n)


AUTOCONFIG_SRC = os.path.join(BASE, "downloads/autoconfig")
AUTOCONFIG_URL = "https://github.com/libretro/retroarch-joypad-autoconfig/archive/refs/heads/master.tar.gz"


def fetch_autoconfig():
    """下载 libretro 官方手柄配置库并拷入 rootfs。
    RetroArch 的 input_autodetect_enable 依靠 udev/*.cfg 为陌生手柄自动匹配按键
    （M8 Lite 的 2.4G HID 手柄没有专属配置，靠这批通用配置覆盖）。"""
    dst = os.path.join(ROOTFS, "usr/share/libretro/autoconfig/udev")
    os.makedirs(dst, exist_ok=True)
    cfgs = glob.glob(os.path.join(AUTOCONFIG_SRC, "*.cfg"))
    if not cfgs:
        print("downloading retroarch-joypad-autoconfig ...")
        tar_path = os.path.join(BASE, "downloads", "joypad-autoconfig.tar.gz")
        http_get(AUTOCONFIG_URL, tar_path, retries=3, timeout=600)
        os.makedirs(AUTOCONFIG_SRC, exist_ok=True)
        with tarfile.open(tar_path, "r:gz") as tf:
            for m in tf.getmembers():
                if m.name.startswith("retroarch-joypad-autoconfig-master/udev/") \
                        and m.name.endswith(".cfg") and m.isfile():
                    m.name = os.path.basename(m.name)
                    tf.extract(m, AUTOCONFIG_SRC, filter="data")
        cfgs = glob.glob(os.path.join(AUTOCONFIG_SRC, "*.cfg"))
    n = 0
    for c in cfgs:
        shutil.copy2(c, dst)
        n += 1
    print("autoconfig profiles: %d" % n)


def write_configs():
    # ---- busybox ----
    bb = os.path.join(BUSYBOX, "bin/busybox")
    dst_bb = os.path.join(ROOTFS, "bin/busybox")
    os.makedirs(os.path.dirname(dst_bb), exist_ok=True)
    shutil.copy2(bb, dst_bb)
    os.chmod(dst_bb, 0o755)
    applets = ["sh", "mount", "umount", "insmod", "lsmod", "mkdir", "sleep",
               "cat", "echo", "ln", "cp", "mv", "rm", "ls", "dmesg", "poweroff",
               "reboot", "vi", "grep", "find", "ifconfig", "ping", "df", "free"]
    # Windows 无符号链接权限：登记到清单，由镜像构建器在 ext2 内创建
    links_f = open(LINKS_MANIFEST, "a", encoding="utf-8")
    for a in applets:
        p = os.path.join(ROOTFS, "bin", a)
        if not os.path.lexists(p):
            links_f.write("bin/%s\tbusybox\n" % a)
    links_f.write("sbin/init\t../bin/busybox\n")
    links_f.close()

    # ---- /etc/inittab ----
    open(os.path.join(ROOTFS, "etc/inittab"), "w").write(
        "# M8 Lite busybox init\n"
        "::sysinit:/etc/init.d/rcS\n"
        "::respawn:-/bin/sh\n"
        "::ctrlaltdel:/bin/busybox reboot\n"
        "::shutdown:/bin/busybox umount -a -r\n"
    )

    # ---- /etc/init.d/rcS ----
    os.makedirs(os.path.join(ROOTFS, "etc/init.d"), exist_ok=True)
    open(os.path.join(ROOTFS, "etc/init.d/rcS"), "w").write("""#!/bin/sh
# M8 Lite (Allwinner A20) 系统启动脚本
/bin/busybox mount -t devtmpfs dev /dev
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sys /sys
/bin/busybox mount -t tmpfs tmp /tmp 2>/dev/null
/bin/busybox mkdir -p /var/run /var/log /var/lock /mnt/games /root/.config/retroarch

export LD_LIBRARY_PATH=/lib/arm-linux-gnueabihf:/usr/lib/arm-linux-gnueabihf:/lib:/usr/lib
export HOME=/root
export PATH=/bin:/sbin:/usr/bin:/usr/sbin

# 首次启动生成动态库缓存
/sbin/ldconfig 2>/dev/null

echo "[M8] loading kernel modules..."
while read -r m; do
  case "$m" in ""|"#"*) continue ;; esac
  /bin/busybox insmod /lib/modules/KVER/$m 2>/dev/null || echo "  [warn] $m"
done < /etc/modules.load

# 热插拔：注册 mdev 为内核 hotplug 助手——未预载的模块（如特定 hid-xxx）按
# MODALIAS 自动经 /sbin/modprobe 加载。设备节点本身由 devtmpfs 提供。
echo /bin/busybox mdev > /proc/sys/kernel/hotplug 2>/dev/null

# USB 大容量存储：开机扫描并只读挂载到 /mnt/usb（拷 ROM 用；随后插入需手动 mount）
/bin/busybox mkdir -p /mnt/usb
for d in /dev/sd[a-z]1; do
  [ -b "$d" ] || continue
  if /bin/busybox mount -o ro -t vfat "$d" /mnt/usb 2>/dev/null; then
    echo "[M8] usb disk: $d -> /mnt/usb (ro)"
    break
  fi
done

# GAMES 分区：从根分区设备名推导（避免硬编码 mmcblk0p3）
ROOTDEV=""
while read -r dev mp fst rest; do
  if [ "$mp" = "/" ]; then ROOTDEV="$dev"; break; fi
done < /proc/mounts
GAMESDEV="${ROOTDEV%?}3"
echo "[M8] root=$ROOTDEV games=$GAMESDEV"

# 挂载 GAMES 分区（构建时已预格式化为 FAT32；失败则自动格式化为 exFAT）
if ! /bin/busybox mount -t vfat "$GAMESDEV" /mnt/games 2>/dev/null; then
  if ! /bin/busybox mount -t exfat "$GAMESDEV" /mnt/games 2>/dev/null; then
    echo "[M8] formatting GAMES partition (exFAT, first boot)..."
    /usr/sbin/mkfs.exfat -n GAMES "$GAMESDEV" >/dev/null 2>&1
    /bin/busybox mount -t exfat "$GAMESDEV" /mnt/games 2>/dev/null || echo "  [warn] games mount failed"
  fi
fi
/bin/busybox mkdir -p /mnt/games/roms /mnt/games/saves /mnt/games/states 2>/dev/null

# 读取游戏说明（若存在）
[ -f /mnt/games/README.txt ] && /bin/busybox cat /mnt/games/README.txt

echo "[M8] starting RetroArch..."
export SDL_VIDEODRIVER=fbdev
export SDL_AUDIODRIVER=alsa
export SDL_NOMOUSE=1
fails=0
while true; do
  /usr/bin/retroarch --config /root/.config/retroarch/retroarch.cfg
  fails=$((fails+1))
  if [ $fails -ge 3 ]; then
    echo "[M8] RetroArch exited $fails times -> dropping to shell"
    echo "[M8] type 'retroarch' to retry, 'poweroff' to shut down"
    exec /bin/sh
  fi
  # QUIT & POWER OFF：菜单里选 "Quit RetroArch" 退到这里的处理——
  #   15 秒内有任意手柄/键盘输入 → 重新启动 RetroArch
  #   15 秒无输入               → 卸载文件系统并自动关机
  echo "[M8] RetroArch exited."
  echo "[M8] 按任意键返回 RetroArch；15 秒无操作将自动关机"
  : > /tmp/evhit
  pids=""
  for d in /dev/input/event*; do
    [ -e "$d" ] || continue
    /bin/busybox cat "$d" >> /tmp/evhit 2>/dev/null &
    pids="$pids $!"
  done
  n=0
  while [ $n -lt 15 ]; do
    [ -s /tmp/evhit ] && break
    /bin/busybox sleep 1
    n=$((n+1))
  done
  [ -n "$pids" ] && kill $pids 2>/dev/null
  if [ -s /tmp/evhit ]; then
    echo "[M8] input detected -> restarting RetroArch"
    continue
  fi
  echo "[M8] no input -> powering off..."
  /bin/busybox umount /mnt/games 2>/dev/null
  /bin/busybox umount /mnt/usb 2>/dev/null
  /bin/busybox umount -a -r 2>/dev/null
  /bin/busybox sync
  /bin/busybox poweroff -f
  echo "[M8] poweroff failed -> dropping to shell"
  echo "[M8] type 'poweroff' to shut down"
  exec /bin/sh
done
""".replace("KVER", KVER))
    os.chmod(os.path.join(ROOTFS, "etc/init.d/rcS"), 0o755)

    # ---- /etc/fstab ----
    open(os.path.join(ROOTFS, "etc/fstab"), "w").write(
        "# M8 Lite fstab\n"
        "/dev/mmcblk0p2  /          ext2  defaults  0 1\n"
        "/dev/mmcblk0p3  /mnt/games vfat  utf8,defaults  0 0\n"
    )

    # ---- 基础 etc 文件 ----
    for name, content in [
        ("etc/hostname", "m8lite\n"),
        ("etc/hosts", "127.0.0.1 localhost m8lite\n"),
        ("etc/resolv.conf", "nameserver 8.8.8.8\n"),
        ("etc/ld.so.conf", "/lib/arm-linux-gnueabihf\n/usr/lib/arm-linux-gnueabihf\n/lib\n/usr/lib\n"),
    ]:
        p = os.path.join(ROOTFS, name)
        if not os.path.exists(p):
            open(p, "w").write(content)

    # ---- 目录骨架 ----
    for d in ("var/run", "var/log", "var/lock", "var/tmp", "run", "tmp",
              "mnt/games", "root/.config/retroarch", "root/.config/retroarch/system",
              "root/.config/retroarch/screenshots", "media", "srv", "opt"):
        os.makedirs(os.path.join(ROOTFS, d), exist_ok=True)
    os.chmod(os.path.join(ROOTFS, "tmp"), 0o1777)
    os.chmod(os.path.join(ROOTFS, "var/tmp"), 0o1777)


RETROARCH_CFG = """# M8 Lite RetroArch 配置（A20 / 无 GPU 加速 / SDL2 fbdev）
# 由构建脚本生成

# ---- 视频 ----
video_driver = "sdl2"
video_fullscreen = "true"
video_vsync = "true"
video_smooth = "false"
video_font_enable = "false"
video_threaded = "false"
video_fps_show = "false"
video_windowed_fullscreen = "false"
video_scale_integer = "false"

# ---- 音频 ----
audio_driver = "alsathreaded"
audio_enable = "true"

# ---- 输入 ----
input_driver = "sdl2"
input_autodetect_enable = "true"
joypad_autoconfig_dir = "/usr/share/libretro/autoconfig"
input_enable_hotkey = "2"
input_menu_toggle = "9"
input_menu_toggle_gamepad_combo = "2"
input_player1_analog_dpad_mode = "1"
# Select(2)+Start(3) 关闭当前游戏回主菜单（原绑定 Escape 键盘，手柄用不了）
input_exit_emulator = "3"

# ---- 即时存储（Save States）----
# 手柄按钮编号：0=B 1=Y 2=Select 3=Start 8=A 9=X 10=L 11=R 12=L2 13=R2 14=L3 15=R3
# _btn 为官方手柄绑定形式；无后缀的传统形式一并给出，两种解析方式下均生效。
# 存档位置：savestate_directory（GAMES 分区 /states，可写）
# Select + R2 即时存储；Select + L2 读取存档
# Select + R / L 切换存档槽（0~9 共 10 个位置）
input_enable_hotkey_btn = "2"
input_menu_toggle_btn = "9"
input_exit_emulator_btn = "3"
input_save_state_btn = "13"
input_load_state_btn = "12"
input_state_slot_increase_btn = "11"
input_state_slot_decrease_btn = "10"
input_save_state = "13"
input_load_state = "12"
input_state_slot_increase = "11"
input_state_slot_decrease = "10"
# Quick Menu（Select+X 呼出的游戏内菜单）里的 Save/Load State 条目
quick_menu_show_save_load_state = "true"

# ---- 菜单 ----
menu_driver = "rgui"
menu_show_core_updater = "false"
menu_show_online_updater = "false"
# QUIT & POWER OFF：主菜单显示 "Quit RetroArch"（退出后由 rcS 接管：
# 按任意键返回 RetroArch，15 秒无输入自动关机）
menu_show_quit_retroarch = "true"
menu_show_restart_retroarch = "true"
rgui_browser_directory = "/mnt/games/roms"

# ---- 路径 ----
libretro_directory = "/usr/lib/libretro/cores"
libretro_info_path = "/usr/share/libretro/info"
content_directory = "/mnt/games/roms"
screenshot_directory = "/root/.config/retroarch/screenshots"
savefile_directory = "/mnt/games/saves"
savestate_directory = "/mnt/games/states"
system_directory = "/root/.config/retroarch/system"

# ---- 关闭不需要的功能 ----
core_updater_buildbot_url = ""
core_updater_buildbot_cores_url = ""
check_for_updates = "false"
builtin_mediaplayer_enable = "false"
builtin_imageviewer_enable = "false"
cheevos_enable = "false"
rewind_enable = "false"
network_cmd_enable = "false"
"""


def write_retroarch_cfg():
    p = os.path.join(ROOTFS, "root/.config/retroarch/retroarch.cfg")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(RETROARCH_CFG)
    # 也放一份到 /etc 作为兜底
    os.makedirs(os.path.join(ROOTFS, "etc"), exist_ok=True)
    open(os.path.join(ROOTFS, "etc/retroarch.cfg"), "w").write(RETROARCH_CFG)


def main():
    finish_only = "--finish" in sys.argv
    if finish_only:
        print("finish mode: skipping deb extraction")
    else:
        if os.path.isdir(ROOTFS) and os.listdir(ROOTFS):
            print("ERROR: %s exists and is not empty." % ROOTFS)
            print("Use a different dir: set M8_ROOTFS_DIR=D:/.../build/rootfs-new")
            sys.exit(1)
        extract_all()
    name2path, depends, aliases = build_module_resolver()
    ordered = resolve_order(RUNTIME_MODULES, name2path, depends)
    copy_modules(name2path, depends, aliases, ordered)
    copy_cores()
    copy_info_files()
    fetch_autoconfig()
    write_configs()
    write_retroarch_cfg()
    # 统计
    total = 0
    nfiles = 0
    for root, dirs, files in os.walk(ROOTFS):
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):
                total += os.path.getsize(fp)
                nfiles += 1
    print("rootfs: %d files, %.1f MB" % (nfiles, total / 1024 / 1024))
    # 报告缺失模块
    missing = [n for n in RUNTIME_MODULES if n not in name2path and n.replace("-", "_") not in name2path]
    if missing:
        print("NOTE: modules not found in tree (maybe builtin): %s" % missing)


if __name__ == "__main__":
    main()
