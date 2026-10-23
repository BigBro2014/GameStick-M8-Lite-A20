#!/usr/bin/env python3
"""最小 FAT32 实现：在镜像文件内创建 FAT32 文件系统并写入文件/目录。
支持 LFN 长文件名。用于制作 u-boot 可识别的 BOOT 启动分区。"""
import struct, os, time, hashlib

EOC = 0x0FFFFFF8
BAD = 0x0FFFFFF7

def _dosdatetime(ts=None):
    t = time.localtime(ts or time.time())
    date = ((max(0, t.tm_year - 1980) & 0x7F) << 9) | ((t.tm_mon & 0x0F) << 5) | (t.tm_mday & 0x1F)
    tm = ((t.tm_hour & 0x1F) << 11) | ((t.tm_min & 0x3F) << 5) | ((t.tm_sec // 2) & 0x1F)
    return date, tm

class Fat32:
    def __init__(self, path, offset, size, label="BOOT", oem="mkfs.fat", max_spc=128):
        """offset: 分区在镜像文件中的字节偏移; size: 分区字节数
        max_spc: 每簇扇区数上限（GAMES 分区限制为 32，即 16KB 簇，避免小文件浪费）"""
        self.path = path
        self.offset = offset
        self.size = size
        self.label = label
        self.oem = oem
        self.bps = 512
        self.max_spc = max_spc
        self._calc_geometry()

    def _calc_geometry(self):
        """联合选择 spc/fat_size/clusters：FAT 开销本身会减少簇数，需迭代"""
        bps = self.bps
        total_sec = self.size // bps
        reserved, nfats = 32, 2
        chosen = None
        spc_list = [s for s in (128, 64, 32, 16, 8, 4, 2, 1) if s <= self.max_spc]
        for spc in spc_list:
            fat_size = 1
            while True:
                clusters = (total_sec - reserved - nfats * fat_size) // spc
                if clusters < 1:
                    break
                need_sec = ((clusters + 2) * 4 + bps - 1) // bps
                if need_sec <= fat_size:
                    break
                fat_size = need_sec
            clusters = (total_sec - reserved - nfats * fat_size) // spc
            if clusters >= 65525:
                chosen = (spc, fat_size, clusters)
                break
        if not chosen:
            raise RuntimeError("分区对 FAT32 太小（需 >= ~64MB）: %d bytes" % self.size)
        self.spc, self.fat_size, self.clusters = chosen

    # ---------- 底层 ----------
    def _open(self):
        if getattr(self, "_fh", None) is None:
            self._fh = open(self.path, "r+b")
        return self._fh

    def _w(self, pos, data):
        f = self._open()
        f.seek(self.offset + pos)
        f.write(data)

    def _r(self, pos, n):
        f = self._open()
        f.seek(self.offset + pos)
        return f.read(n)

    def close(self):
        self._write_fats()
        self._flush_fsinfo()
        if getattr(self, "_fh", None) is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    # ---------- 格式化 ----------
    def format(self):
        bps, spc = self.bps, self.spc
        total_sec = self.size // bps
        reserved = 32
        nfats = 2
        fat_size = self.fat_size
        self.reserved = reserved
        self.nfats = nfats
        self.total_sec = total_sec
        self.fat_start = reserved * bps
        self.data_start = (reserved + nfats * fat_size) * bps
        self.root_cluster = 2
        assert self.clusters >= 65525, "FAT32 needs >= 65525 clusters, got %d" % self.clusters

        # FAT 表（内存）
        self.fat = [0] * (self.clusters + 2)
        self.fat[0] = 0xFFFFFF00 | 0xF8
        self.fat[1] = EOC
        self.next_free = 3  # 2 是根目录
        self.root_chain = [2]
        self.fat[2] = EOC
        self.dirty = set(range((self.clusters + 2) * 4 // self.bps + 1))  # 初始全脏

        # BPB
        date, tm = _dosdatetime()
        bpb = bytearray(512)
        bpb[0:3] = b"\xEB\x3C\x90"
        bpb[3:11] = self.oem.encode().ljust(8)[:8]
        struct.pack_into("<H", bpb, 11, bps)
        bpb[13] = spc
        struct.pack_into("<H", bpb, 14, reserved)
        bpb[16] = nfats
        struct.pack_into("<H", bpb, 17, 0)          # root entries
        struct.pack_into("<H", bpb, 19, 0)          # total16
        bpb[21] = 0xF8
        struct.pack_into("<H", bpb, 22, 0)          # fatsz16
        struct.pack_into("<H", bpb, 24, 63)         # spt
        struct.pack_into("<H", bpb, 26, 255)        # heads
        struct.pack_into("<I", bpb, 28, self.offset // bps)  # hidden
        struct.pack_into("<I", bpb, 32, total_sec)
        struct.pack_into("<I", bpb, 36, fat_size)
        struct.pack_into("<H", bpb, 40, 0)          # extflags
        struct.pack_into("<H", bpb, 42, 0)          # fsver
        struct.pack_into("<I", bpb, 44, self.root_cluster)
        struct.pack_into("<H", bpb, 48, 1)          # fsinfo
        struct.pack_into("<H", bpb, 50, 6)          # backup boot
        bpb[64] = 0x80
        bpb[66] = 0x29
        struct.pack_into("<I", bpb, 67, 0x12345678)  # serial
        bpb[71:82] = self.label.encode().ljust(11)[:11]
        bpb[82:90] = b"FAT32   "
        bpb[510:512] = b"\x55\xAA"

        # FSInfo
        fsi = bytearray(512)
        struct.pack_into("<I", fsi, 0, 0x41615252)
        struct.pack_into("<I", fsi, 484, 0x61417272)
        struct.pack_into("<I", fsi, 488, self.clusters - 3)   # free count
        struct.pack_into("<I", fsi, 492, 3)                    # next free
        struct.pack_into("<I", fsi, 508, 0xAA550000)

        # 清零保留区 + FAT 区 + 根目录
        zero = bytes(bps)
        for s in range(0, reserved + nfats * fat_size + spc):
            self._w(s * bps, zero)

        self._w(0, bytes(bpb))
        self._w(bps, bytes(fsi))
        self._w(6 * bps, bytes(bpb))     # backup boot
        self._w(7 * bps, bytes(fsi))     # backup fsinfo
        self._write_fats()
        self._flush_fsinfo()

    def _touch(self, c):
        """标记 FAT 条目 c 所在扇区为脏"""
        sec = (c * 4) // self.bps
        self.dirty.add(sec)

    # ---------- 读取挂载 ----------
    def mount(self):
        """从磁盘解析 BPB + FAT，进入可读状态（不修改镜像）"""
        bpb = self._r(0, 512)
        if bpb[510:512] != b"\x55\xAA":
            raise RuntimeError("not a FAT filesystem at offset %d" % self.offset)
        self.bps = struct.unpack_from("<H", bpb, 11)[0]
        self.spc = bpb[13]
        reserved = struct.unpack_from("<H", bpb, 14)[0]
        self.nfats = bpb[16]
        total_sec = struct.unpack_from("<I", bpb, 32)[0]
        self.fat_size = struct.unpack_from("<I", bpb, 36)[0]
        self.reserved = reserved
        self.total_sec = total_sec
        self.root_cluster = struct.unpack_from("<I", bpb, 44)[0]
        self.fat_start = reserved * self.bps
        self.data_start = (reserved + self.nfats * self.fat_size) * self.bps
        self.clusters = (total_sec - reserved - self.nfats * self.fat_size) // self.spc
        # 读入 FAT
        fat_bytes = self._r(self.fat_start, self.fat_size * self.bps)
        self.fat = list(struct.unpack("<%dI" % (len(fat_bytes) // 4), fat_bytes))
        self.fat = [v & 0x0FFFFFFF for v in self.fat]
        self.root_chain = [self.root_cluster]
        while self.fat[self.root_chain[-1]] < EOC and self.fat[self.root_chain[-1]] >= 2:
            self.root_chain.append(self.fat[self.root_chain[-1]])
        self.next_free = 3
        self.dirty = set()
        return self

    def _write_fats(self):
        """仅写入脏扇区（增量）"""
        if not self.dirty:
            return
        per_sec = self.bps // 4
        for sec in sorted(self.dirty):
            start = sec * per_sec
            chunk = self.fat[start:start + per_sec]
            buf = struct.pack("<%dI" % len(chunk), *[v & 0x0FFFFFFF for v in chunk])
            for i in range(self.nfats):
                self._w(self.fat_start + sec * self.bps + i * self.fat_size * self.bps, buf)
        self.dirty.clear()

    def _flush_fsinfo(self):
        fsi = bytearray(self._r(self.bps, 512))
        # free_count 按 FAT 实际空表项统计（数据簇 2..clusters+1）——
        # 不能用格式化时的 clusters-3 常量，否则分配文件后 df/stat 显示不准
        free = self.fat[2:self.clusters + 2].count(0)
        struct.pack_into("<I", fsi, 488, free)
        struct.pack_into("<I", fsi, 492, self.next_free)
        self._w(self.bps, bytes(fsi))
        self._w(7 * self.bps, bytes(fsi))

    def _alloc(self):
        while self.next_free <= self.clusters and self.fat[self.next_free] != 0:
            self.next_free += 1
        if self.next_free > self.clusters:
            raise RuntimeError("FAT32 full")
        c = self.next_free
        self.fat[c] = EOC
        self._touch(c)
        self.next_free += 1
        # 清零新簇
        self._w(self.data_start + (c - 2) * self.spc * self.bps, bytes(self.spc * self.bps))
        return c

    def _chain_extend(self, chain):
        c = self._alloc()
        self.fat[chain[-1]] = c
        self._touch(chain[-1])
        chain.append(c)
        return c

    # ---------- 目录操作 ----------
    def _used_shorts(self, chain):
        """收集目录链中已使用的 8.3 短名（11 字节）"""
        used = set()
        for e in self._dir_entries(chain):
            if e[0] in (0x00, 0xE5):
                continue
            if e[11] == 0x0F:
                continue
            used.add(bytes(e[0:11]))
        return used

    def _short_name(self, name, chain):
        """生成唯一 8.3 短名（与目录内现有条目不冲突）"""
        used = self._used_shorts(chain)
        if "." in name:
            base_raw, _, ext_raw = name.upper().rpartition(".")
        else:
            base_raw, ext_raw = name.upper(), ""
        ext_raw = "".join(ch for ch in ext_raw if ch.isalnum() or ch in "_-~")[:3]
        base_raw = "".join(ch for ch in base_raw if ch.isalnum() or ch in "_-~")
        # 不能直接用原名作为短名的情况：超长 / 含小写 / 含非法字符
        fits = (len(base_raw) <= 8 and len(ext_raw) <= 3
                and name == name.upper()
                and name == (base_raw + ("." + ext_raw if ext_raw else "")))
        base = base_raw[:8] if fits else base_raw[:6]
        ext = ext_raw
        cand, n = base, 1
        while (cand.ljust(8) + ext.ljust(3)).encode("ascii") in used:
            tail = "~%d" % n
            cand = base[:8 - len(tail)] + tail
            n += 1
        return cand.ljust(8), ext.ljust(3)

    @staticmethod
    def _lfn_checksum(short11):
        s = 0
        for b in short11:
            s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
        return s

    def _dir_entries(self, chain):
        """读取目录链的所有 32 字节条目"""
        data = b""
        for c in chain:
            data += self._r(self.data_start + (c - 2) * self.spc * self.bps, self.spc * self.bps)
        return [data[i:i + 32] for i in range(0, len(data), 32)]

    @staticmethod
    def _entry_cluster(e):
        """从目录条目解析首簇号（offset 20=高16位, offset 26=低16位）"""
        return struct.unpack("<H", e[26:28])[0] | (struct.unpack("<H", e[20:22])[0] << 16)

    def _find_dir_chain(self, path):
        """按路径查找目录（不含首尾斜杠），返回簇链；支持 LFN 长名匹配"""
        chain = self.root_chain
        if not path:
            return chain
        for part in path.split("/"):
            want = part.upper()
            found = None
            entries = self._dir_entries(chain)
            for idx, e in enumerate(entries):
                if e[0] == 0x00:
                    break
                if e[11] == 0x0F:
                    continue
                if not (e[11] & 0x10):
                    continue
                nm = (e[0:8] + e[8:11]).decode("ascii", "replace").rstrip()
                long_name = self._decode_lfn(entries, idx)
                if want in (nm.upper(), (long_name or "").upper()):
                    found = e
                    break
            if not found:
                return None
            c = self._entry_cluster(found)
            if c == 0:
                return None
            chain = [c]
            while self.fat[chain[-1]] < EOC:
                chain.append(self.fat[chain[-1]])
        return chain

    def _dir_add_entry(self, chain, entry_bytes):
        """向目录链追加一个 32 字节条目，必要时扩展簇"""
        need_slot = None
        was_end = False
        last_in_cluster = False
        slots = self.spc * self.bps // 32
        for c in chain:
            for i in range(slots):
                pos = self.data_start + (c - 2) * self.spc * self.bps + i * 32
                e = self._r(pos, 32)
                if e[0] in (0x00, 0xE5):
                    need_slot = pos
                    was_end = (e[0] == 0x00)
                    last_in_cluster = (i == slots - 1)
                    break
            if need_slot is not None:
                break
        if need_slot is None:
            c = self._chain_extend(chain)
            need_slot = self.data_start + (c - 2) * self.spc * self.bps
            was_end = True
            last_in_cluster = True
        pos = need_slot
        self._w(pos, entry_bytes)
        # 若原槽位是 0x00（目录结束标记），需补一个 0x00 终止，防止目录不结束
        if was_end:
            if last_in_cluster:
                # 该簇已无空槽：若目录链还有后续簇，则清零下一簇首槽；否则扩展链
                if self.fat[chain[-1]] < EOC:
                    nxt = self.fat[chain[-1]]
                    self._w(self.data_start + (nxt - 2) * self.spc * self.bps, bytes(32))
                else:
                    c = self._chain_extend(chain)
                    self._w(self.data_start + (c - 2) * self.spc * self.bps, bytes(32))
            else:
                self._w(pos + 32, bytes(32))
        return pos

    def _make_entries(self, name, attr, cluster, size, chain):
        """生成短名 + LFN 条目列表（chain 为父目录链，用于短名去重）"""
        short_base, short_ext = self._short_name(name, chain)
        short11 = (short_base + short_ext).encode("ascii")
        # 判断是否需要 LFN（含小写/非8.3字符）
        needs_lfn = name != (short_base + ("." + short_ext if short_ext.strip() else "")).rstrip(".").rstrip() or any(ch.islower() for ch in name) or len(name) > 12
        entries = []
        if needs_lfn:
            uname = name.encode("utf-16-le")
            n = (len(uname) + 25) // 26
            chk = self._lfn_checksum(short11)
            padded = uname + b"\x00\x00"
            if len(padded) % 26:
                padded += b"\xFF" * (26 - len(padded) % 26)
            for seq in range(n, 0, -1):
                e = bytearray(32)
                # seq=N（带 0x40 标志）装文件名第一部分，seq=1 装最后部分
                chunk = padded[(n - seq) * 26: (n - seq + 1) * 26]
                e[0] = seq | (0x40 if seq == n else 0)
                e[1:11] = chunk[0:10]
                e[11] = 0x0F
                e[12] = 0
                e[13] = chk
                e[14:26] = chunk[10:22]
                e[26:28] = b"\x00\x00"
                e[28:32] = chunk[22:26]
                entries.append(bytes(e))
        date, tm = _dosdatetime()
        e = bytearray(32)
        e[0:11] = short11
        e[11] = attr
        e[13] = 0
        struct.pack_into("<H", e, 14, tm)
        struct.pack_into("<H", e, 16, date)
        struct.pack_into("<H", e, 18, date)
        struct.pack_into("<H", e, 20, (cluster >> 16) & 0xFFFF)
        struct.pack_into("<H", e, 22, tm)
        struct.pack_into("<H", e, 24, date)
        struct.pack_into("<H", e, 26, cluster & 0xFFFF)
        struct.pack_into("<I", e, 28, size)
        entries.append(bytes(e))
        return entries

    def mkdir(self, path):
        parent, _, name = path.rpartition("/")
        chain = self._find_dir_chain(parent)
        if chain is None:
            raise RuntimeError("parent not found: " + parent)
        c = self._alloc()
        # 初始化 . 和 .. 条目
        date, tm = _dosdatetime()
        dot = bytearray(32)
        dot[0:11] = b".          "
        dot[11] = 0x10
        struct.pack_into("<H", dot, 20, (c >> 16) & 0xFFFF)
        struct.pack_into("<H", dot, 26, c & 0xFFFF)
        dotdot = bytearray(32)
        dotdot[0:11] = b"..         "
        dotdot[11] = 0x10
        pclus = chain[0]
        struct.pack_into("<H", dotdot, 20, (pclus >> 16) & 0xFFFF)
        struct.pack_into("<H", dotdot, 26, pclus & 0xFFFF)
        self._w(self.data_start + (c - 2) * self.spc * self.bps, bytes(dot) + bytes(dotdot) + bytes(32))
        self.fat[c] = EOC
        self._touch(c)
        for e in self._make_entries(name, 0x10, c, 0, chain):
            self._dir_add_entry(chain, e)
        return c

    def write_file(self, fat_path, local_path=None, data=None):
        if data is None:
            with open(local_path, "rb") as f:
                data = f.read()
        parent, _, name = fat_path.rpartition("/")
        chain = self._find_dir_chain(parent)
        if chain is None:
            raise RuntimeError("dir not found: " + parent)
        # 分配数据链
        cluster_bytes = self.spc * self.bps
        need = max(1, (len(data) + cluster_bytes - 1) // cluster_bytes)
        first = self._alloc()
        dchain = [first]
        for _ in range(need - 1):
            dchain.append(self._alloc())
        for i in range(len(dchain) - 1):
            self.fat[dchain[i]] = dchain[i + 1]
            self._touch(dchain[i])
        self.fat[dchain[-1]] = EOC
        self._touch(dchain[-1])
        # 写数据
        padded = data + bytes(cluster_bytes * need - len(data))
        for i, c in enumerate(dchain):
            self._w(self.data_start + (c - 2) * cluster_bytes, padded[i * cluster_bytes:(i + 1) * cluster_bytes])
        for e in self._make_entries(name, 0x20, first, len(data), chain):
            self._dir_add_entry(chain, e)
        self._write_fats()
        self._flush_fsinfo()
        return len(data)

    # ---------- 验证 ----------
    @staticmethod
    def _decode_lfn(entries, idx):
        """从短名条目 idx 向前收集 LFN 条目并解码完整长名；无 LFN 返回 None"""
        parts = []
        i = idx - 1
        while i >= 0 and entries[i][11] == 0x0F:
            e = entries[i]
            chunk = e[1:11] + e[14:26] + e[28:32]
            s = chunk.decode("utf-16-le", "ignore")
            for term in ("\x00", "\uffff"):
                if term in s:
                    s = s[:s.index(term)]
            parts.append(s)
            i -= 1
        if not parts:
            return None
        return "".join(reversed(parts))

    def read_file(self, fat_path):
        fat_path = fat_path.replace("\\", "/").lstrip("/")
        dirn = fat_path.rpartition("/")[0] if "/" in fat_path else ""
        chain = self._find_dir_chain(dirn)
        if chain is None:
            return None
        target = fat_path.rpartition("/")[2].upper()
        size = None
        first = None
        entries = self._dir_entries(chain)
        for idx, e in enumerate(entries):
            if e[0] == 0x00:
                break
            if e[11] == 0x0F:
                continue
            nm = (e[0:8] + e[8:11]).decode("ascii", "replace").rstrip()
            long_name = self._decode_lfn(entries, idx)
            if target in (nm.upper(), (long_name or "").upper()):
                size = struct.unpack("<I", e[28:32])[0]
                first = self._entry_cluster(e)
                break
        if first is None:
            return None
        dchain = [first]
        while self.fat[dchain[-1]] < EOC:
            dchain.append(self.fat[dchain[-1]])
        out = b""
        for c in dchain:
            out += self._r(self.data_start + (c - 2) * self.spc * self.bps, self.spc * self.bps)
        return out[:size]

    def list_all(self):
        result = []
        def walk(chain, prefix):
            entries = self._dir_entries(chain)
            for idx, e in enumerate(entries):
                if e[0] == 0x00:
                    return
                if e[11] == 0x0F:
                    continue
                nm = (e[0:8] + e[8:11]).decode("ascii", "replace").rstrip()
                if nm in (".", ".."):
                    continue
                long_name = self._decode_lfn(entries, idx)
                c = self._entry_cluster(e)
                size = struct.unpack("<I", e[28:32])[0]
                p = prefix + "/" + (long_name or nm)
                if e[11] & 0x10:
                    result.append(("D", p, 0))
                    sub = [c]
                    while self.fat[sub[-1]] < EOC:
                        sub.append(self.fat[sub[-1]])
                    walk(sub, p)
                else:
                    result.append(("F", p, size))
        walk(self.root_chain, "")
        return result
