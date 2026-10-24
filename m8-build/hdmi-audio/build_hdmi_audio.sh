#!/bin/bash
# M8 Lite A20 —— 在 WSL 中用完整内核源码树构建带 HDMI 音频支持的 sun4i-drm-hdmi.ko
#
# 背景：主线 6.1 的 sun4i-drm-hdmi 只有视频。HDMI 音频驱动（Stefan Mavrodiev
# 2020 年为 Olimex 编写，Armbian 长期以外挂补丁维护）把音频代码编进
# sun4i-drm-hdmi.ko 本身。本脚本把补丁打进完整内核源码树，以 external（M=）
# 方式交叉编译该模块，产物 vermagic/符号 CRC/depends 与原版一致，可直接替换。
# （为什么必须 external 而不是 in-tree 单目标，见第 9 节注释：in-tree 单模块
#   构建拿不到 __versions CRC 和 depends。）
#
# 为什么用完整源码树（而不是 Debian linux-headers 包 out-of-tree）：
#   Debian 的 linux-kbuild 只提供按宿主架构预编译的 modpost：它对
#   devicetable-offsets 的探测是 64 位的（sizeof(of_device_id)=200），而 ARM
#   模块的表条目是 196 字节/条，modpost 的 device_id_check 直接 FATAL
#   （内核上游 device_id_check 的已知缺陷：注释说"跨架构时忽略检查"但没实现；
#   Debian 虽然给 kbuild 打了 lsb/msb × 32/64 四变体分发补丁，但 external
#   module 构建下仍可能选错，且版本间行为不一，不可依赖）。
#   用源码树构建时 modpost 从源码现场编译：empty.o / devicetable-offsets.s
#   交给 ARM 交叉编译器探测，elfconfig.h=ELFCLASS32、offsets=196，与模块完全
#   匹配，问题从根上消失——这也是内核官方的交叉编译方式。
#
# 附带好处：源码树保留在 WSL 本地盘（~/.cache/m8-lite/linux-<ver>），已打好
#   HDMI 音频补丁、配置与目标内核一致，随时可以重编整个内核镜像，例如：
#     make -C ~/.cache/m8-lite/linux-<ver> -j$(nproc) \
#          ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- \
#          DEBIAN_KERNEL_NO_CC_VERSION_CHECK=1 \
#          VERSION=6 PATCHLEVEL=1 SUBLEVEL=0 EXTRAVERSION=-50 LOCALVERSION=-armmp \
#          Image modules dtbs
#   （VERSION 等参数以脚本输出为准；DEBIAN_KERNEL_NO_CC_VERSION_CHECK 见第 7 节）
#
# 用法（在 WSL 里）：
#   bash /mnt/d/Projects/M8-lite/m8-build/hdmi-audio/build_hdmi_audio.sh
#
# 产物：hdmi-audio/out/sun4i-drm-hdmi.ko
# 之后回到 Windows 跑：python scripts/install_hdmi_audio.py
set -euo pipefail

# ---- 路径（WSL 里 D: 挂在 /mnt/d）----
BASE="${M8_BASE:-/mnt/d/Projects/M8-lite/m8-build}"
HDMI="$BASE/hdmi-audio"
OUT="$HDMI/out"
DL="$HDMI/downloads"
# 源码树放 WSL 本地盘：/mnt/d（drvfs）上编译会极慢，且符号链接/权限有坑
SRCROOT="${M8_SRCROOT:-$HOME/.cache/m8-lite}"
mkdir -p "$OUT" "$DL" "$SRCROOT"

# ---- 1. 从内核 deb 推断版本 ----
deb="$(ls "$BASE"/downloads/debs/linux-image-*-armmp_*.deb 2>/dev/null | head -1 || true)"
[ -n "$deb" ] || { echo "ERROR: 找不到 linux-image-*-armmp deb（先跑 scripts/fetch_pkgs.py）"; exit 1; }
b="$(basename "$deb")"
# linux-image-6.1.0-50-armmp_6.1.176-1_armhf.deb
KVER="${b#linux-image-}"; KVER="${KVER%%_*}"          # 6.1.0-50-armmp
DEBVER="${b#linux-image-*}"; DEBVER="${DEBVER#*_}"; DEBVER="${DEBVER%_*}"  # 6.1.176-1
ABI="${KVER%-*}"                                        # 6.1.0-50
KBUILD="${KVER%%.*}.$(echo "$KVER" | cut -d. -f2)"      # 6.1
SRC="$SRCROOT/linux-$DEBVER"
echo "内核: $KVER  deb 版本: $DEBVER"
echo "源码树: $SRC"

fetch() {  # wget/curl 兼容（精简镜像可能只有一个）
  if command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  elif command -v curl >/dev/null 2>&1; then
    curl -sSfL -o "$2" "$1"
  else
    echo "ERROR: 既没有 wget 也没有 curl"; return 1
  fi
}

# ---- 2. 下载源码 deb 和 flavour headers deb ----
# linux-source-<KBUILD>（all 架构）：上游 6.1.176 源码 tarball 的 deb 封装
# linux-headers-<KVER>（armhf）：只需要里面的 .config 和 Module.symvers
#   —— .config 是目标内核的权威配置；Module.symvers 是目标内核导出符号的
#   权威 CRC 表（modpost 靠它给导入符号生成 __crc_*）。这个包是 armhf，
#   跨架构不能 dpkg -i，只解包取文件。
SRCDEB="linux-source-${KBUILD}_${DEBVER}_all.deb"
FLAVDEB="linux-headers-${KVER}_${DEBVER}_armhf.deb"
for f in "$SRCDEB" "$FLAVDEB"; do
  if [ ! -s "$DL/$f" ]; then
    echo "下载 $f（较大，请耐心）..."
    fetch "https://deb.debian.org/debian/pool/main/l/linux/$f" "$DL/$f" \
      || { echo "ERROR: 下载失败 $f"; exit 1; }
  fi
done

# ---- 3. 构建依赖（先装好，后面解压/打补丁/编译都要用）----
# 源码树构建需要：make/bc/flex/bison（kconfig）、gcc（宿主工具 modpost 等）、
# patch（打补丁）、libssl-dev / libelf-dev（scripts 下的证书/BTF 工具）、
# 交叉编译器（ARM 模块）。
# .kernelvariables 的坑（gcc-12 写死）在这里不存在——源码树用普通 gcc。
missing=()
command -v make    >/dev/null 2>&1 || missing+=(make)
command -v bc      >/dev/null 2>&1 || missing+=(bc)
command -v flex    >/dev/null 2>&1 || missing+=(flex)
command -v bison   >/dev/null 2>&1 || missing+=(bison)
command -v patch   >/dev/null 2>&1 || missing+=(patch)
command -v gcc     >/dev/null 2>&1 || missing+=(build-essential)
[ -e /usr/include/openssl/ssl.h ]  || missing+=(libssl-dev)
[ -e /usr/include/libelf.h ]       || missing+=(libelf-dev)
command -v modinfo >/dev/null 2>&1 || missing+=(kmod)   # 第 10 节 vermagic 自检用
command -v file    >/dev/null 2>&1 || missing+=(file)   # 第 8 节打印 modpost 架构用
command -v readelf >/dev/null 2>&1 || missing+=(binutils) # 第 10 节 __versions 自检用
CROSS=""
if [ "$(dpkg --print-architecture)" = "armhf" ]; then
  echo "本机就是 armhf，用原生 gcc"
else
  command -v arm-linux-gnueabihf-gcc >/dev/null 2>&1 || missing+=(crossbuild-essential-armhf)
  CROSS="arm-linux-gnueabihf-"
fi
if [ "${#missing[@]}" -gt 0 ]; then
  echo "安装构建依赖（需要 sudo，首次约 250~400MB）: ${missing[*]}"
  sudo apt-get update -qq || true
  sudo apt-get install -y "${missing[@]}" || { echo "ERROR: 依赖安装失败"; exit 1; }
fi
command -v make >/dev/null 2>&1 || { echo "ERROR: make 仍未安装"; exit 1; }
command -v "${CROSS}gcc" >/dev/null 2>&1 || { echo "ERROR: 找不到 ${CROSS}gcc"; exit 1; }

# ---- 4. 解包源码树（幂等；版本变了要求手动清理，绝不误删用户数据）----
if [ -d "$SRC" ] && [ "$(cat "$SRC/.m8-debver" 2>/dev/null || true)" != "$DEBVER" ]; then
  echo "ERROR: $SRC 不是 $DEBVER 的源码树（可能是旧版本）。"
  echo "       确认无用后手动删除再跑：  rm -rf \"$SRC\""
  exit 1
fi
if [ ! -f "$SRC/Makefile" ]; then
  free_kb="$(df -Pk "$SRCROOT" | awk 'NR==2{print $4}')"
  if [ "${free_kb:-0}" -lt 3000000 ]; then
    echo "warn: $SRCROOT 剩余空间不足 3GB（源码树解压后约 1.1GB，构建产物另算）"
  fi
  echo "解压内核源码（约 1 分钟）..."
  TMP="$(mktemp -d)"
  dpkg-deb -x "$DL/$SRCDEB" "$TMP"
  tarball="$(find "$TMP" -name 'linux-source-*.tar.xz' | head -1)"
  [ -n "$tarball" ] || { echo "ERROR: deb 里没有 linux-source-*.tar.xz"; rm -rf "$TMP"; exit 1; }
  tar -xJf "$tarball" -C "$TMP"
  # 找解出来的源码目录（TMP 下若干目录里，带 Makefile 和 drivers/ 的那个）
  srcdir=""
  for d in "$TMP"/*/; do
    if [ -f "${d}Makefile" ] && [ -d "${d}drivers" ]; then srcdir="${d%/}"; break; fi
  done
  [ -n "$srcdir" ] || { echo "ERROR: 解压后没找到源码目录"; rm -rf "$TMP"; exit 1; }
  rm -rf "$SRC"
  mv "$srcdir" "$SRC"
  rm -rf "$TMP"
  echo "$DEBVER" > "$SRC/.m8-debver"
  echo "源码树就绪: $SRC"
else
  echo "源码树已存在，跳过解压"
fi

# ---- 5. 打 HDMI 音频补丁（幂等）----
PATCH="$HDMI/sun4i-hdmi-audio-6.1.patch"
if [ -f "$SRC/drivers/gpu/drm/sun4i/sun4i_hdmi_audio.c" ]; then
  echo "HDMI 音频补丁已应用，跳过"
else
  echo "应用 HDMI 音频补丁..."
  patch -p1 -d "$SRC" < "$PATCH"
  echo "补丁应用完成"
fi

# ---- 6. 从 flavour headers deb 取 .config 和 Module.symvers ----
FLAV="$SRC/.m8-flavour"
if [ ! -f "$FLAV/Module.symvers" ] || [ ! -f "$FLAV/.config" ]; then
  rm -rf "$FLAV"
  dpkg-deb -x "$DL/$FLAVDEB" "$FLAV"
fi
cp "$FLAV/usr/src/linux-headers-$KVER/.config" "$SRC/.config"
cp "$FLAV/usr/src/linux-headers-$KVER/Module.symvers" "$SRC/Module.symvers"
# 顺便读出目标内核的 UTS_RELEASE（如 6.1.0-50-armmp），第 6 节要用
UTS="$(sed -n 's/^#define UTS_RELEASE "\(.*\)"$/\1/p' \
      "$FLAV/usr/src/linux-headers-$KVER/include/generated/utsrelease.h")"
[ -n "$UTS" ] || { echo "ERROR: 读不到目标 UTS_RELEASE"; exit 1; }
echo "目标内核 release: $UTS"

# ---- 7. 版本变量注入 ----
# Debian 打包内核时在命令行注入 EXTRAVERSION/LOCALVERSION（config 里
# CONFIG_LOCALVERSION 是空的），源码 Makefile 是上游的 6.1.176，直接编出来
# 的 vermagic 会是 "6.1.176"，与目标内核的 "6.1.0-50-armmp" 不符、加载被拒。
# 这里从目标 UTS_RELEASE 反推，命令行变量覆盖 Makefile 赋值。
V_TRIPLE="${UTS%%-*}"          # 6.1.0
V_MAJOR="${V_TRIPLE%%.*}"
V_REST="${V_TRIPLE#*.}"; V_MINOR="${V_REST%%.*}"; V_PATCH="${V_REST#*.}"
TAIL="${UTS#"$V_TRIPLE"}"; TAIL="${TAIL#-}"   # 50-armmp
if [ "$TAIL" = "$UTS" ]; then TAIL=""; fi
if [ -n "$TAIL" ]; then
  V_EXTRA="-${TAIL%%-*}"       # -50（ABI 号）
  V_LOCAL="-${TAIL#*-}"        # -armmp（flavour）
else
  V_EXTRA=""; V_LOCAL=""
fi
# make 命令行变量（所有 make 调用都要带，保证 kernel.release 一致）
# DEBIAN_KERNEL_NO_CC_VERSION_CHECK=1：Debian 给源码 Makefile 打了补丁，
#   prepare 阶段会比对 $(CC) --version 与 config 里的 CONFIG_CC_VERSION_TEXT
#   （Debian 打包机记录的是 "gcc-12 (Debian 12.2.0-...) 12.2.0"）。交叉编译器
#   第一行是 "arm-linux-gnueabihf-gcc (...)"，必然不匹配而报错。该检查只防
#   "用错编译器"，vermagic 不含编译器版本，关掉安全（Debian 自己在
#   .kernelvariables 里也是关的）。
MKVARS=(
  ARCH=arm
  CROSS_COMPILE="$CROSS"
  DEBIAN_KERNEL_NO_CC_VERSION_CHECK=1
  VERSION="$V_MAJOR" PATCHLEVEL="$V_MINOR" SUBLEVEL="$V_PATCH"
  EXTRAVERSION="$V_EXTRA" LOCALVERSION="$V_LOCAL"
)
echo "注入版本变量: VERSION=$V_MAJOR PATCHLEVEL=$V_MINOR SUBLEVEL=$V_PATCH EXTRAVERSION=$V_EXTRA LOCALVERSION=$V_LOCAL"

cd "$SRC"

# ---- 8. 配置 + 准备构建环境 ----
# olddefconfig：用目标内核的 .config 定稿（新选项取默认值）
# modules_prepare：编译 scripts/ 下的宿主工具——重点是从源码编译 modpost，
#   它用 ARM 交叉编译器探测出的 ELFCLASS32 + 196 字节 offsets（见文件头注释）
echo "olddefconfig..."
make "${MKVARS[@]}" olddefconfig >"$OUT/prepare.log" 2>&1 || {
  echo "ERROR: olddefconfig 失败，日志: $OUT/prepare.log"; tail -20 "$OUT/prepare.log"; exit 1; }
echo "modules_prepare（编译 scripts/ 宿主工具，首次约 1~3 分钟）..."
make "${MKVARS[@]}" modules_prepare >>"$OUT/prepare.log" 2>&1 || {
  echo "ERROR: modules_prepare 失败，日志: $OUT/prepare.log"; tail -30 "$OUT/prepare.log"; exit 1; }
# 校验现场编译的 modpost 确实是 32 位配置（看到 196 就对）
if [ -f scripts/mod/modpost ]; then
  echo "modpost 已从源码构建: $(file -b scripts/mod/modpost 2>/dev/null | cut -d, -f1-3)"
fi

# ---- 9. 生成 external 模块目录并交叉编译 ----
# 为什么是 external（M=）而不是 in-tree 单目标 make .../sun4i-drm-hdmi.ko：
#   in-tree 单模块构建时，modpost 只从 vmlinux.o 拿 CRC 和符号归属
#   （scripts/Makefile.modpost：vmlinux.o 不在就退化成 modules-only.symvers，
#   编译日志里那句 "WARNING: vmlinux.o is missing. Modules may not have
#   dependencies or modversions." 就是它）。我们没有 vmlinux，结果
#   __versions 全空、depends 全空——而目标内核 CONFIG_MODVERSIONS=y，
#   内核 check_version() 对"导出方有 CRC 而模块没有对应条目"直接返回 0，
#   模块加载即失败（"no symbol version for xxx"）；depends 为空还会让
#   预载/modprobe 顺序失控。
#   external 模式下 Makefile.modpost 会给 modpost 传 -i Module.symvers，
#   CRC 和模块归属都从它来（实测：该文件 CRC 与原版 .ko 的 __versions
#   逐条一致，条目数 = 未定义符号 86 + modpost 合成的 module_layout 1 条）。
EXT="$HDMI/extmod"
rm -rf "$EXT"; mkdir -p "$EXT"
SUN4I="$SRC/drivers/gpu/drm/sun4i"
for f in sun4i_hdmi_audio.c sun4i_hdmi_ddc_clk.c sun4i_hdmi_enc.c \
         sun4i_hdmi_i2c.c sun4i_hdmi_tmds_clk.c; do
  [ -f "$SUN4I/$f" ] || { echo "ERROR: 源码树里没有 $f（补丁没打上？）"; exit 1; }
  cp "$SUN4I/$f" "$EXT/$f"
done
cat > "$EXT/Kbuild" <<'EOF'
# 由 build_hdmi_audio.sh 从打补丁的内核源码树自动生成，请勿手工修改
obj-m += sun4i-drm-hdmi.o
sun4i-drm-hdmi-y := sun4i_hdmi_ddc_clk.o sun4i_hdmi_enc.o sun4i_hdmi_i2c.o \
                    sun4i_hdmi_tmds_clk.o sun4i_hdmi_audio.o
# 补丁用 #ifdef CONFIG_DRM_SUN4I_HDMI_AUDIO 守护（Kconfig 里 default y，
# 但 external 构建不读内核 .config，这里显式传入）
ccflags-y += -DCONFIG_DRM_SUN4I_HDMI_AUDIO=1
# 兄弟头文件（sun4i_hdmi.h / sun4i_crtc.h / sun4i_drv.h 等）从源码树取，
# 保证与补丁后的版本严格一致
ccflags-y += -I$(srctree)/drivers/gpu/drm/sun4i
EOF
# 防御性重申 Module.symvers：第 8 节的 make 之后树根的这个文件必须还是
# flavour deb 里那份（external modpost 靠 -i 读它生成 __crc_ 条目）
cp "$FLAV/usr/src/linux-headers-$KVER/Module.symvers" "$SRC/Module.symvers"
echo "编译 external 模块（源码取自打补丁的源码树）..."
if ! make "${MKVARS[@]}" M="$EXT" modules -j"$(nproc)" >"$OUT/build.log" 2>&1; then
  echo "ERROR: 编译失败。完整日志: $OUT/build.log，末尾 30 行："
  tail -30 "$OUT/build.log"
  exit 1
fi
tail -8 "$OUT/build.log"

KO="$EXT/sun4i-drm-hdmi.ko"
[ -f "$KO" ] || { echo "ERROR: 编译未产出 sun4i-drm-hdmi.ko"; exit 1; }

# ---- 10. 自检：vermagic / __versions / depends，任何一项不对加载都会失败 ----
ORIG="$BASE/build/kernel/lib/modules/$KVER/kernel/drivers/gpu/drm/sun4i/sun4i-drm-hdmi.ko"
if [ -f "$ORIG" ]; then
  v1="$(modinfo -F vermagic "$KO" | head -1)"
  v2="$(modinfo -F vermagic "$ORIG" | head -1)"
  echo "新模块 vermagic: $v1"
  echo "原模块 vermagic: $v2"
  if [ "$v1" != "$v2" ]; then
    echo "ERROR: vermagic 不一致，模块加载会被拒。检查版本注入推导（UTS_RELEASE=$UTS）。"
    exit 1
  fi
  echo "vermagic 一致 OK"
fi
# __versions：目标内核 CONFIG_MODVERSIONS=y，每个导入符号都要有 CRC 条目，
#   否则内核 check_version() 返回 0，加载报 "no symbol version for xxx"。
#   module_layout 是 modpost 合成条目，所以预期比未定义符号多 1 条。
#   每条目固定 64 字节：{u32 crc; char sym[60];}
und_n="$(readelf -sW "$KO" | awk '$7=="UND" && $8!="" {n++} END{print n+0}')"
# __versions 节的 Size（十六进制）。坑：readelf 有 GNU binutils / llvm 等实现，
#   节表列布局不同（GNU: Name Type Address Off Size；llvm: Name Size VMA Type），
#   写死列号（$3/$7）在另一种实现下取到空值或错列。这里按 Name 列定位，
#   用 Name 后第一个字段是不是段类型名区分布局，不假设列号。
vsz_hex="$(readelf -SW "$KO" | awk '
  { for (i = 1; i <= NF; i++) if ($i == "__versions") {
      t = $(i+1)
      if (t ~ /^[A-Z]+$/) { s = $(i+4) } else { s = $(i+1) }
      sub(/^0x/, "", s)
      print s
      exit
    } }')"
ver_n=0
[ -n "$vsz_hex" ] && ver_n=$(( 16#$vsz_hex / 64 ))
echo "__versions 条目: $ver_n（未定义符号 $und_n，预期 $((und_n+1))）"
if [ "$ver_n" -lt "$und_n" ]; then
  echo "ERROR: __versions 条目不足（$ver_n < $und_n）。目标内核带 modversions，"
  echo "       缺 CRC 的符号会在加载时报 no symbol version。检查树根的 Module.symvers。"
  echo "--- 诊断：readelf -SW 实际输出（前 25 行）---"
  readelf -SW "$KO" 2>&1 | sed -n '1,25p'
  echo "--- 诊断结束 ---"
  exit 1
fi
# depends：为空会让 modprobe / 启动预载顺序失控（原版是 cec,drm,drm_kms_helper，
#   音频版还应包含 snd-soc-core 等）
dep="$(modinfo -F depends "$KO" | head -1)"
echo "depends: $dep"
if [ -z "$dep" ]; then
  echo "ERROR: depends 为空，modprobe 无法排加载顺序。检查 Module.symvers。"
  exit 1
fi

# ---- 11. 交付 ----
cp "$KO" "$OUT/sun4i-drm-hdmi.ko"
echo
echo "模块大小: $(stat -c%s "$OUT/sun4i-drm-hdmi.ko") 字节"
echo "依赖（应包含 snd-soc-core）: $(modinfo -F depends "$OUT/sun4i-drm-hdmi.ko")"
echo "别名: $(modinfo -F alias "$OUT/sun4i-drm-hdmi.ko" | head -3 | tr '\n' ' ')"
echo
echo "=== 完成 ==="
echo "产物: $OUT/sun4i-drm-hdmi.ko"
echo "源码树保留在: $SRC（已打补丁，可随时重编内核镜像，命令见脚本头注释）"
echo "external 构建目录: $EXT（Kbuild 由脚本从源码树生成，重跑会重建）"
echo "下一步（Windows 侧）："
echo "  python D:/Projects/M8-lite/m8-build/scripts/install_hdmi_audio.py"
