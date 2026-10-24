#!/usr/bin/env python3
"""M8 Lite A20 —— 安装带 HDMI 音频的 sun4i-drm-hdmi.ko 并重组装镜像

前置：先在 WSL 里跑
    bash /mnt/d/Projects/M8-lite/m8-build/hdmi-audio/build_hdmi_audio.sh
产出 hdmi-audio/out/sun4i-drm-hdmi.ko 后，在 Windows 跑本脚本：
    python scripts/install_hdmi_audio.py
    python scripts/install_hdmi_audio.py --revert   # 恢复原版模块

流程：vermagic 校验 -> depends 校验 -> __versions CRC 条目校验 ->
      备份原版 -> 换 .ko（内核树 + rootfs 树两处） ->
      build_rootfs --finish -> assemble_image -> verify_image。

__versions 校验的原因：目标内核 CONFIG_MODVERSIONS=y，每个导入符号都要有
CRC 条目，否则内核 check_version() 拒绝加载。in-tree 单模块构建（没有
vmlinux）会导致 __versions 全空——那种 .ko 必须在这里拦住。

注意模块树有两份副本，都必须替换：
  build/kernel/lib/modules/<kver>/   —— build_rootfs 的元数据（modules.dep）来源
  build/rootfs2/lib/modules/<kver>/  —— 实际打进镜像的文件
--finish 只按前者重新生成元数据，不会同步 .ko 文件。
"""
import os, sys, shutil, subprocess, glob

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(BASE, "scripts")
HDMI = os.path.join(BASE, "hdmi-audio")
OUT = os.path.join(HDMI, "out")
ROOTFS = os.environ.get("M8_ROOTFS_DIR", os.path.join(BASE, "build", "rootfs2"))
REL = "kernel/drivers/gpu/drm/sun4i/sun4i-drm-hdmi.ko"


def modinfo(ko, key):
    """从 .ko 的 .modinfo 段取一个字段（NUL 分隔字符串表，暴力扫全文件）"""
    data = open(ko, "rb").read()
    for chunk in data.split(b"\x00"):
        s = chunk.decode("utf-8", "replace")
        if s.startswith(key + "="):
            return s[len(key) + 1:]
    return None


def elf_versions_info(ko):
    """解析 ARM32 .ko，返回 (未定义符号数, __versions 条目数)。

    __versions 每条 64 字节（u32 CRC + NUL 结尾名称，即 struct
    modversion_info）。目标内核 CONFIG_MODVERSIONS=y 时，每个导入符号都
    必须有 CRC 条目，否则内核 check_version() 直接拒绝加载
    （"no symbol version for xxx"）。module_layout 是 modpost 合成的
    额外条目，所以健康值 = 未定义符号数 + 1。
    """
    import struct
    d = open(ko, "rb").read()
    if d[:4] != b"\x7fELF" or d[4] != 1:
        raise ValueError("不是 ELF32 文件: %s" % ko)
    e_shoff, = struct.unpack_from("<I", d, 0x20)
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", d, 0x2e)
    shs = [struct.unpack_from("<10I", d, e_shoff + i * e_shentsize)
           for i in range(e_shnum)]
    strtab_off = shs[e_shstrndx][4]

    def sname(n):
        return d[strtab_off + n:d.index(b"\x00", strtab_off + n)].decode()

    ver_n = 0
    symtab = strtab = None
    for s in shs:
        nm = sname(s[0])
        if nm == "__versions" and s[5] > 0:
            ver_n = s[5] // 64
        elif nm == ".symtab":
            symtab = s
        elif nm == ".strtab":
            strtab = s
    und_n = 0
    if symtab and strtab:
        for i in range(symtab[5] // 16):
            off = symtab[4] + i * 16
            st_name, st_value, st_size, st_info, st_other, st_shndx = \
                struct.unpack_from("<IIIBBH", d, off)
            nm = d[strtab[4] + st_name:d.index(b"\x00", strtab[4] + st_name)].decode()
            if st_shndx == 0 and nm:
                und_n += 1
    return und_n, ver_n


def run(cmd, env=None):
    print("\n>>>", " ".join(cmd))
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0:
        print("ERROR: 命令失败 exit=%d" % r.returncode)
        sys.exit(r.returncode)


def main():
    revert = "--revert" in sys.argv

    kvers = [os.path.basename(d) for d in glob.glob(os.path.join(BASE, "build/kernel/lib/modules", "*"))]
    if not kvers:
        print("ERROR: 找不到内核模块树 build/kernel/lib/modules/*")
        sys.exit(1)
    kver = sorted(kvers)[-1]
    # 两份模块树
    trees = [os.path.join(BASE, "build/kernel/lib/modules", kver),
             os.path.join(ROOTFS, "lib/modules", kver)]
    trees = [t for t in trees if os.path.isdir(t)]
    if not trees:
        print("ERROR: 找不到模块树")
        sys.exit(1)
    print("内核: %s" % kver)
    for t in trees:
        print("模块树: %s" % t)

    if revert:
        for t in trees:
            stock = os.path.join(t, REL + ".stock")
            live = os.path.join(t, REL)
            if os.path.exists(stock):
                shutil.copy2(stock, live)
                print("已恢复 %s" % live)
        print("恢复完成")
    else:
        new = os.path.join(OUT, "sun4i-drm-hdmi.ko")
        if not os.path.exists(new):
            print("ERROR: 找不到 %s" % new)
            print("请先在 WSL 执行: bash /mnt/d/Projects/M8-lite/m8-build/hdmi-audio/build_hdmi_audio.sh")
            sys.exit(1)

        # ---- vermagic 必须与原版一致，否则加载时直接被拒 ----
        v_new = modinfo(new, "vermagic")
        if not v_new:
            print("ERROR: 新模块没有 vermagic 字段，文件可能损坏")
            sys.exit(1)
        print("新模块 vermagic: %s" % v_new)
        for t in trees:
            live = os.path.join(t, REL)
            if not os.path.exists(live):
                print("ERROR: 模块树里没有 %s" % live)
                sys.exit(1)
            v_old = modinfo(live, "vermagic")
            if v_old != v_new:
                print("ERROR: %s" % live)
                print("  vermagic 不一致（现 %r / 新 %r）" % (v_old, v_new))
                print("  确认 WSL 里装的 linux-headers 与内核 deb 同版本后重跑构建。")
                sys.exit(1)
        dep = modinfo(new, "depends") or ""
        if "snd-soc-core" not in dep:
            print("ERROR: 新模块 depends=%r，不像带 HDMI 音频的版本" % dep)
            sys.exit(1)
        print("depends: %s" % dep)

        # ---- __versions CRC 条目：目标内核 modversions 强制，缺了加载即失败 ----
        try:
            und_n, ver_n = elf_versions_info(new)
        except Exception as e:
            print("ERROR: 解析 %s 失败: %s" % (new, e))
            sys.exit(1)
        print("__versions 条目: %d（未定义符号 %d，预期 %d）"
              % (ver_n, und_n, und_n + 1))
        if ver_n < und_n:
            print("ERROR: __versions CRC 条目不足（%d < %d）" % (ver_n, und_n))
            print("  目标内核 CONFIG_MODVERSIONS=y，缺 CRC 的符号加载时报")
            print("  'no symbol version for xxx'，模块根本加载不了。")
            print("  请用新版 build_hdmi_audio.sh（external M= 构建）重新生成模块。")
            sys.exit(1)

        # ---- 备份（只保留首次备份）+ 替换两处 ----
        for t in trees:
            live = os.path.join(t, REL)
            stock = live + ".stock"
            if not os.path.exists(stock):
                shutil.copy2(live, stock)
                print("已备份 %s" % stock)
            shutil.copy2(new, live)
            print("已安装 %s" % live)

    # ---- 重组装 ----
    env = dict(os.environ)
    env["M8_ROOTFS_DIR"] = ROOTFS
    run([sys.executable, os.path.join(SCRIPTS, "build_rootfs.py"), "--finish"], env=env)
    run([sys.executable, os.path.join(SCRIPTS, "assemble_image.py"), "--size", "8G"], env=env)
    run([sys.executable, os.path.join(SCRIPTS, "verify_image.py")], env=env)

    print("\n=== 完成 ===")
    print("镜像: %s" % os.path.join(BASE, "build", "m8-lite-a20.img"))
    print("启动后验证（串口/接显示器看日志/shell）：")
    print("  cat /proc/asound/cards     # 出现画面后应列出 sun4i-hdmi")
    print("  aplay -l                   # alsa-utils 已随系统安装")
    print("注意：未接显示器或显示器不支持音频时 sun4i-hdmi 声卡不会注册")
    print("      （驱动挂在 DRM 编码器上，出画面时才创建声卡）。")


if __name__ == "__main__":
    main()
