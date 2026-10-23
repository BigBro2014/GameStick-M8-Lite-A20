#!/usr/bin/env python3
"""mkimage：生成 u-boot legacy uImage 格式文件（启动脚本 / RAMDISK）。
头部 64 字节，大端。hcrc = 头部CRC(hcrc字段置0)，dcrc = 数据CRC。"""
import struct, zlib, time

IH_MAGIC = 0x27051956
IH_OS_LINUX = 5
IH_ARCH_ARM = 2
IH_TYPE_KERNEL = 2
IH_TYPE_RAMDISK = 3
IH_TYPE_SCRIPT = 7
IH_COMP_NONE = 0
IH_COMP_GZIP = 1


def _crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def make_uimage(data, name, ih_type, load=0, ep=0, os=IH_OS_LINUX,
                arch=IH_ARCH_ARM, comp=IH_COMP_NONE):
    hdr = bytearray(64)
    struct.pack_into(">I", hdr, 0, IH_MAGIC)
    struct.pack_into(">I", hdr, 4, 0)            # hcrc 先置 0
    struct.pack_into(">I", hdr, 8, int(time.time()))
    struct.pack_into(">I", hdr, 12, len(data))   # ih_size
    struct.pack_into(">I", hdr, 16, load)        # ih_load
    struct.pack_into(">I", hdr, 20, ep)          # ih_ep
    struct.pack_into(">I", hdr, 24, _crc32(data))  # ih_dcrc
    hdr[28] = os
    hdr[29] = arch
    hdr[30] = ih_type
    hdr[31] = comp
    nm = name.encode("ascii", "replace")[:31]
    hdr[32:32 + len(nm)] = nm
    hcrc = _crc32(bytes(hdr))
    struct.pack_into(">I", hdr, 4, hcrc)
    return bytes(hdr) + data


def make_script(text, name="M8-Lite-A20"):
    if isinstance(text, str):
        text = text.encode("ascii")
    return make_uimage(text, name, IH_TYPE_SCRIPT)


def make_ramdisk(data, name="M8-Lite-initrd", comp=IH_COMP_GZIP):
    return make_uimage(data, name, IH_TYPE_RAMDISK, comp=comp)


if __name__ == "__main__":
    import sys
    # 自检：生成 boot.scr 并回读校验
    txt = b"echo hello\nbootz ${loadaddr}\n"
    scr = make_script(txt)
    assert struct.unpack(">I", scr[0:4])[0] == IH_MAGIC
    assert struct.unpack(">I", scr[12:16])[0] == len(txt)
    assert struct.unpack(">I", scr[24:28])[0] == _crc32(scr[64:])
    # hcrc 校验
    hdr = bytearray(scr[:64])
    struct.pack_into(">I", hdr, 4, 0)
    assert struct.unpack(">I", scr[4:8])[0] == _crc32(bytes(hdr))
    print("mkimage self-test OK, script size", len(scr))
