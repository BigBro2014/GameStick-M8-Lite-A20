# M8 Lite (Allwinner A20) 游戏系统镜像 — 构建手册

本手册说明 `build/m8-lite-a20.img` 是如何从零构建出来的，以及如何手工复现 / 定制。
全部流程在 **Windows 原生环境**下完成（Python 3 标准库，无需 pip 安装任何包，
无需 Linux / WSL / Docker）。

---

## 1. 镜像规格

| 项 | 值 |
|---|---|
| 总大小 | 8GB（支持 `--size 4G` 适配小卡） |
| 8KB 偏移 | U-Boot 2021.01（SPL + U-Boot 合并镜像，A20-OLinuXino-Lime2 变体，全志 BootROM 约定位置） |
| p1 BOOT | FAT32 256MB：`boot.scr` `uEnv.txt` `zImage` `board.dtb` `uInitrd` |
| p2 ROOT | ext2 1GB：Debian 12 (bookworm) armhf + busybox init + RetroArch 1.14 + 41 个 libretro 核心 |
| p3 GAMES | FAT32 剩余空间：README + 36 个机种 roms 目录 + saves/ + states/ |

启动链：BootROM → SPL(8KB) → U-Boot(distro boot, mmc0) → boot.scr → bootz(zImage+uInitrd+dtb)
→ initramfs 加载 sunxi-mmc/ext4 → 挂载 p2(ext2) → switch_root → rcS → RetroArch(RGUI 菜单)。

---

## 2. 环境要求

- Windows 10/11，Python ≥ 3.10（开发使用 3.13，仅标准库）
- 磁盘预留 **≥ 15GB**：下载 ~700MB + rootfs 树 ~1GB + 镜像 8GB
- 网络可达：`deb.debian.org`、`ports.ubuntu.com`、`buildbot.libretro.com`、
  `raw.githubusercontent.com`、`github.com`。代理慢属正常，全部支持断点续传，
  中断后重跑同一条命令即可继续
- WSL 非必需（仅用于事后验证，见 §7）

---

## 3. 目录结构

```
m8-build/
├── scripts/                 全部构建脚本（本手册的主角）
│   ├── debtools.py          deb(ar/tar) 解析、依赖闭包解析、断点续传下载
│   ├── fetch_pkgs.py        下载 Debian armhf 包闭包（282 个）
│   ├── fetch_cores.py       下载 41 个 libretro nightly 核心 + .info
│   ├── fetch_info_only.py   只补下载 .info 元数据
│   ├── fat32.py             FAT32 写入器/读取器（含 LFN 长名）
│   ├── ext2.py              ext2 写入器（块组/位图/间接块/符号链接）
│   ├── mkimage.py           uImage 头工具（boot.scr / uInitrd 封装）
│   ├── make_bootscr.py      生成 build/boot/boot.scr
│   ├── dtbpatch.py          DTB 补丁器（FDT 完整解析+序列化，round-trip 自检）
│   ├── build_initramfs.py   构建 build/boot/uInitrd
│   ├── build_rootfs.py      构建 rootfs 树（解 deb + 模块 + 核心 + 配置）
│   ├── assemble_image.py    组装最终镜像
│   ├── verify_image.py      读回校验（60+ 项）
│   ├── test_fat32.py        FAT32 测试（含磁盘重挂载回归）
│   ├── test_ext2.py         ext2 测试
│   └── build_all.py         一键构建（按顺序跑完下面所有步骤）
├── downloads/
│   ├── Packages-armhf       Debian 索引（48MB）
│   ├── debs/                282 个 .deb + u-boot deb（~330MB）
│   ├── cores/               41 个 *_libretro.so（~136MB）
│   ├── coreinfo/            41 个 *_libretro.info
│   └── autoconfig/          joypad-autoconfig 的 udev/*.cfg
├── build/
│   ├── kernel/              linux-image deb 解包：boot/{vmlinuz,config,System.map}
│   │                        + usr/lib/linux-image-<KVER>/*.dtb(898 个) + lib/modules/
│   ├── uboot/               u-boot deb 解包：usr/lib/u-boot/A20-*/u-boot-sunxi-with-spl.bin
│   ├── busybox-static/      busybox-static deb 解包：bin/busybox
│   ├── boot/                启动文件产物：boot.scr / zImage / board.dtb / uInitrd
│   ├── rootfs.links         符号链接清单（Windows 不能建链接，打包时在镜像内创建）
│   ├── rootfs2/             rootfs 树（~950MB）
│   └── m8-lite-a20.img      最终镜像
└── wsl-test.sh              WSL 验证脚本（fsck + QEMU 冒烟）
```

---

## 4. 一键构建（推荐）

```bash
cd /d/Projects/M8-lite/m8-build

python scripts/build_all.py                    # 完整构建，默认 8G
python scripts/build_all.py --size 4G          # 4GB 小卡
python scripts/build_all.py --no-download      # 跳过下载（已有缓存时）
python scripts/build_all.py --from rootfs      # 从 rootfs 步骤继续
python scripts/build_all.py --steps dtb initrd zimage assemble verify
python scripts/build_all.py --clean            # 全新 rootfs 目录重新解包
```

每个步骤幂等：下载断点续传/已存在则跳过；本地生成步骤覆盖旧产物。
下面 §5 是同样内容的分步手工版，便于理解每一步在做什么。

---

## 5. 分步构建（手工）

> 以下命令均在 `D:/Projects/M8-lite/m8-build` 目录执行。
> 「耗时」为代理较慢时的参考值，断点续传可反复重跑。

### 步骤 1 · Debian 索引（可选手动）

`fetch_pkgs.py` 依赖 `downloads/Packages-armhf`，缺失时先下载：

```bash
python -c "import lzma,urllib.request; \
open('downloads/Packages-armhf','wb').write(lzma.decompress(urllib.request.urlopen( \
'https://deb.debian.org/debian/dists/bookworm/main/binary-armhf/Packages.xz').read()))"
```

产物：`downloads/Packages-armhf`（48MB 文本索引）。

### 步骤 2 · Debian 包闭包（~330MB，耗时最长）

```bash
python scripts/fetch_pkgs.py
```

从索引解析 `TARGETS`（base-files / bash / coreutils / util-linux / e2fsprogs /
kmod / exfatprogs / busybox-static / alsa-utils / **retroarch** / **linux-image-armmp**
等 28 个目标）的递归 Depends 闭包，4 线程并发下载，按文件大小校验跳过已完成部分。

产物：`downloads/debs/` 共 **282 个 .deb**，另生成 `downloads/pkglist.txt` 清单。

### 步骤 3 · U-Boot（Debian bookworm 没有，取自 Ubuntu ports）

```bash
curl -L -o downloads/debs/u-boot-sunxi_2021.01_armhf.deb \
  "https://ports.ubuntu.com/ubuntu-ports/pool/universe/u/u-boot/u-boot-sunxi_2021.01+dfsg-3ubuntu0~20.04.5_armhf.deb"

python -c "import sys; sys.path.insert(0,'scripts'); \
from debtools import extract_deb; \
extract_deb('downloads/debs/u-boot-sunxi_2021.01_armhf.deb','build/uboot')"
```

产物：`build/uboot/usr/lib/u-boot/A20-OLinuXino-Lime2/u-boot-sunxi-with-spl.bin`
（572KB；前 32KB 是 SPL，8KB 偏移处有 eGON.BT0 魔数——全志 BootROM 的约定）。

### 步骤 4 · libretro 核心 + 元数据（~136MB）

```bash
python scripts/fetch_cores.py
python scripts/fetch_info_only.py   # 核心已下过，只补 .info 时用
```

从 `https://buildbot.libretro.com/nightly/linux/armhf/latest/<core>_libretro.so.zip`
下载（Python zipfile 解压，7z 不能解 zip）。核心清单在 `fetch_cores.py` 顶部的
`CORES` 列表（41 个：NES/SNES/GB/GBA/MD/街机/PS1/3DO/PCE/WS/VB/NGP/Lynx/DOOM/
DOS/ScummVM/Amiga/C64/ZX/Intellivision/Odyssey2/Vectrex/G&W/PokeMini 等）。

产物：`downloads/cores/*_libretro.so`、`downloads/coreinfo/*_libretro.info`。

### 步骤 5 · 内核与 busybox 解包

```bash
python -c "import sys,glob; sys.path.insert(0,'scripts'); \
from debtools import extract_deb; \
extract_deb(glob.glob('downloads/debs/linux-image-*_armhf.deb')[0],'build/kernel')"

python -c "import sys,glob; sys.path.insert(0,'scripts'); \
from debtools import extract_deb; \
extract_deb(glob.glob('downloads/debs/busybox-static_*_armhf.deb')[0],'build/busybox-static')"
```

产物：
- `build/kernel/boot/{vmlinuz-*,config-*,System.map-*}`（vmlinuz 即标准 ARM zImage）
- `build/kernel/usr/lib/linux-image-<KVER>/*.dtb`（898 个，含全部 `sun7i-a20-*`）
- `build/kernel/lib/modules/<KVER>/`（模块树）
- `build/busybox-static/bin/busybox`

> 内核版本号（KVER，如 `6.1.0-50-armmp`）由脚本从模块目录名**自动探测**，
> Debian 索引更新导致版本变化时无需改代码；也可用 `M8_KVER` 环境变量强制指定。

### 步骤 6 · boot.scr（U-Boot 启动脚本）

```bash
python scripts/make_bootscr.py
```

生成 uImage 封装的启动脚本（U-Boot distro boot 会在 mmc0 各分区根目录找 `boot.scr`）。
内容：设 bootargs（`console=tty0 root=/dev/mmcblk0p2 rootwait rootfstype=ext4`）→
依次 load zImage / board.dtb / uInitrd → `bootz`。
**注意：脚本内注释和 echo 只能 ASCII**（U-Boot 不解析中文）。

### 步骤 7 · board.dtb（设备树补丁）

```bash
python scripts/dtbpatch.py \
  build/kernel/usr/lib/linux-image-*/sun7i-a20-mk808c.dtb \
  build/boot/board.dtb
```

`dtbpatch.py` 是完整 FDT 解析+序列化器（自带 round-trip 自检，先跑
`python scripts/dtbpatch.py` 无参数版本可验证）。补丁内容：
- `usb@1c13000` → `dr_mode = "host"`（OTG 口强制主机模式，插 2.4G 手柄接收器）
- `codec@1c22c00` + `i2s@1c22000` → `status = "okay"`，自动分配 phandle
- 新增 `/sound` simple-audio-card 节点（板载音频口出声；HDMI 音频主线内核不支持）

**换板型**：把上面的 DTB 文件名换成别的（如 `sun7i-a20-olinuxino-micro.dtb`、
`sun7i-a20-cubieboard2.dtb`）。启动不了时优先换这个。

### 步骤 8 · uInitrd（initramfs）

```bash
python scripts/build_initramfs.py
```

busybox + 启动关键模块（`sunxi-mmc`、`ext4` 依赖闭包 + `virtio_*` 供 QEMU 冒烟）+
init 脚本，cpio(newc) + gzip + uImage(GZIP) 封装。init 负责：加载模块 →
等 `/dev/mmcblk0p2` 出现 → `mount -t ext4`（ext4 驱动以 ext2 模式挂载）→
`switch_root /sysroot /sbin/init`。

### 步骤 9 · zImage

```bash
cp build/kernel/boot/vmlinuz-* build/boot/zImage
```

（vmlinuz 开头的 "MZ" 字样只是普通指令字节，`bootz` 直接可引导。）

### 步骤 10 · rootfs 树（核心步骤）

```bash
export M8_ROOTFS_DIR=D:/Projects/M8-lite/m8-build/build/rootfs2   # bash; PowerShell 用 $env:

python scripts/build_rootfs.py            # 全新目录：解包 282 个 deb
python scripts/build_rootfs.py --finish   # 已解包过：只重做模块元数据/核心/配置
```

脚本依次做：
1. **解包 282 个 deb** 到 rootfs 树。Windows 不能创建符号链接，deb 里的
   593 个 symlink 全部登记到 `build/rootfs.links`（`路径<TAB>目标`），
   由步骤 11 在 ext2 镜像内创建；
2. **模块树**：内核 deb 已自带，原地再生 `modules.dep/alias/symbols`，
   按 RUNTIME_MODULES（USB 栈/HID/DRM 显示/vfat/exfat/声卡/ext4/**USB 存储**）
   做依赖拓扑排序，生成 `/etc/modules.load` 供 rcS 用 busybox `insmod` 按序加载；
   HID 部分预载 20+ 个驱动：hid-generic（兜底，任意标准 HID 手柄即插即用）、
   hid-dr（DragonRise，廉价 2.4G 接收器主力）、hid-sunplus/xinmo/zpff/gaff/
   twinhan/emsff/sjoy/redragon、hid-sony/playstation/nintendo/microsoft/steam、
   joydev（/dev/input/jsX）、ff-memless + led-class-multicolor（上述驱动的依赖）；
3. **拷入 41 个核心 + .info 元数据 + 手柄 autoconfig 配置**（joypad-autoconfig 的
   udev/*.cfg，RetroArch 靠它给陌生手柄自动匹配按键）；
4. **写系统配置**：`/sbin/init`→busybox、`/etc/inittab`、`/etc/init.d/rcS`
   （挂载 /dev /proc /sys → 加载模块 → 注册 mdev 为内核 hotplug 助手
   （未预载模块按 MODALIAS 经 /sbin/modprobe 自动加载）→ 开机扫描并只读挂载
   U 盘到 /mnt/usb（拷 ROM 用）→ 推导并挂载 GAMES 分区 → 启动 RetroArch，
   **QUIT & POWER OFF**：RetroArch 退出后等 15 秒，有任意输入则重启、
   无输入则卸载文件系统并 `poweroff -f`；连续退出 3 次退 shell）、
   `/etc/fstab`、`/root/.config/retroarch/retroarch.cfg`
   （SDL2 fbdev 视频 + alsa 音频 + RGUI 菜单 + 手柄热键：
   Select+X 呼出菜单、Select+Start 关游戏、主菜单 Quit RetroArch 退出、
   **Select+R2/L2 即时存/读档、Select+R/L 换存档槽**——_btn 与传统无后缀
   两种绑定形式同时写入，savestate_directory 指向 GAMES 分区 /states）。

> 若目录非空，脚本会拒绝整树重解（防误删）——要么用 `--finish`，要么设
> `M8_ROOTFS_DIR` 指向新目录（或 `build_all.py --clean`）。

### 步骤 11 · 组装镜像（8G 约 1 分钟）

```bash
python scripts/assemble_image.py \
  --size 8G \
  --out build/m8-lite-a20.img \
  --rootfs D:/Projects/M8-lite/m8-build/build/rootfs2
```

四步：① 稀疏镜像 + MBR（p1 0x0C FAT32 / p2 0x83 ext2 / p3 0x0C FAT32）
+ 8KB 处写 U-Boot；② 手写 FAT32 格式化 BOOT 分区并写入 5 个启动文件；
③ 手写 ext2 格式化 ROOT 分区并打包 rootfs（打包时剔除
`usr/share/fonts` 342MB 和 `usr/share/libretro/assets/wallpapers` 97MB，
创建 /dev 静态节点和 593 个符号链接）；④ FAT32 格式化 GAMES 分区，
建 36 个机种 roms 目录 + README。

小卡适配：`--size 4G`（BOOT 128MB / ROOT 768MB），也可 `--boot-mb/--root-mb` 细调。

### 步骤 12 · 读回验证

```bash
python scripts/verify_image.py \
  build/m8-lite-a20.img \
  D:/Projects/M8-lite/m8-build/build/rootfs2
```

60+ 项独立校验：MBR/分区表/SPL 魔数；BOOT 五个文件逐一 SHA256 比对；
ext2 内 **12265 个文件全量遍历**（内容字节级比对）、602 个符号链接目标存在性、
权限位、/dev 设备节点；GAMES 分区目录结构。全部输出 `PASS` 才算构建成功。

---

## 6. 快速自检（可选）

修改过 `fat32.py` / `ext2.py` 后先跑单测（含磁盘重挂载回归，防止只验内存）：

```bash
python scripts/test_fat32.py
python scripts/test_ext2.py
```

---

## 7. WSL 验证（可选，覆盖不到真机硬件）

```bash
# WSL (Ubuntu) 中：
sudo apt update && sudo apt install -y qemu-system-arm dosfstools e2fsprogs util-linux
bash /mnt/d/Projects/M8-lite/m8-build/wsl-test.sh
```

依次：① `fsck` 只读校验三个分区（验证手写 FAT32/ext2 符合标准）；
② 列目录抽查启动文件；③ QEMU `virt` 机型冒烟启动，验证
「内核 → initramfs → 挂载根分区 → rcS」全链（看到 `[M8] starting RetroArch...` 即通）。
QEMU 没有 A20 机型，SPL/U-Boot/HDMI/手柄只能在真机验证。

---

## 8. 烧录

Rufus 或 BalenaEtcher，选 `build/m8-lite-a20.img`，TF 卡 ≥ 8GB，**DD 模式**。
烧录前先用 DiskGenius 整卡备份原卡（重点保留 boot 分区里的手柄配对文件）。

---

## 9. 定制指南

| 想改什么 | 改哪里 |
|---|---|
| 换板型 DTB | 步骤 7 的命令行参数（`build_all.py` 里改 `DTB_SRC` 常量） |
| 换 U-Boot 变体 | `assemble_image.py` 的 `UBOOT` 常量（deb 内另有 cubieboard2 / lamobo-r1 等 A20 变体） |
| 增删模拟器核心 | `fetch_cores.py` 顶部 `CORES` 列表 → 重跑步骤 4/10/11/12 |
| 分区大小 | `assemble_image.py --size / --boot-mb / --root-mb` |
| 内核版本 | 自动探测；`M8_KVER=6.1.0-xx-armmp` 强制 |
| 加载哪些模块 | `build_rootfs.py` 的 `RUNTIME_MODULES` 列表 |
| RetroArch 配置 | `build_rootfs.py` 的 `RETROARCH_CFG` |
| 启动参数 | `make_bootscr.py` 的 `BOOT_SCRIPT` |

---

## 10. 已知坑（构建时全部踩过）

1. **WSL 被沙箱黑名单拦截** → 才有这套全 Windows 原生管线（Python 手写 FAT32/ext2）。
2. **Windows 符号链接**：`tarfile filter="data"` 遇绝对路径 symlink 直接中止解包、
   相对 symlink 静默跳过 → 593 个链接全丢 → 改为清单登记 + 镜像内创建。
3. **FAT32 FAT 表写入偏移重复乘 bps**（FAT 写飞位置，内存态测试发现不了）
   → `test_fat32.py` 末尾补了「重新解析 BPB+FAT 再读回」的磁盘回归。
4. **目录项簇号 16 位截断**：`e[26]|(e[20]<<16)` 只取了低字节，簇号 >255 时串链。
5. **7zr 不能解 .zip** → 一律用 Python `zipfile`。
6. **`rm -rf` 被 safe-delete 钩子拦截**（>50 文件需确认）→ 用新目录名而非删除。
7. **boot.scr 不能含中文** → U-Boot 只解析 ASCII。
8. **Debian bookworm 没有 u-boot-sunxi** → 取 Ubuntu 20.04 ports 的 2021.01 版。

---

## 11. 产物清单

| 文件 | 大小 | 说明 |
|---|---|---|
| `build/m8-lite-a20.img` | 8GB（稀疏） | 最终镜像，烧录用 |
| `build/boot/boot.scr` | 1.1KB | U-Boot 启动脚本 |
| `build/boot/zImage` | 5.2MB | Linux 6.1 LTS (armhf) |
| `build/boot/board.dtb` | 24KB | mk808c + 补丁（USB host/声卡） |
| `build/boot/uInitrd` | 1.4MB | initramfs |
| `build/rootfs2/` | ~950MB | rootfs 树（含 41 核心 + 模块 + 配置） |
| `build/rootfs.links` | — | 593+ 条符号链接清单 |
