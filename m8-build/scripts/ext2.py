#!/usr/bin/env python3
"""最小 ext2 写入器：在镜像文件内创建 ext2 文件系统并写入目录树。
支持：目录（多块）、普通文件（直接+一级+二级间接块）、快速/慢速符号链接、
特殊文件（设备/FIFO）、权限位、稀疏超级块备份。
目标：由 Linux ext4.ko 以 ext2 兼容模式挂载。"""
import struct, os, time, stat

EXT2_SUPER_MAGIC = 0xEF53
SPARSE_SUPER = 0x0001       # ro_compat
INCOMPAT_FILETYPE = 0x0002  # incompat

FT_REG_FILE = 1
FT_DIR = 2
FT_CHRDEV = 3
FT_BLKDEV = 4
FT_FIFO = 5
FT_SOCK = 6
FT_SYMLINK = 7

EXT2_ROOT_INO = 2
LOST_FOUND_INO = 11
EXT2_FIRST_INO = 11


class Ext2:
    def __init__(self, path, offset, size, block_size=1024, label="ROOT",
                 bytes_per_inode=8192):
        assert block_size == 1024, "本实现仅支持 1024 字节块"
        self.path = path
        self.offset = offset
        self.size = size
        self.bs = block_size
        self.label = label
        self.blocks_total = size // block_size
        self.bpg = 8 * block_size
        self.groups = (self.blocks_total - 1 + self.bpg - 1) // self.bpg
        self.inodes_total = ((size // bytes_per_inode + self.groups - 1) // self.groups) * self.groups
        self.ipg = self.inodes_total // self.groups
        self.gdt_blocks = (self.groups * 32 + block_size - 1) // block_size
        self.itb = (self.ipg * 128 + block_size - 1) // block_size
        self._fh = None

    # ---------- 底层 IO ----------
    def _open(self):
        if self._fh is None:
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
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    # ---------- 布局 ----------
    # 注意：仅支持 1024 字节块（first_data_block=1，组0覆盖块1..bpg，块0为引导区）
    def _is_backup(self, g):
        if g <= 1:
            return True
        for p in (3, 5, 7):
            q = p
            while q < g:
                q *= p
            if q == g:
                return True
        return False

    def _group_layout(self, g):
        """返回 (first_block, has_sb, block_bitmap_blk, inode_bitmap_blk, inode_table_blk)
        组 g 覆盖块 [first, first+bpg-1]；1K 块时组0从块1开始"""
        first = 1 + g * self.bpg
        if self._is_backup(g):
            base = 1 + self.gdt_blocks          # 超级块占1块 + GDT
            return first, True, first + base, first + base + 1, first + base + 2
        return first, False, first, first + 1, first + 2

    # ---------- 初始化 ----------
    def format(self):
        bs = self.bs
        self.now = int(time.time())
        self.block_bitmap = bytearray((self.blocks_total + 7) // 8)
        self.inode_bitmap = bytearray((self.inodes_total + 8) // 8)
        self.inodes = [None] * (self.inodes_total + 1)
        self._dir_entries = {}       # ino -> [(ino, name, ftype)]
        self._dir_blocks = {}        # ino -> [block numbers]
        self.next_inode = EXT2_FIRST_INO + 1
        self.next_block_hint = None

        def mark_block(b):
            self.block_bitmap[b // 8] |= 1 << (b % 8)

        def mark_inode(i):
            self.inode_bitmap[i // 8] |= 1 << (i % 8)

        # 预标记所有元数据块
        for g in range(self.groups):
            first, has_sb, bbit, ibit, itab = self._group_layout(g)
            n_meta = self.itb + 2 + ((1 + self.gdt_blocks) if has_sb else 0)
            for b in range(first, min(first + n_meta, self.blocks_total)):
                mark_block(b)
        # 预标记保留 inode 1..11
        for i in range(1, EXT2_FIRST_INO + 1):
            mark_inode(i)

        # 根目录与 lost+found
        for ino, mode in ((EXT2_ROOT_INO, 0o040755), (LOST_FOUND_INO, 0o040700)):
            self.inodes[ino] = {
                "mode": mode, "uid": 0, "gid": 0, "size": 0,
                "atime": self.now, "ctime": self.now, "mtime": self.now,
                "links": 2, "i_blocks": 0, "flags": 0, "symlink": None,
                "rdev": None,
            }
        self._dir_entries[EXT2_ROOT_INO] = [
            (EXT2_ROOT_INO, ".", FT_DIR), (EXT2_ROOT_INO, "..", FT_DIR),
            (LOST_FOUND_INO, "lost+found", FT_DIR),
        ]
        self._dir_entries[LOST_FOUND_INO] = [
            (LOST_FOUND_INO, ".", FT_DIR), (EXT2_ROOT_INO, "..", FT_DIR),
        ]
        # lost+found 预分配 16KB
        lf_blocks = [self._alloc_block() for _ in range(16 * 1024 // bs)]
        self._dir_blocks[LOST_FOUND_INO] = lf_blocks
        self._dir_blocks[EXT2_ROOT_INO] = [self._alloc_block()]
        self.inodes[LOST_FOUND_INO]["size"] = 16 * 1024
        self.inodes[LOST_FOUND_INO]["i_blocks"] = len(lf_blocks) * bs // 512
        self.inodes[EXT2_ROOT_INO]["size"] = bs
        self.inodes[EXT2_ROOT_INO]["i_blocks"] = bs // 512
        return self

    # ---------- 分配 ----------
    def _alloc_block(self):
        if self.next_block_hint is None:
            first, has_sb, bbit, ibit, itab = self._group_layout(0)
            self.next_block_hint = itab + self.itb
        while self.next_block_hint < self.blocks_total:
            b = self.next_block_hint
            self.next_block_hint += 1
            if b < 1:
                continue
            if not (self.block_bitmap[b // 8] >> (b % 8)) & 1:
                self.block_bitmap[b // 8] |= 1 << (b % 8)
                return b
        raise RuntimeError("ext2: out of blocks")

    def _alloc_inode(self, mode):
        while self.next_inode <= self.inodes_total:
            i = self.next_inode
            self.next_inode += 1
            if not (self.inode_bitmap[i // 8] >> (i % 8)) & 1:
                self.inode_bitmap[i // 8] |= 1 << (i % 8)
                self.inodes[i] = {
                    "mode": mode, "uid": 0, "gid": 0, "size": 0,
                    "atime": self.now, "ctime": self.now, "mtime": self.now,
                    "links": 1, "i_blocks": 0, "flags": 0, "symlink": None,
                    "rdev": None,
                }
                return i
        raise RuntimeError("ext2: out of inodes")

    # ---------- 目录操作 ----------
    def add_dir(self, parent_ino, name, mode=0o040755):
        ino = self._alloc_inode(mode)
        self.inodes[ino]["links"] = 2
        self._dir_entries[ino] = [(ino, ".", FT_DIR), (parent_ino, "..", FT_DIR)]
        self._dir_blocks[ino] = []
        self._dir_entries[parent_ino].append((ino, name, FT_DIR))
        self.inodes[parent_ino]["links"] += 1
        return ino

    def _link(self, parent_ino, ino, name, ftype):
        self._dir_entries[parent_ino].append((ino, name, ftype))

    # ---------- 文件操作 ----------
    def add_data(self, parent_ino, name, data, mode=0o100644):
        ino = self._alloc_inode(mode)
        rec = self.inodes[ino]
        rec["size"] = len(data)
        nblocks = (len(data) + self.bs - 1) // self.bs
        blocks = [self._alloc_block() for _ in range(nblocks)]
        rec["blocks"] = blocks
        rec["i_blocks"] = nblocks * self.bs // 512
        for i, b in enumerate(blocks):
            self._w(b * self.bs, data[i * self.bs:(i + 1) * self.bs].ljust(self.bs, b"\x00"))
        self._link(parent_ino, ino, name, FT_REG_FILE)
        return ino

    def add_file(self, parent_ino, name, local_path, mode=0o100644):
        with open(local_path, "rb") as f:
            return self.add_data(parent_ino, name, f.read(), mode)

    def add_symlink(self, parent_ino, name, target):
        ino = self._alloc_inode(0o120777)
        tb = target.encode("utf-8") if isinstance(target, str) else target
        if len(tb) <= 60:
            self.inodes[ino]["symlink"] = tb
            self.inodes[ino]["size"] = len(tb)
            self.inodes[ino]["i_blocks"] = 0
        else:
            b = self._alloc_block()
            self._w(b * self.bs, tb.ljust(self.bs, b"\x00"))
            self.inodes[ino]["blocks"] = [b]
            self.inodes[ino]["size"] = len(tb)
            self.inodes[ino]["i_blocks"] = self.bs // 512
        self._link(parent_ino, ino, name, FT_SYMLINK)
        return ino

    def add_special(self, parent_ino, name, mode, rdev=0):
        ino = self._alloc_inode(mode)
        self.inodes[ino]["rdev"] = rdev
        if stat.S_ISCHR(mode):
            ftype = FT_CHRDEV
        elif stat.S_ISBLK(mode):
            ftype = FT_BLKDEV
        else:
            ftype = FT_FIFO
        self._link(parent_ino, ino, name, ftype)
        return ino

    # ---------- 目录打包 ----------
    def _pack_dir_blocks(self, entries):
        """把目录条目打包成若干完整块；每块最后一条的 rec_len 延伸到块尾"""
        blocks = []
        cur = bytearray()
        for ino, name, ftype in entries:
            nm = name.encode("utf-8") if isinstance(name, str) else name
            need = 8 + len(nm)
            need = (need + 3) // 4 * 4
            if need < 12:
                need = 12
            if len(cur) + need > self.bs:
                # 当前块收尾：补齐并扩展最后一条 rec_len 至块尾
                cur += bytes(self.bs - len(cur))
                self._extend_last(cur)
                blocks.append(bytes(cur))
                cur = bytearray()
            cur += struct.pack("<IHBB", ino, need, len(nm), ftype) + nm
            cur += bytes(need - 8 - len(nm))
        if not cur:
            cur = bytearray()
        cur += bytes(self.bs - len(cur))
        self._extend_last(cur)
        blocks.append(bytes(cur))
        return blocks

    def _extend_last(self, block):
        """block 必须是恰好 self.bs 字节；把最后一条目录条目的 rec_len 延伸到块尾"""
        off = 0
        last = None
        while off + 8 <= len(block):
            ino, rec, nlen, ftype = struct.unpack_from("<IHBB", block, off)
            if rec < 12 or off + rec > len(block):
                break
            last = off
            off += rec
        if last is not None:
            struct.pack_into("<H", block, last + 4, len(block) - last)

    # ---------- 间接块 ----------
    def _write_indirect(self, ptrs):
        blk = self._alloc_block()
        buf = bytearray(self.bs)
        for i, p in enumerate(ptrs):
            struct.pack_into("<I", buf, i * 4, p)
        self._w(blk * self.bs, bytes(buf))
        return blk

    def _map_blocks(self, blocks):
        """返回 (direct, ind1, ind2)"""
        direct = blocks[:12]
        rest = blocks[12:]
        ind1 = ind2 = None
        per = self.bs // 4
        if rest:
            ind1 = self._write_indirect(rest[:per])
            rest = rest[per:]
        if rest:
            dbl = self._alloc_block()
            buf = bytearray(self.bs)
            for i in range(0, len(rest), per):
                sub = self._write_indirect(rest[i:i + per])
                struct.pack_into("<I", buf, (i // per) * 4, sub)
            self._w(dbl * self.bs, bytes(buf))
            ind2 = dbl
        return direct, ind1, ind2

    # ---------- 元数据落盘 ----------
    def flush(self):
        bs = self.bs
        now = self.now
        # ---- 目录数据块 ----
        for ino, entries in self._dir_entries.items():
            blocks = self._pack_dir_blocks(entries)
            need = len(blocks)
            have = self._dir_blocks.get(ino, [])
            while len(have) < need:
                have.append(self._alloc_block())
            self._dir_blocks[ino] = have   # 保留预分配块（如 lost+found 的 16KB）
            self.inodes[ino]["blocks"] = have
            for i, b in enumerate(have[:need]):
                self._w(b * bs, blocks[i])
            self.inodes[ino]["size"] = len(have) * bs
            self.inodes[ino]["i_blocks"] = len(have) * bs // 512

        # ---- inode 表 ----
        raw = bytearray(self.groups * self.ipg * 128)
        for ino in range(1, self.inodes_total + 1):
            rec = self.inodes[ino]
            if rec is None:
                continue
            off = (ino - 1) * 128
            struct.pack_into("<H", raw, off + 0, rec["mode"])
            struct.pack_into("<H", raw, off + 2, rec["uid"])
            struct.pack_into("<I", raw, off + 4, rec["size"] & 0xFFFFFFFF)
            struct.pack_into("<I", raw, off + 8, rec["atime"])
            struct.pack_into("<I", raw, off + 12, rec["ctime"])
            struct.pack_into("<I", raw, off + 16, rec["mtime"])
            struct.pack_into("<I", raw, off + 20, 0)  # dtime
            struct.pack_into("<H", raw, off + 24, rec["gid"])
            struct.pack_into("<H", raw, off + 26, rec["links"])
            struct.pack_into("<I", raw, off + 28, rec["i_blocks"])
            struct.pack_into("<I", raw, off + 32, rec["flags"])
            if rec["symlink"]:
                raw[off + 40:off + 40 + len(rec["symlink"])] = rec["symlink"]
            elif rec["rdev"] is not None:
                struct.pack_into("<I", raw, off + 40, rec["rdev"])
            elif "blocks" in rec and rec["blocks"]:
                direct, ind1, ind2 = self._map_blocks(rec["blocks"])
                for i, b in enumerate(direct):
                    struct.pack_into("<I", raw, off + 40 + i * 4, b)
                if ind1:
                    struct.pack_into("<I", raw, off + 40 + 12 * 4, ind1)
                if ind2:
                    struct.pack_into("<I", raw, off + 40 + 13 * 4, ind2)
        for g in range(self.groups):
            first, has_sb, bbit, ibit, itab = self._group_layout(g)
            chunk = raw[g * self.ipg * 128:(g + 1) * self.ipg * 128]
            self._w(itab * bs, bytes(chunk))

        # ---- 各组位图 ----
        for g in range(self.groups):
            first, has_sb, bbit, ibit, itab = self._group_layout(g)
            bb = bytearray(bs)
            for j in range(self.bpg):
                b = first + j
                if b >= self.blocks_total:
                    break
                if (self.block_bitmap[b // 8] >> (b % 8)) & 1:
                    bb[j // 8] |= 1 << (j % 8)
            self._w(bbit * bs, bytes(bb))
            ib = bytearray(bs)
            for j in range(self.ipg):
                i = g * self.ipg + j + 1
                if i > self.inodes_total:
                    break
                if (self.inode_bitmap[i // 8] >> (i % 8)) & 1:
                    ib[j // 8] |= 1 << (j % 8)
            self._w(ibit * bs, bytes(ib))

        # ---- 块组描述符 ----
        gdt = bytearray()
        for g in range(self.groups):
            first, has_sb, bbit, ibit, itab = self._group_layout(g)
            used = free_dirs = 0
            total_g = min(self.bpg, self.blocks_total - first)
            for j in range(total_g):
                b = first + j
                if (self.block_bitmap[b // 8] >> (b % 8)) & 1:
                    used += 1
            free_blocks = total_g - used
            free_inodes = self.ipg
            for j in range(self.ipg):
                i = g * self.ipg + j + 1
                if i > self.inodes_total:
                    free_inodes -= 1
                    continue
                if (self.inode_bitmap[i // 8] >> (i % 8)) & 1:
                    free_inodes -= 1
                elif self.inodes[i] is not None and (self.inodes[i]["mode"] & 0xF000) == 0x4000:
                    free_dirs += 1
            # ext2 块组描述符固定 32 字节（含保留/校验字段，留空）
            gdt += struct.pack("<IIIIHHHH", bbit, ibit, itab, free_blocks,
                               free_inodes, free_dirs, 0, 0)
            gdt += bytes(8)    # bg_exclude_bitmap_lo(4) + 校验字段(4) 留空，凑满 32 字节

        # ---- 超级块 ----
        sb = bytearray(1024)
        free_blocks_total = self.blocks_total - sum(
            1 for b in range(self.blocks_total) if (self.block_bitmap[b // 8] >> (b % 8)) & 1)
        free_inodes_total = sum(
            1 for i in range(1, self.inodes_total + 1)
            if not (self.inode_bitmap[i // 8] >> (i % 8)) & 1)
        struct.pack_into("<I", sb, 0, self.inodes_total)
        struct.pack_into("<I", sb, 4, self.blocks_total)
        struct.pack_into("<I", sb, 8, 0)
        struct.pack_into("<I", sb, 12, free_blocks_total)
        struct.pack_into("<I", sb, 16, free_inodes_total)
        struct.pack_into("<I", sb, 20, 1)
        struct.pack_into("<i", sb, 24, {1024: 0, 2048: 1, 4096: 2}[bs])
        struct.pack_into("<i", sb, 28, {1024: 0, 2048: 1, 4096: 2}[bs])
        struct.pack_into("<I", sb, 32, self.bpg)
        struct.pack_into("<I", sb, 36, self.bpg)
        struct.pack_into("<I", sb, 40, self.ipg)
        struct.pack_into("<I", sb, 44, now)
        struct.pack_into("<I", sb, 48, now)
        struct.pack_into("<H", sb, 52, 1)
        struct.pack_into("<H", sb, 54, 0xFFFF)
        struct.pack_into("<H", sb, 56, EXT2_SUPER_MAGIC)
        struct.pack_into("<H", sb, 58, 1)   # state: valid
        struct.pack_into("<H", sb, 60, 1)   # errors: continue
        struct.pack_into("<H", sb, 62, 0)   # minor rev
        struct.pack_into("<I", sb, 64, now)
        struct.pack_into("<I", sb, 68, 0)
        struct.pack_into("<I", sb, 72, 0)   # creator os: Linux
        struct.pack_into("<I", sb, 76, 1)   # rev level
        struct.pack_into("<H", sb, 80, 0)
        struct.pack_into("<H", sb, 82, 0)
        struct.pack_into("<I", sb, 84, EXT2_FIRST_INO)
        struct.pack_into("<H", sb, 88, 128)
        struct.pack_into("<H", sb, 90, 0)   # block_group_nr (主)
        struct.pack_into("<I", sb, 92, 0)   # feature compat
        struct.pack_into("<I", sb, 96, INCOMPAT_FILETYPE)
        struct.pack_into("<I", sb, 100, SPARSE_SUPER)
        sb[104:120] = bytes(range(16))      # uuid（任意固定值）
        sb[120:136] = self.label.encode().ljust(16, b"\x00")[:16]
        # last_mounted @136 (64B) 留空

        block0 = bytearray(bs)
        block0[1024:2048] = sb
        self._w(0, bytes(block0))
        # 1K 块：主超级块在块1（字节1024），GDT 从块2 开始
        self._w(2 * bs, bytes(gdt))
        for g in range(1, self.groups):
            if self._is_backup(g):
                first, has_sb, bbit, ibit, itab = self._group_layout(g)
                sb_bak = bytearray(sb)
                struct.pack_into("<H", sb_bak, 90, g)
                self._w(first * bs, bytes(sb_bak))
                self._w((first + 1) * bs, bytes(gdt))
