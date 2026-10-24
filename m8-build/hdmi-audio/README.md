# M8 Lite A20 —— HDMI 音频支持

## 背景

主线 Linux 的 `sun4i-drm-hdmi` 驱动（A10/A10s/A20/A31 的 HDMI 控制器）**只有视频，
没有音频**。原厂 Allwinner BSP 内核有 `sndhdmi` 驱动实现音画同口，但该代码从未
合入主线。Olimex 的 Stefan Mavrodiev 于 2020-01 向社区提交了移植补丁
（`[PATCH v3] drm: sun4i: hdmi: Add support for sun4i HDMI encoder audio`），
至今未合入，由 Armbian 以外挂补丁形式长期维护
（`patch/kernel/archive/sunxi-*/patches.armbian/drv-gpu-drm-sun4i-Add-HDMI-audio-sun4i-hdmi-encoder.patch`）。

硬件层面 A20 完全支持 HDMI 音频：主线设备树 `sun7i-a20.dtsi` 的 `hdmi@1c16000`
节点早就声明了 `dma-names = "ddc-tx","ddc-rx","audio-tx"`
（audio-tx = `SUN4I_DMA_DEDICATED` 专用 DMA 通道），本项目的 board.dtb 与主线
一致，**不需要改设备树**。差的只是驱动代码。

## 方案

补丁把音频代码（`sun4i_hdmi_audio.c`，450 行，ASoC 声卡寄生在 HDMI encoder 上）
编入 `sun4i-drm-hdmi.ko` **本身**（`sun4i-drm-hdmi-y += sun4i_hdmi_audio.o`），
并挂了 4 个钩子：

| 位置 | 作用 |
|---|---|
| `sun4i_hdmi_enc.c: sun4i_hdmi_get_modes()` | 读 EDID 时 `drm_detect_monitor_audio()` 判断显示器是否支持音频 |
| `sun4i_hdmi_enc.c: sun4i_hdmi_enable()` | 出画面（编码器使能）时注册 ASoC 声卡 `sun4i-hdmi` |
| `sun4i_hdmi_enc.c: sun4i_hdmi_disable()` | 关闭输出时注销声卡 |
| `sun4i_hdmi.h` | 音频寄存器定义（0x040~0x0ad）、`struct sun4i_hdmi_audio` |

因此**不用重编整个内核，但要用完整源码树编这一个模块**：把适配补丁打进
Debian 同版本内核源码树，交叉编译 `sun4i-drm-hdmi.ko`，产物 vermagic、符号
CRC 与原版完全一致，直接替换 `.ko`。

> **为什么必须用源码树而不是 Debian 的 linux-headers 包 out-of-tree 编？**
> Debian 的 `linux-kbuild` 只带按宿主架构预编译的 `modpost`，它对
> `devicetable-offsets` 的探测是 64 位的（`sizeof(of_device_id)=200`），而
> ARM 模块的设备表条目是 196 字节/条，`modpost` 的 `device_id_check` 会直接
> FATAL（内核上游该检查的已知缺陷：注释说"跨架构时忽略检查"但代码没实现；
> Debian 虽给 kbuild 打了 lsb/msb × 32/64 四变体分发补丁，external module
> 构建下仍不可靠）。用源码树时 `modpost` 从源码现场编译——`empty.o` /
> `devicetable-offsets.s` 交给 ARM 交叉编译器探测，`elfconfig.h=ELFCLASS32`、
> offsets=196，与模块完全匹配。这也是内核官方的交叉编译方式。
>
> **附带好处**：源码树保留在 WSL 本地盘，已打好补丁、配置与目标内核一致，
> 随时可以重编整个内核镜像（见 `build_hdmi_audio.sh` 头部的 `make Image
> modules dtbs` 示例命令）。

内核配置依赖（Debian `6.1.0-50-armmp` 均已满足，无需改配置）：
`CONFIG_DRM_SUN4I=m`、`CONFIG_DMA_SUN4I=y`、`CONFIG_SND=m`、`CONFIG_SND_SOC=m`、
`CONFIG_SND_PCM_ELD=y`、`CONFIG_SND_SOC_GENERIC_DMAENGINE_PCM=y`。

音频参数：仅 **S16_LE**，32/44.1/48/88.2/96/176.4/192 kHz，1~8 声道；
打开 PCM 时按显示器 ELD（EDID 的音频扩展块）约束参数——显示器不支持音频时
声卡根本不会注册。

## 文件

| 文件 | 说明 |
|---|---|
| `Add-HDMI-audio-sun4i-hdmi.patch` | Armbian sunxi-6.12 维护版原补丁（按 6.12 上下文，仅作溯源） |
| `sun4i-hdmi-audio-6.1.patch` | 适配 Debian linux 6.1.176 的补丁（脚本自动打进源码树；也可给完整内核树用） |
| `src-6.1/` | Debian 6.1.176 的 `drivers/gpu/drm/sun4i/` 原始源码（比对/再加工用） |
| `module/` | 早期 out-of-tree 方案的构建目录（11 个文件）。现已被"源码树 + external 构建"取代，仅作留存参考 |
| `extmod/` | 脚本每次运行自动生成：从打补丁的源码树拷 5 个 `.c` + 生成 Kbuild，`make M=extmod modules` 在此构建（勿手改，重跑即重建） |
| `downloads/` | 构建用的 deb：`linux-source-6.1`（源码）+ `linux-headers-<kver>-armmp`（取 `.config` 和 `Module.symvers`），脚本也会按需自行下载 |
| `build_hdmi_audio.sh` | **WSL 里执行**：下载/解压源码树、打补丁、交叉编译、自检、产出 `.ko` |
| `../scripts/install_hdmi_audio.py` | **Windows 里执行**：换 `.ko` + 重组装镜像 + 校验 |

## 使用

### 1. WSL 里编译（首次约 15~25 分钟，含 128MB 源码下载和依赖安装）

```bash
bash /mnt/d/Projects/M8-lite/m8-build/hdmi-audio/build_hdmi_audio.sh
```

脚本自动完成：

1. 从 `downloads/debs/linux-image-*-armmp_*.deb` 推断内核版本，下载
   `linux-source-6.1`（源码）+ `linux-headers-<kver>-armmp`（armhf，只解包取
   `.config` 和 `Module.symvers`）；
2. 把源码 tarball 解压到 **WSL 本地盘** `~/.cache/m8-lite/linux-<版本>`
   （约 1.1GB；不能放 `/mnt/d`，drvfs 上编译极慢），打上 HDMI 音频补丁；
3. 按需安装构建依赖（`build-essential`、`bc`、`flex`、`bison`、`libssl-dev`、
   `libelf-dev`、`crossbuild-essential-armhf`）；
4. 用目标内核的 `.config` 跑 `olddefconfig` + `modules_prepare`——
   重点是 **modpost 从源码现场编译**（32 位 ARM 配置，见上文说明）；
5. 从目标内核的 `UTS_RELEASE` 反推 `VERSION/PATCHLEVEL/SUBLEVEL/EXTRAVERSION/
   LOCALVERSION` 命令行注入（Debian 打包内核时也是这样注入 `-50-armmp` 的），
   以 **external（`M=`）方式**编译：脚本把打补丁后的源码树里 5 个 `.c` 拷进
   `extmod/`、生成 Kbuild（`-DCONFIG_DRM_SUN4I_HDMI_AUDIO=1` +
   `-I<源码树>/drivers/gpu/drm/sun4i`），再 `make M=extmod modules`；
6. 自检 **vermagic / `__versions` CRC 条目 / `depends`** 三项——任何一项不对
   加载都会失败——然后产出 `hdmi-audio/out/sun4i-drm-hdmi.ko`。

> 为什么是 external（`M=`）而不是 in-tree 单目标 `make .../sun4i-drm-hdmi.ko`：
> in-tree 单模块构建时 modpost 只从 `vmlinux.o` 取 CRC 和符号归属，没有 vmlinux
> 时 `__versions` 和 `depends` 双双为空（编译日志会警告 "vmlinux.o is missing"）。
> 目标内核带 `CONFIG_MODVERSIONS=y`，内核 `check_version()` 对"导出方有 CRC 而
> 模块没有对应条目"直接返回 0——模块根本加载不了（`no symbol version for xxx`）。
> external 模式下 kbuild 会给 modpost 传 `-i Module.symvers`（第 6 节从 flavour
> deb 取的那份），CRC 和 depends 都从它生成；实测该文件 CRC 与原版 `.ko` 的
> `__versions` 逐条一致。

源码树会保留（带 `.m8-debver` 版本标记），之后再跑脚本只重编模块，几秒完成。
内核小版本升级后脚本会提示手动删除旧源码树。

**重编整个内核镜像**（可选，源码树已具备全部条件）：

```bash
make -C ~/.cache/m8-lite/linux-6.1.176-1 -j$(nproc) ARCH=arm \
     CROSS_COMPILE=arm-linux-gnueabihf- DEBIAN_KERNEL_NO_CC_VERSION_CHECK=1 \
     VERSION=6 PATCHLEVEL=1 SUBLEVEL=0 EXTRAVERSION=-50 LOCALVERSION=-armmp \
     Image modules dtbs
```
（版本参数以脚本输出为准；`DEBIAN_KERNEL_NO_CC_VERSION_CHECK=1` 关闭 Debian
给源码打的编译器版本比对，交叉编译器名称与打包机不同必须关；
重编后用 `assemble_image.py` 打包即可，需要注意 U-Boot 与内核版本的匹配。）

### 2. Windows 里安装并重组装

```bash
python D:/Projects/M8-lite/m8-build/scripts/install_hdmi_audio.py
```

脚本自动：vermagic 比对 → 备份原版为 `sun4i-drm-hdmi.ko.stock` → 换 `.ko` →
`build_rootfs.py --finish` → `assemble_image.py --size 8G` → `verify_image.py`。
依赖闭包解析器会把 `snd-soc-core`/`snd` 等自动拉进预载清单。
检测到补丁版模块时自动生成 `/etc/asound.conf`（默认声卡 `sun4i-hdmi`，
板载模拟声卡在无 3.5mm 输出的机器上不可用）。

### 3. 烧录验证

```sh
cat /proc/asound/cards     # 应出现 sun4i-hdmi（接上带音频的显示器后）
aplay -l                   # alsa-utils 已随系统安装
```

启动日志里 `[M8] sound cards:` 会打印声卡清单。RetroArch 用
`audio_driver = alsathreaded` + ALSA default 设备，无需额外配置。

## 回退

```bash
python D:/Projects/M8-lite/m8-build/scripts/install_hdmi_audio.py --revert
```

恢复 `sun4i-drm-hdmi.ko.stock` 并重组装；`/etc/asound.conf` 会被自动删除。

## 已知限制

- **没有显示器（或显示器不支持音频）时 `sun4i-hdmi` 声卡不存在**：声卡是在
  DRM 编码器使能（出画面）时注册的。此时可删掉 `/etc/asound.conf` 回到默认
  声卡顺序，或改用 USB 声卡（`snd-usb-audio` 已预载）。
- 只支持 S16_LE；核心模拟器输出多为 16bit 44.1/48kHz，正好覆盖。
- 声道数 1~8；单声道时驱动会把同一样本发给左右声道。
- Armbian 在 6.x 上仍在持续 rebase 该补丁；将来 Debian 内核小版本升级后，
  删掉旧源码树（`rm -rf ~/.cache/m8-lite/linux-<旧版本>`）重跑脚本即可，
  补丁若与新版本上下文不符会由 `patch` 报错，按提示 rebase
  （`src-6.1/` 留作比对基线）。

## 来源与许可

- 原始补丁：Stefan Mavrodiev <stefan@olimex.com>，GPL-2.0，
  <https://patchwork.kernel.org/project/linux-arm-kernel/patch/20200128140642.8404-2-stefan@olimex.com/>
- 维护版：<https://github.com/armbian/build> →
  `patch/kernel/archive/sunxi-6.12/patches.armbian/drv-gpu-drm-sun4i-Add-HDMI-audio-sun4i-hdmi-encoder.patch`
- 6.1 适配：本项目（上下文逐 hunk 核对，改动点见 `sun4i-hdmi-audio-6.1.patch` 头部说明）
