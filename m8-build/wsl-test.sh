#!/usr/bin/env bash
# ============================================================================
# M8 Lite (Allwinner A20) 游戏系统镜像 —— WSL 验证脚本
#
# 用法（在 WSL Ubuntu 中）：
#   sudo apt update && sudo apt install -y qemu-system-arm dosfstools e2fsprogs util-linux
#   bash /mnt/d/Projects/COM++/m8-build/wsl-test.sh [镜像路径]
#
# 默认镜像：/mnt/d/Projects/COM++/m8-build/build/m8-lite-a20.img
# 脚本只读镜像，不会修改任何文件。QEMU 部分需要 sudo 以外的普通权限即可。
# ============================================================================
set -u

IMG="${1:-/mnt/d/Projects/COM++/m8-build/build/m8-lite-a20.img}"
BOOTDIR="$(dirname "$IMG")/../boot"
BOOTDIR="$(cd "$BOOTDIR" 2>/dev/null && pwd || echo /mnt/d/Projects/COM++/m8-build/build/boot)"

if [ ! -f "$IMG" ]; then
    echo "!! 镜像不存在: $IMG"
    echo "!! 先完成镜像组装，或把镜像路径作为第一个参数传入"
    exit 1
fi
echo "镜像: $IMG ($(du -h "$IMG" | cut -f1) 实际占用)"

# ---------------------------------------------------------------- 1. MBR
echo
echo "===== [1/5] MBR 分区表 ====="
sfdisk -d "$IMG" || { echo "!! sfdisk 解析失败"; exit 1; }

mapfile -t STARTS < <(sfdisk -d "$IMG" | grep -oP 'start=\s*\K[0-9]+')
mapfile -t SIZES  < <(sfdisk -d "$IMG" | grep -oP 'size=\s*\K[0-9]+')
NP=${#STARTS[@]}
echo "-> 解析到 $NP 个分区"

# ---------------------------------------------------------------- 2. SPL
echo
echo "===== [2/5] SPL 引导头 (@8KB，应为 eGON.BT0) ====="
MAGIC=$(dd if="$IMG" bs=1 skip=8192 count=8 2>/dev/null)
echo "-> $MAGIC"
[ "$MAGIC" = "eGON.BT0" ] && echo "PASS: SPL magic ok" || echo "FAIL: SPL magic 不对"

# ---------------------------------------------------------------- 3. 文件系统校验
echo
echo "===== [3/5] 文件系统只读校验（fsck） ====="
L1=""; L2=""; L3=""
cleanup() { for l in "$L1" "$L2" "$L3"; do [ -n "$l" ] && sudo losetup -d "$l" 2>/dev/null; done; }
trap cleanup EXIT

if [ "$NP" -ge 1 ]; then
    L1=$(sudo losetup -f --show -o $((STARTS[0]*512)) --sizelimit $((SIZES[0]*512)) --read-only "$IMG" 2>/dev/null)
fi
if [ "$NP" -ge 2 ]; then
    L2=$(sudo losetup -f --show -o $((STARTS[1]*512)) --sizelimit $((SIZES[1]*512)) --read-only "$IMG" 2>/dev/null)
fi
if [ "$NP" -ge 3 ]; then
    L3=$(sudo losetup -f --show -o $((STARTS[2]*512)) --sizelimit $((SIZES[2]*512)) --read-only "$IMG" 2>/dev/null)
fi
echo "-> loop: BOOT=${L1:-none} ROOT=${L2:-none} GAMES=${L3:-none}"

if [ -n "$L1" ]; then
    echo "--- BOOT 分区 (FAT32) ---"
    fsck.vfat -vn "$L1" 2>&1 | tail -6
fi
if [ -n "$L2" ]; then
    echo "--- ROOT 分区 (ext2) ---"
    e2fsck -fn "$L2" 2>&1 | tail -6
fi
if [ -n "$L3" ]; then
    echo "--- GAMES 分区 (FAT32) ---"
    fsck.vfat -vn "$L3" 2>&1 | tail -4
fi

# ---------------------------------------------------------------- 4. 目录内容
echo
echo "===== [4/5] 分区内容抽查 ====="
if [ -n "$L1" ]; then
    echo "--- BOOT:/ ---"
    mdir -i "$L1" ::/ 2>/dev/null || ls "/mnt$(true)" 2>/dev/null
fi
if [ -n "$L2" ]; then
    echo "--- ROOT:/ ---"
    sudo debugfs -R "ls -l /" "$L2" 2>/dev/null | head -20
    echo "--- ROOT:/usr/bin/retroarch ---"
    sudo debugfs -R "stat /usr/bin/retroarch" "$L2" 2>/dev/null | head -8
    echo "--- ROOT:/etc/init.d/rcS 前 12 行 ---"
    sudo debugfs -R "cat /etc/init.d/rcS" "$L2" 2>/dev/null | head -12
    echo "--- ROOT:/sbin/init (符号链接目标) ---"
    sudo debugfs -R "stat /sbin/init" "$L2" 2>/dev/null | grep -iE "fast link|symlink|target|Type" | head -4
fi
if [ -n "$L3" ]; then
    echo "--- GAMES:/ ---"
    mdir -i "$L3" ::/ 2>//null | head -15
    mdir -i "$L3" ::/roms 2>/dev/null | head -10
fi

# ---------------------------------------------------------------- 5. QEMU 冒烟测试
echo
echo "===== [5/5] QEMU 冒烟测试（内核+initramfs+根文件系统，virt 机型） ====="
echo "说明：QEMU 无 A20 机型，此项验证内核/initramfs/rootfs 启动链；"
echo "      SPL/U-Boot/HDMI/手柄等 A20 专属部分只能在真机验证。"
echo

if ! command -v qemu-system-arm >/dev/null 2>&1; then
    echo "!! 未安装 qemu-system-arm，跳过（sudo apt install -y qemu-system-arm）"
    exit 0
fi
for f in zImage uInitrd; do
    [ -f "$BOOTDIR/$f" ] || { echo "!! 缺少 $BOOTDIR/$f，跳过 QEMU"; exit 0; }
done

echo "--- 启动中（最多 240 秒，看到 'starting RetroArch' 即表示启动链贯通）---"
timeout 240 qemu-system-arm \
    -M virt -cpu cortex-a15 -m 512 \
    -kernel "$BOOTDIR/zImage" \
    -initrd "$BOOTDIR/uInitrd" \
    -append "console=ttyAMA0 root=/dev/vda2 rootwait rootfstype=ext4 panic=-1" \
    -drive file="$IMG",format=raw,if=none,id=hd0 \
    -device virtio-blk-device,drive=hd0 \
    -nographic -no-reboot 2>&1 | tail -60
RC=$?
echo
if [ $RC -eq 124 ]; then
    echo "QEMU: 超时（若上面已有 RetroArch 重启循环输出，属预期：virt 机型无帧缓冲）"
else
    echo "QEMU: 退出码 $RC"
fi

echo
echo "===== 验证结束 ====="
echo "关键期待："
echo "  [3] fsck.vfat / e2fsck 无 error（有 clean/done 字样）"
echo "  [4] BOOT 含 boot.scr/zImage/board.dtb/uInitrd；ROOT 含 retroarch/rcS；GAMES 含 README.txt/roms"
echo "  [5] 出现 '[initramfs] root found: /dev/vda2' 与 '[M8] starting RetroArch...'"
