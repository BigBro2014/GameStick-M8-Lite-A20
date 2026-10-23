#!/usr/bin/env python3
"""DTB 编辑器：解析 Flattened Device Tree，修改后重新序列化。
用于给 M8 Lite 的 DTB 打补丁：
  1. /soc/usb@1c13000 dr_mode = "host"   （强制 OTG 口为主机模式，供 2.4G 手柄接收器）
  2. /soc/i2s@1c22000 status = "okay"    （A20 模拟音频 I2S，接 codec）
  3. 新增 /sound 节点（simple-audio-card: i2s1 + codec）
"""
import struct, sys

FDT_BEGIN_NODE = 1
FDT_END_NODE = 2
FDT_PROP = 3
FDT_END = 9


class Node:
    def __init__(self, name):
        self.name = name
        self.props = {}      # name -> bytes
        self.children = []   # [Node]

    def get(self, path):
        if path == "":
            return self
        cur = self
        for part in path.strip("/").split("/"):
            for c in cur.children:
                if c.name == part:
                    cur = c
                    break
            else:
                return None
        return cur

    def phandle_of(self, path):
        n = self.get(path)
        if n is None:
            return None
        v = n.props.get("linux,phandle") or n.props.get("phandle")
        if v and len(v) == 4:
            return struct.unpack(">I", v)[0]
        return None


def parse(data):
    assert data[:4] == b"\xd0\x0d\xfe\xed", "bad FDT magic"
    (totalsize, off_struct, off_strings, off_rsvmap, version,
     last_comp_version, boot_cpuid, size_strings, size_struct) = struct.unpack_from(">9I", data, 4)
    strings = data[off_strings:off_strings + size_strings]
    root = Node("")
    stack = [root]

    def sname(idx):
        end = strings.find(b"\x00", idx)
        return strings[idx:end].decode("ascii", "replace")

    p = off_struct
    end_struct = off_struct + size_struct
    first_node = True
    while p < end_struct:
        tok = struct.unpack_from(">I", data, p)[0]
        p += 4
        if tok == FDT_BEGIN_NODE:
            end = data.find(b"\x00", p)
            name = data[p:end].decode("ascii", "replace")
            p = (end + 4) & ~3
            if first_node:
                # 第一个 BEGIN_NODE 就是根节点本身
                first_node = False
                root.name = name
                continue
            n = Node(name)
            stack[-1].children.append(n)
            stack.append(n)
        elif tok == FDT_END_NODE:
            if len(stack) > 1:
                stack.pop()
        elif tok == FDT_PROP:
            plen, nidx = struct.unpack_from(">II", data, p)
            p += 8
            val = bytes(data[p:p + plen])
            p = (p + plen + 3) & ~3
            stack[-1].props[sname(nidx)] = val
        elif tok == FDT_END:
            break
    return root, version


def serialize(root, version=17, boot_cpuid=0):
    """重新序列化（保留 mem_rsvmap 为空表）"""
    # ---- strings block 收集 ----
    strings = bytearray()
    str_off = {}

    def sintern(s):
        b = s.encode("ascii") + b"\x00"
        if b in str_off:
            return str_off[b]
        off = len(strings)
        str_off[b] = off
        strings.extend(b)
        return off

    struct_blk = bytearray()

    def emit_node(n):
        nb = n.name.encode("ascii") + b"\x00"
        struct_blk.extend(struct.pack(">I", FDT_BEGIN_NODE))
        struct_blk.extend(nb)
        pad = (4 - len(struct_blk) % 4) % 4
        struct_blk.extend(b"\x00" * pad)
        for name in sorted(n.props.keys()):
            val = n.props[name]
            struct_blk.extend(struct.pack(">III", FDT_PROP, len(val), sintern(name)))
            struct_blk.extend(val)
            pad = (4 - len(val) % 4) % 4
            struct_blk.extend(b"\x00" * pad)
        for c in n.children:
            emit_node(c)
        struct_blk.extend(struct.pack(">I", FDT_END_NODE))

    emit_node(root)
    struct_blk.extend(struct.pack(">I", FDT_END))

    off_rsvmap = 40
    rsv = b"\x00" * 16
    off_struct = 40 + 16
    off_strings = off_struct + len(struct_blk)
    totalsize = off_strings + len(strings)

    hdr = struct.pack(">10I", 0xd00dfeed, totalsize, off_struct, off_strings,
                      off_rsvmap, version, version, boot_cpuid,
                      len(strings), len(struct_blk))
    return hdr + rsv + bytes(struct_blk) + bytes(strings)


def patch(src, dst, log=print):
    data = open(src, "rb").read()
    root, ver = parse(data)

    # 1. USB OTG → host 模式
    usb = root.get("/soc/usb@1c13000")
    if usb is not None:
        usb.props["dr_mode"] = b"host\x00"
        usb.props["status"] = b"okay\x00"
        log("  patched /soc/usb@1c13000 -> dr_mode=host")
    else:
        log("  WARN: /soc/usb@1c13000 not found")

    # 2. 启用 codec 与 i2s1（0x01C22000，A13/A20 codec 对应的 I2S）
    codec = root.get("/soc/codec@1c22c00")
    if codec is not None:
        codec.props["status"] = b"okay\x00"
        log("  patched /soc/codec@1c22c00 -> status=okay")
    i2s = root.get("/soc/i2s@1c22000")
    if i2s is not None:
        i2s.props["status"] = b"okay\x00"
        log("  patched /soc/i2s@1c22000 -> status=okay")

    # 3. 添加 /sound 简单声卡（自动分配 phandle）
    def max_phandle(n, acc=[0]):
        for k, v in n.props.items():
            if k in ("linux,phandle", "phandle") and len(v) == 4:
                acc[0] = max(acc[0], struct.unpack(">I", v)[0])
        for c in n.children:
            max_phandle(c, acc)
        return acc[0]

    def ensure_phandle(n):
        v = n.props.get("linux,phandle") or n.props.get("phandle")
        if v and len(v) == 4:
            return struct.unpack(">I", v)[0]
        ph = max_phandle(root) + 1
        n.props["linux,phandle"] = struct.pack(">I", ph)
        return ph

    i2s_ph = ensure_phandle(root.get("/soc/i2s@1c22000")) if root.get("/soc/i2s@1c22000") else None
    codec_ph = ensure_phandle(root.get("/soc/codec@1c22c00")) if root.get("/soc/codec@1c22c00") else None
    if i2s_ph and codec_ph and root.get("/sound") is None:
        snd = Node("sound")
        snd.props["compatible"] = b"simple-audio-card\x00"
        snd.props["simple-audio-card,name"] = b"M8-Lite\x00"
        snd.props["simple-audio-card,format"] = b"i2s\x00"
        cpu = Node("simple-audio-card,cpu")
        cpu.props["sound-dai"] = struct.pack(">I", i2s_ph)
        codec_dai = Node("simple-audio-card,codec")
        codec_dai.props["sound-dai"] = struct.pack(">I", codec_ph)
        snd.children.extend([cpu, codec_dai])
        root.children.append(snd)
        log("  added /sound (simple-audio-card: i2s1 + codec, ph=%d/%d)" % (i2s_ph, codec_ph))
    else:
        log("  WARN: sound node skipped (i2s_ph=%s codec_ph=%s exists=%s)"
            % (i2s_ph, codec_ph, root.get("/sound") is not None))

    out = serialize(root, ver)
    # 自校验：重新解析
    r2, _ = parse(out)
    assert r2.get("/soc/usb@1c13000") is not None, "roundtrip failed"
    assert r2.get("/sound") is not None, "roundtrip sound failed"
    open(dst, "wb").write(out)
    log("  wrote %s (%d bytes, was %d)" % (dst, len(out), len(data)))


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        patch(sys.argv[1], sys.argv[2])
    else:
        # 自检：往返一致性
        import glob, os
        src = "build/kernel/usr/lib/linux-image-6.1.0-50-armmp/sun7i-a20-mk808c.dtb"
        data = open(src, "rb").read()
        root, ver = parse(data)
        out = serialize(root, ver)
        r2, _ = parse(out)
        assert len(r2.children) == len(root.children)
        def walk(a, b, path=""):
            assert a.name == b.name
            assert set(a.props) == set(b.props), (path + "/" + a.name, set(a.props) ^ set(b.props))
            for k in a.props:
                assert a.props[k] == b.props[k], (path, k)
            assert len(a.children) == len(b.children), path
            for ca, cb in zip(a.children, b.children):
                walk(ca, cb, path + "/" + a.name)
        walk(root, r2)
        print("roundtrip OK: %d bytes -> %d bytes, tree identical" % (len(data), len(out)))
