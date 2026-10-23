#!/usr/bin/env python3
"""Debian 包工具：解析 Packages 索引、递归依赖解析、deb 提取、带重试的下载器。"""
import lzma, os, re, shutil, subprocess, sys, tarfile, tempfile, time, urllib.request, urllib.error

UA = {"User-Agent": "m8-image-builder/1.0"}

# ---------- HTTP ----------
def http_get(url, dest=None, retries=5, timeout=120, min_size=0):
    """下载 url 到 dest（文件路径），支持断点续传。返回 dest。"""
    if dest is None:
        dest = tempfile.mktemp()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=dict(UA, Range="bytes=%d-" % (os.path.getsize(dest) if os.path.exists(dest) else 0)))
            with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "ab") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            if os.path.getsize(dest) >= min_size:
                return dest
        except Exception as e:
            sys.stderr.write("  [retry %d] %s: %s\n" % (attempt + 1, url, e))
            time.sleep(2 + attempt * 2)
    raise RuntimeError("download failed: " + url)

# ---------- Packages 索引 ----------
def parse_packages(path):
    """解析 Debian control 格式索引 -> {package_name: stanza_dict}"""
    pkgs = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    for stanza in text.split("\n\n"):
        stanza = stanza.strip("\n")
        if not stanza:
            continue
        fields = {}
        key = None
        for line in stanza.split("\n"):
            if line.startswith((" ", "\t")) and key:
                fields[key] += " " + line.strip()
            elif ":" in line:
                key, _, val = line.partition(":")
                key = key.strip()
                fields[key] = val.strip()
        name = fields.get("Package")
        if name:
            pkgs[name] = fields
    return pkgs

def _split_depends(field):
    """'a (>= 1), b | c, d:any' -> [[('a',''),('b',''),('c','')], [('d','any')]]"""
    groups = []
    for alt in field.split(","):
        alt = alt.strip()
        if not alt:
            continue
        members = []
        for item in alt.split("|"):
            item = item.strip()
            m = re.match(r"^([a-zA-Z0-9.+-]+)(?::([a-zA-Z0-9]+))?(?:\s*\(([^)]*)\))?$", item)
            if m:
                members.append((m.group(1), m.group(2) or ""))
        if members:
            groups.append(members)
    return groups

def resolve(pkgs, targets, verbose=True):
    """递归解析 Depends/Pre-Depends 闭包。返回 {name: stanza}"""
    resolved = {}
    queue = list(targets)
    while queue:
        name = queue.pop(0)
        if name in resolved:
            continue
        st = pkgs.get(name)
        if st is None:
            # 虚拟包：找 Provides 它的真实包
            provider = None
            for pn, ps in pkgs.items():
                prov = ps.get("Provides", "")
                for pv in prov.split(","):
                    pv = pv.strip().split()[0] if pv.strip() else ""
                    if pv == name:
                        provider = pn
                        break
                if provider:
                    break
            if provider:
                queue.insert(0, provider)
                resolved[name] = None  # 标记为虚拟，已替换
                continue
            sys.stderr.write("[warn] package not found in index: %s (assumed preinstalled/essential)\n" % name)
            resolved[name] = None
            continue
        resolved[name] = st
        deps = st.get("Pre-Depends", "") + ", " + st.get("Depends", "")
        for group in _split_depends(deps):
            # 选组内第一个能解析的
            for cand, archq in group:
                if cand in pkgs:
                    queue.append(cand)
                    break
            else:
                sys.stderr.write("[warn] unresolved dep group in %s: %s\n" % (name, group))
        if verbose:
            print("  resolved %-40s %s" % (name, st.get("Version", "")))
    return {k: v for k, v in resolved.items() if v is not None}

# ---------- deb 提取 ----------
def extract_deb(deb_path, dest, links_out=None):
    """解包 .deb（ar -> data.tar.{xz,gz,zst}）到 dest 目录。

    Windows 无符号链接权限：符号链接不落盘，改为写入清单（每行 "path\ttarget"），
    由 ext2 镜像构建器在镜像内创建。links_out 为已打开的文件句柄（追加模式）。
    逐成员提取并隔离错误：单个成员失败不影响其余文件。
    """
    import io
    with open(deb_path, "rb") as f:
        magic = f.read(8)
        if magic != b"!<arch>\n":
            raise RuntimeError("not a deb (ar): " + deb_path)
        members = {}
        while True:
            hdr = f.read(60)
            if len(hdr) < 60:
                break
            name = hdr[0:16].decode().strip()
            size = int(hdr[48:58].decode().strip())
            data = f.read(size)
            if size % 2:
                f.read(1)
            members[name] = data
    data = None
    for key in ("data.tar.xz", "data.tar.gz", "data.tar.zst", "data.tar"):
        if key in members:
            data = members[key]
            comp = key.split(".")[-1]
            break
    if data is None:
        raise RuntimeError("no data.tar.* in " + deb_path)
    if comp == "xz":
        import lzma
        data = lzma.decompress(data)
    elif comp == "gz":
        import gzip
        data = gzip.decompress(data)
    elif comp == "zst":
        # 尝试 zstandard，没有则报错
        try:
            import zstandard
            data = zstandard.ZstdDecompressor().decompress(data, max_output_size=1 << 30)
        except ImportError:
            raise RuntimeError("zstd deb not supported (pip install zstandard): " + deb_path)

    def norm(name):
        # "./usr/bin/foo" / "/usr/bin/foo" -> "usr/bin/foo"
        n = name.replace("\\", "/")
        while n.startswith("./"):
            n = n[2:]
        return n.lstrip("/")

    links = 0
    skipped = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        for m in tf.getmembers():
            if m.issym():
                if links_out is not None:
                    tgt = m.linkname.replace("\\", "/")
                    links_out.write("%s\t%s\n" % (norm(m.name), tgt))
                    links += 1
                continue
            if m.isdir():
                try:
                    tf.extract(m, dest, filter="data")
                except Exception:
                    pass
                continue
            try:
                tf.extract(m, dest, filter="data")
            except Exception:
                # 硬链接在 Windows 上可能失败：退化为复制目标文件内容
                if m.islnk():
                    try:
                        src = os.path.join(dest, norm(m.linkname))
                        dst = os.path.join(dest, norm(m.name))
                        if os.path.isfile(src):
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copyfile(src, dst)
                            continue
                    except Exception:
                        pass
                skipped += 1
    if skipped:
        sys.stderr.write("  [warn] %s: %d members skipped\n" % (os.path.basename(deb_path), skipped))
    return links

def deb_info(deb_path):
    """读取 deb 的 control 信息"""
    import io
    with open(deb_path, "rb") as f:
        f.read(8)
        while True:
            hdr = f.read(60)
            if len(hdr) < 60:
                return {}
            name = hdr[0:16].decode().strip()
            size = int(hdr[48:58].decode().strip())
            data = f.read(size)
            if size % 2:
                f.read(1)
            if name.startswith("control.tar"):
                if name.endswith(".gz"):
                    import gzip
                    data = gzip.decompress(data)
                elif name.endswith(".xz"):
                    import lzma
                    data = lzma.decompress(data)
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
                    for m in tf.getmembers():
                        if m.name.endswith("control"):
                            return dict(
                                line.partition(":")[0::2]
                                for line in tf.extractfile(m).read().decode("utf-8", "replace").splitlines()
                                if ":" in line
                            )
                return {}

if __name__ == "__main__":
    print("debtools module ok")
