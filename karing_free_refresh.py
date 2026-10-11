#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Karing 免费节点一键刷新工具
============================
面向 Karing(兼容 Clash.Meta / sing-box / V2ray / ss 订阅)的免费节点流水线:

    拉取 GitHub 免费节点(多镜像并发容错)
      -> 解析去重
      -> TCP 预筛(快速踢掉不可能连通的)
      -> mihomo 真实可用性验证(google generate_204)
      -> 下载测速(Cloudflare, 多轮取最优)
      -> 生成 Clash.Meta 订阅(含 url-test / fallback 分组, Karing 端自动择优)
      -> 本地 HTTP 服务提供固定订阅链接, Karing 可反复「更新订阅」

用法:
    python karing_free_refresh.py                  # 刷新一次, 写入 output/
    python karing_free_refresh.py --serve          # 常驻: 提供订阅链接 + 定时自动刷新
    python karing_free_refresh.py --skip-fetch     # 复用 cache/ 中上次的源文件(快)
    python karing_free_refresh.py --limit 3000     # 只解析前 N 个去重节点(快速跑)
    python karing_free_refresh.py --top 120        # 只对延迟最低的前 N 个测速
    python karing_free_refresh.py --fast-ms 300    # 「快速」标记的延迟阈值, 默认 300
    python karing_free_refresh.py --no-color       # 关闭彩色输出
    python karing_free_refresh.py --mihomo PATH    # 手动指定 mihomo.exe

依赖: Python 3.10+ 与 pyyaml(pip install pyyaml)。mihomo 内核自动探测/下载。
"""
import argparse, base64, http.server, io, json, os, re, shutil, socket, subprocess, sys, threading
import time, urllib.parse, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import yaml
except ImportError:
    print("缺少 pyyaml: 请先执行  pip install pyyaml")
    sys.exit(1)


# ======================================================================
# 配置区(想加/换节点源, 改这里即可)
# ======================================================================
BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache")
OUTDIR = os.path.join(BASE, "output")
LOGDIR = os.path.join(BASE, "logs")
BINDIR = os.path.join(BASE, "bin")
RUNDIR = os.path.join(BASE, "rundir")

# 常见客户端自带 mihomo 的位置(按顺序探测, 找到即用)
MIHOMO_CANDIDATES = [
    os.path.join(BINDIR, "mihomo.exe"),
    r"E:\Software\v2rayN-windows-64-desktop\v2rayN-windows-64\bin\mihomo\mihomo.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\clash-verge\resources\sidecar\mihomo-x86_64-pc-windows-msvc.exe"),
    os.path.expandvars(r"%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\mihomo\mihomo.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\clash-verge-rev\mihomo.exe"),
]
MIHOMO_REPO = "MetaCubeX/mihomo"

# 免费节点源: (缓存文件名, 分组显示名, raw 路径 repo/branch/file)
SOURCES = [
    ("mhdi_mix",       "GitHub-MhdiTaheri",   "MhdiTaheri/V2rayCollector/main/sub/mix"),
    ("epodonios_all",  "GitHub-Epodonios",    "Epodonios/v2ray-configs/main/All_Configs_Sub.txt"),
    ("mfuu_sub",       "GitHub-mfuu",         "mfuu/v2ray/master/sub"),
    ("ermaozi_v2ray",  "GitHub-ermaozi",      "ermaozi/get_subscribe/main/subscribe/v2ray.txt"),
    ("ebrasha_all",    "GitHub-ebrasha",      "ebrasha/free-v2ray-public-list/main/all_extracted_configs.txt"),
    ("aiboboxx_sub",   "GitHub-aiboboxx",     "aiboboxx/v2rayfree/main/sub"),
    ("peasoft_raw",    "GitHub-peasoft",      "peasoft/NoMoreWalls/master/list_raw.txt"),
    ("pawdroid_sub",   "GitHub-Pawdroid",     "Pawdroid/Free-servers/main/sub"),
    ("barryfar_all",   "GitHub-barry-far",    "barry-far/V2ray-Configs/main/All_Configs_Sub.txt"),
    ("ripaojiedian",   "GitHub-ripaojiedian", "ripaojiedian/freenode/main/sub"),
    ("mahdi_eternity", "GitHub-mahdibland",   "mahdibland/ShadowsocksAggregator/master/Eternity.txt"),
]

# 镜像模板。{raw}=repo/branch/file, {gh}=owner@branch/rest。依次尝试, 先成功先用。
MIRROR_TEMPLATES = [
    "https://raw.githubusercontent.com/{raw}",
    "https://ghproxy.net/https://raw.githubusercontent.com/{raw}",
    "https://gh-proxy.com/https://raw.githubusercontent.com/{raw}",
    "https://ghfast.top/https://raw.githubusercontent.com/{raw}",
    "https://raw.gitmirror.com/{raw}",
    "https://testingcf.jsdelivr.net/gh/{gh}",
    "https://cdn.jsdelivr.net/gh/{gh}",
]

SCREEN_URL = "https://www.google.com/generate_204"      # 真实可用性验证(与 Karing 同款)
DELAY_URL  = "https://www.gstatic.com/generate_204"     # 仅作参考
SPEED_URL  = "https://speed.cloudflare.com/__down?bytes=30000000"

GOOD_SCHEMES = ("vmess://", "vless://", "trojan://", "ss://", "hysteria2://", "hy2://", "tuic://")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PBK_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
FP_OK = {"chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random",
         "randomized", "macos", "opera"}
SS_CIPHERS = {
    "aes-128-gcm", "aes-192-gcm", "aes-256-gcm", "aes-128-cfb", "aes-192-cfb", "aes-256-cfb",
    "aes-128-ctr", "aes-192-ctr", "aes-256-ctr", "rc4-md5", "rc4", "chacha20", "chacha20-ietf",
    "chacha20-ietf-poly1305", "xchacha20", "xchacha20-ietf", "xchacha20-ietf-poly1305",
    "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm", "2022-blake3-chacha20-poly1305", "none",
}

# Karing 订阅里的分组名
GROUP_SELECT = "🚀 节点选择"
GROUP_AUTO = "🎯 自动选择"
GROUP_FALLBACK = "♻️ 故障转移"

LOGFILE = None
COLOR = True
STATE_LOCK = threading.Lock()
STATE = {"last_refresh": None, "usable": 0, "fast": 0, "total": 0, "top": [], "subscription": {}}


# ======================================================================
# 日志 / 终端
# ======================================================================
class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
    BLUE = "\033[34m"; MAGENTA = "\033[35m"; CYAN = "\033[36m"


def _init_color():
    # 控制台可能是不支持 emoji 的 GBK 代码页, 用 replace 兜底避免崩溃
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        os.system("")  # 让 Windows 10+ 控制台支持 ANSI


def paint(s, color):
    return f"{color}{s}{C.RESET}" if COLOR else str(s)


def log(msg=""):
    line = time.strftime("%H:%M:%S ") + str(msg)
    print(line, flush=True)
    if LOGFILE:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def step(n, total, title):
    log(paint(f"\n[{n}/{total}] {title}", C.BOLD + C.CYAN))


def ok(msg):
    log("  " + paint("✓ ", C.GREEN) + str(msg))


def warn(msg):
    log("  " + paint("⚠ ", C.YELLOW) + str(msg))


def err(msg):
    log("  " + paint("✗ ", C.RED) + str(msg))


class Progress:
    """单行刷新的进度条, 结束时自动换行。"""

    def __init__(self, label, total):
        self.label = label
        self.total = max(int(total), 1)
        self.done = 0
        self.lock = threading.Lock()
        self.enabled = sys.stdout.isatty()
        self.t0 = time.time()

    def update(self, n=1, extra=""):
        with self.lock:
            self.done += n
            if not self.enabled:
                return
            frac = min(self.done / self.total, 1.0)
            width = 28
            filled = int(width * frac)
            bar = "#" * filled + "." * (width - filled)
            sys.stdout.write(f"\r  {self.label} [{bar}] {self.done}/{self.total} {extra}   ")
            sys.stdout.flush()

    def close(self, extra=""):
        if self.enabled:
            sys.stdout.write("\r" + " " * 90 + "\r")
            sys.stdout.flush()
        if extra:
            log("  " + str(extra))


# ======================================================================
# 1. 拉取
# ======================================================================
def _mirror_urls(path):
    parts = path.split("/")
    if len(parts) >= 3:
        gh = f"{parts[0]}@{parts[1]}/" + "/".join(parts[2:])
    else:
        gh = path
    for tpl in MIRROR_TEMPLATES:
        yield tpl.format(raw=path, gh=gh)


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Karing-free-refresh)",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_source(tag, remarks, path):
    """下载单个源, 多镜像容错。返回 (bytes|None, 说明)。"""
    last_err = ""
    for url in _mirror_urls(path):
        try:
            data = _http_get(url, timeout=20)
            if len(data) > 100:
                return data, url
            last_err = "内容过短"
        except Exception as e:
            last_err = type(e).__name__
    return None, last_err


def fetch_all(skip_fetch):
    os.makedirs(CACHE, exist_ok=True)
    log(f"  并发拉取 {len(SOURCES)} 个源(命中镜像优先, 失败自动换下一个) ...")
    results = {}
    prog = Progress("下载", len(SOURCES))

    def one(tag, remarks, path):
        dest = os.path.join(CACHE, tag + ".raw")
        if skip_fetch and os.path.exists(dest) and os.path.getsize(dest) > 100:
            return tag, "cache", os.path.getsize(dest)
        data, src = fetch_source(tag, remarks, path)
        if data:
            with open(dest, "wb") as f:
                f.write(data)
            return tag, "下载", len(data)
        return tag, f"失败({src})", 0

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futs = {ex.submit(one, *s): s for s in SOURCES}
        for f in as_completed(futs):
            tag, how, size = f.result()
            results[tag] = (how, size)
            prog.update(1, tag)
    prog.close()
    for tag, remarks, path in SOURCES:
        how, size = results.get(tag, ("?", 0))
        if size:
            kind = "缓存" if how == "cache" else "下载"
            log(f"    {kind}  {tag:<16} {size/1024:6.0f} KB")
        else:
            warn(f"{tag:<16} 所有镜像均不可用, 跳过")
    return results


# ======================================================================
# 2. 解析 / 去重
# ======================================================================
def try_b64(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4), validate=False)
    except Exception:
        return None


def extract_lines(data: bytes):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="ignore")
    lines = [l.strip() for l in text.splitlines() if l.strip().startswith(GOOD_SCHEMES)]
    if lines:
        return lines
    whole = try_b64(re.sub(r"\s+", "", text))
    if whole:
        try:
            got = [l.strip() for l in whole.decode("utf-8").splitlines()
                   if l.strip().startswith(GOOD_SCHEMES)]
            if got:
                return got
        except Exception:
            pass
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if len(ln) > 40 and "://" not in ln and not ln.startswith("http"):
            dec = try_b64(ln)
            if dec:
                try:
                    out += [l.strip() for l in dec.decode("utf-8").splitlines()
                            if l.strip().startswith(GOOD_SCHEMES)]
                except Exception:
                    pass
    return out


def uq(s, default=""):
    return urllib.parse.unquote(s or default)


def parse_link(link):
    scheme, _, rest = link.partition("://")
    scheme = scheme.lower()
    if scheme == "vmess":
        try:
            j = json.loads(try_b64(rest).decode("utf-8"))
        except Exception:
            return None
        add = str(j.get("add", "")).strip()
        if not add:
            return None
        return dict(proto="vmess", address=add, port=int(j.get("port", 0) or 0), id=str(j.get("id", "")),
                    aid=int(j.get("aid", 0) or 0), security=str(j.get("scy") or "auto"),
                    network=str(j.get("net") or "tcp"), header=str(j.get("type") or ""),
                    host=str(j.get("host") or ""), path=str(j.get("path") or ""),
                    tls=str(j.get("tls") or ""), sni=str(j.get("sni") or ""),
                    alpn=str(j.get("alpn") or ""), fp=str(j.get("fp") or ""), name=str(j.get("ps") or ""))
    frag = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
    qpos = rest.find("?")
    query = urllib.parse.parse_qs(rest[qpos + 1:] if qpos >= 0 else "")
    if qpos >= 0:
        rest = rest[:qpos]
    if "@" not in rest:
        return None
    cred, _, hostport = rest.rpartition("@")
    hostport = uq(hostport)
    m = re.match(r"^\[(.+)\]:(\d+)$", hostport) or re.match(r"^(.+):(\d+)$", hostport)
    if not m:
        return None
    host, port = m.group(1), int(m.group(2))
    g = lambda k, d="": uq(query.get(k, [d])[0])
    if scheme == "vless":
        return dict(proto="vless", address=host, port=port, id=cred, security=g("encryption", "none"),
                    flow=g("flow"), network=g("type", "tcp"), header=g("headerType"), host=g("host"),
                    path=g("path"), tls=g("security", "none"), sni=g("sni"), alpn=g("alpn"), fp=g("fp"),
                    pbk=g("pbk"), sid=g("sid"), name=uq(frag))
    if scheme == "trojan":
        return dict(proto="trojan", address=host, port=port, id=cred, security="", network=g("type", "tcp"),
                    header=g("headerType"), host=g("host"), path=g("path"), tls=g("security", "tls"),
                    sni=g("sni"), alpn=g("alpn"), fp=g("fp"), allow_insecure=g("allowInsecure"), name=uq(frag))
    if scheme in ("hysteria2", "hy2"):
        return dict(proto="hysteria2", address=host, port=port, id=cred, tls="tls", sni=g("sni"),
                    allow_insecure=g("insecure"), obfs=g("obfs"), obfs_password=g("obfs-password"), name=uq(frag))
    if scheme == "tuic":
        uuid, _, pwd = cred.partition(":")
        return dict(proto="tuic", address=host, port=port, id=uq(uuid), pwd=uq(pwd), tls="tls", sni=g("sni"),
                    alpn=g("alpn"), cc=g("congestion_control"), name=uq(frag))
    if scheme == "ss":
        dec = try_b64(cred)
        if dec and b":" in dec and b"@" not in dec:
            meth, _, pwd = dec.decode("utf-8", "ignore").partition(":")
        elif ":" in cred and "@" not in cred:
            meth, _, pwd = uq(cred).partition(":")
        elif "@" in cred:
            head, _, hp = cred.partition("@")
            dec2 = try_b64(head)
            if not dec2:
                return None
            meth, _, pwd = dec2.decode("utf-8", "ignore").partition(":")
            m2 = re.match(r"^(.+):(\d+)$", uq(hp))
            if not m2:
                return None
            host, port = m2.group(1), int(m2.group(2))
        else:
            return None
        if not meth:
            return None
        return dict(proto="ss", address=host, port=port, security=meth, id=pwd, name=uq(frag))
    return None


def parse_all(limit=None):
    nodes, seen = [], {}
    for tag, remarks, path in SOURCES:
        p = os.path.join(CACHE, tag + ".raw")
        if not os.path.exists(p):
            continue
        kept = 0
        with open(p, "rb") as f:
            raw = f.read()
        for link in extract_lines(raw):
            n = parse_link(link)
            if not n or n["port"] <= 0 or n["port"] > 65535 or not n["address"]:
                continue
            a = n["address"].lower()
            if a.startswith("127.") or a.startswith("0.") or a == "localhost":
                continue
            base = f"{n['proto']}|{a}|{n['port']}|{n.get('id','')}"
            if n["proto"] in ("vmess", "vless", "trojan"):
                base += f"|{n.get('network','')}|{n.get('path','')}|{n.get('sni','')}"
            if n["proto"] in ("vless", "vmess"):
                base += f"|{n.get('pbk','')}|{n.get('sid','')}"
            import hashlib
            k = hashlib.md5(base.encode()).hexdigest()
            if k in seen:
                continue
            seen[k] = True
            n["source"] = tag
            nodes.append(n)
            kept += 1
        log(f"    {tag:<16} 解析保留 {paint(kept, C.GREEN)}")
        if limit and len(nodes) >= limit:
            break
    return nodes


# ======================================================================
# 3. mihomo 配置生成(测试用 + 订阅用共用 to_clash)
# ======================================================================
def clean_path(path):
    path = (path or "").strip()
    return "" if any(ch.isspace() for ch in path) else path


def trans_opts(n):
    net = (n.get("network") or "tcp").lower()
    if net == "raw":
        net = "tcp"
    if net == "http":
        net = "h2"
    host = (n.get("host") or "").strip()
    path = clean_path(n.get("path"))
    o = {}
    pfx = path if path.startswith("/") else ((path and "/" + path) or "/")
    if net == "ws":
        d = {"path": pfx}
        if host:
            d["headers"] = {"Host": host}
        o["ws-opts"] = d
    elif net == "grpc":
        o["grpc-opts"] = {"grpc-service-name": path.split("?")[0] or "grpc"}
    elif net == "h2":
        d = {"path": pfx}
        if host:
            d["host"] = [h.split(",")[0] for h in host.split(",") if h.strip()]
        o["h2-opts"] = d
    elif net == "httpupgrade":
        d = {"path": pfx}
        if host:
            d["headers"] = {"Host": host}
        o["httpupgrade-opts"] = d
    elif net == "tcp" and (n.get("header") or "").lower() == "http":
        d = {"method": "GET", "path": [pfx]}
        if host:
            d["headers"] = {"Host": [host]}
        o["http-opts"] = d
    return net, o


def tls_common(p, n):
    tls = (n.get("tls") or "").lower()
    sni = n.get("sni") or n.get("host") or ""
    fp = n.get("fp") or "chrome"
    if n["proto"] == "vless" and tls == "reality":
        pbk, sid = n.get("pbk") or "", n.get("sid") or ""
        if not PBK_RE.match(pbk) or (sid and not re.match(r"^([0-9a-fA-F]{2}){1,8}$", sid)):
            return False
        p["tls"] = True
        if sni:
            p["servername"] = sni
        ro = {"public-key": pbk}
        if sid:
            ro["short-id"] = sid
        p["reality-opts"] = ro
        p["client-fingerprint"] = fp
        return True
    if tls in ("tls", "1", "true"):
        p["tls"] = True
        if sni:
            p["servername"] = sni
        p["skip-cert-verify"] = True
        if n.get("alpn"):
            p["alpn"] = [a for a in n["alpn"].split(",") if a.strip()]
        p["client-fingerprint"] = fp
        return True
    return False


def sane_common(p):
    if not p.get("server") or any(ch in p["server"] for ch in " /\t"):
        return False
    p["server"] = p["server"].strip().lower()
    if "servername" in p:
        sni = str(p["servername"]).strip()
        if not sni or any(ch in sni for ch in " /\t"):
            p.pop("servername", None)
    if p.get("client-fingerprint") not in FP_OK:
        p["client-fingerprint"] = "chrome"
    return True


def to_clash(n, name):
    proto = n["proto"]
    p = {"name": name, "server": n["address"], "port": n["port"], "udp": True}
    if proto == "vmess":
        if not UUID_RE.match(n["id"]):
            return None
        cipher = (n.get("security") or "auto").lower()
        if cipher not in ("auto", "aes-128-gcm", "chacha20-poly1305", "none", "zero"):
            cipher = "auto"
        p.update(type="vmess", uuid=n["id"], alterId=int(n.get("aid") or 0), cipher=cipher)
        net, o = trans_opts(n)
        p["network"] = net
        p.update(o)
        tls_common(p, n)
        return p if sane_common(p) else None
    if proto == "vless":
        if not UUID_RE.match(n["id"]):
            return None
        p.update(type="vless", uuid=n["id"])
        if n.get("flow") and n["flow"] not in ("xtls-rprx-vision", "xtls-rprx-vision-udp443"):
            return None
        if n.get("flow") and (n.get("tls") or "").lower() not in ("tls", "reality"):
            return None
        if n.get("flow"):
            p["flow"] = n["flow"]
        net, o = trans_opts(n)
        p["network"] = net
        p.update(o)
        tls_common(p, n)
        return p if sane_common(p) else None
    if proto == "trojan":
        if not n["id"] or any(ch.isspace() for ch in n["id"]):
            return None
        p.update(type="trojan", password=n["id"])
        net, o = trans_opts(n)
        if net not in ("tcp", "ws", "grpc", "h2", "httpupgrade"):
            return None
        p["network"] = net
        p.update(o)
        if n.get("sni") or n.get("host"):
            p["sni"] = n.get("sni") or n.get("host")
        p["skip-cert-verify"] = True
        if n.get("alpn"):
            p["alpn"] = [a for a in n["alpn"].split(",") if a.strip()]
        if n.get("fp"):
            p["client-fingerprint"] = n["fp"]
        return p if sane_common(p) else None
    if proto == "ss":
        m = (n.get("security") or "").lower()
        if m not in SS_CIPHERS or not n["id"]:
            return None
        pwd = n["id"]
        if m.startswith("2022-"):
            need = 16 if m.endswith("aes-128-gcm") else 32
            try:
                last = pwd.split(":")[-1]
                kb = base64.b64decode(last + "=" * (-len(last) % 4), altchars=b"-_", validate=True)
                if len(kb) != need:
                    return None
            except Exception:
                return None
        p.update(type="ss", cipher=m, password=pwd)
        return p if sane_common(p) else None
    if proto == "hysteria2":
        if not n["id"] or any(ch.isspace() for ch in n["id"]):
            return None
        p.update(type="hysteria2", password=n["id"])
        if n.get("sni"):
            p["sni"] = n["sni"]
        p["skip-cert-verify"] = True
        if n.get("obfs") == "salamander":
            p["obfs"] = "salamander"
            if n.get("obfs_password"):
                p["obfs-password"] = n["obfs_password"]
        return p if sane_common(p) else None
    if proto == "tuic":
        if not UUID_RE.match(n["id"]):
            return None
        p.update(type="tuic", uuid=n["id"], password=n.get("pwd", ""))
        if n.get("sni"):
            p["sni"] = n["sni"]
        cc = (n.get("cc") or "bbr").lower()
        p["congestion-controller"] = cc if cc in ("bbr", "cubic", "new-reno") else "bbr"
        p["alpn"] = ["h3"]
        p["skip-cert-verify"] = True
        p["udp-relay"] = True
        return p if sane_common(p) else None
    return None


DANGEROUS_YAML = re.compile(
    r"^[-+]?[0-9][0-9_]*(\.[0-9_]*)?([eE][-+]?[0-9]+)?$"
    r"|^0[xXoObB][0-9a-fA-F_]+$|^0[0-9_]+$|^\.inf$|^\.nan$"
    r"|^(true|false|null|yes|no|on|off|~)$", re.I)


class QDumper(yaml.SafeDumper):
    pass


QDumper.add_representer(str, lambda d, s: d.represent_scalar(
    "tag:yaml.org,2002:str", s, style="'" if DANGEROUS_YAML.fullmatch(s) else None))


def dump_yaml(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(obj, f, Dumper=QDumper, allow_unicode=True, sort_keys=False)


def build_test_config(proxies, listeners=None, controller_port=0):
    cfg = {
        "mixed-port": 0, "mode": "direct", "log-level": "warning", "ipv6": False,
        "unified-delay": True, "tcp-concurrent": True,
        "external-controller": f"127.0.0.1:{controller_port}",
        "dns": {"enable": True, "ipv6": False,
                "default-nameserver": ["223.5.5.5", "119.29.29.29"],
                "nameserver": ["223.5.5.5", "119.29.29.29"]},
        "proxies": proxies, "proxy-groups": [],
    }
    if listeners:
        cfg["listeners"] = [{"name": f"lt{i:03d}", "type": "mixed", "port": 30001 + i,
                             "proxy": nm, "udp": False} for i, nm in enumerate(listeners)]
    return cfg


def write_test_config(nodes, listeners=None, controller_port=0, out="config_test.yaml"):
    """生成测试用 mihomo 配置, 返回 {mihomo名: node}。节点名确定性编号, 便于回填。"""
    os.makedirs(RUNDIR, exist_ok=True)
    proxies, keep = [], {}
    for i, n in enumerate(nodes):
        name = f"n{i:05d}"
        p = to_clash(n, name)
        if p is None:
            continue
        proxies.append(p)
        keep[name] = n
    dump_yaml(build_test_config(proxies, listeners, controller_port),
              os.path.join(RUNDIR, out))
    return keep


# ======================================================================
# 4. mihomo 进程管理
# ======================================================================
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def find_mihomo(explicit=None):
    if explicit:
        if os.path.exists(explicit):
            return explicit
        raise RuntimeError(f"指定的 mihomo 不存在: {explicit}")
    for p in MIHOMO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    import glob
    for pat in (r"C:\Program Files\**\mihomo*.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\**\mihomo*.exe")):
        for p in glob.glob(pat, recursive=True):
            if "uninstall" not in p.lower():
                return p
    return None


def download_mihomo():
    os.makedirs(BINDIR, exist_ok=True)
    log("  本机未找到 mihomo 内核, 正在从 GitHub 下载(约 10MB) ...")
    try:
        api = f"https://api.github.com/repos/{MIHOMO_REPO}/releases/latest"
        rel = json.loads(_http_get(api, timeout=25).decode("utf-8"))
        asset = None
        for a in rel.get("assets", []):
            nm = a["name"].lower()
            if nm.endswith(".zip") and "windows-amd64" in nm and "compatible" not in nm:
                asset = a
                break
        if not asset:
            raise RuntimeError("未找到 windows-amd64 资源")
        url = asset["browser_download_url"]
    except Exception as e:
        raise RuntimeError(f"查询 mihomo 版本失败: {e}")
    data = None
    for u in (url, "https://ghproxy.net/" + url, "https://gh-proxy.com/" + url):
        try:
            data = _http_get(u, timeout=60)
            break
        except Exception:
            continue
    if not data:
        raise RuntimeError("mihomo 下载失败(镜像均不可用), 请手动把 mihomo.exe 放到 bin/ 目录")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.namelist():
            if info.lower().endswith("mihomo.exe"):
                dst = os.path.join(BINDIR, "mihomo.exe")
                with z.open(info) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)
                return dst
    raise RuntimeError("压缩包中没有 mihomo.exe")


def ensure_mihomo(explicit=None, allow_download=True):
    p = find_mihomo(explicit)
    if p:
        return p
    if allow_download:
        return download_mihomo()
    raise RuntimeError("未找到 mihomo.exe, 请用 --mihomo 指定, 或放到 bin/ 目录")


class Mihomo:
    def __init__(self, exe):
        self.exe = exe
        self.proc = None
        self.port = 0
        self.logf = None

    def ensure_port(self):
        if not self.port:
            self.port = free_port()
        return self.port

    def start(self, config_path):
        self.stop()
        self.ensure_port()
        self.logf = open(os.path.join(RUNDIR, "mihomo.log"), "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [self.exe, "-d", RUNDIR, "-f", config_path],
            stdout=self.logf, stderr=subprocess.STDOUT)
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/version", timeout=2) as r:
                    json.load(r)
                    return self
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError("mihomo 启动即退出, 请查看 rundir/mihomo.log")
                time.sleep(0.4)
        raise RuntimeError("mihomo 启动超时, 请查看 rundir/mihomo.log")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        if self.logf:
            try:
                self.logf.close()
            except Exception:
                pass
            self.logf = None
        time.sleep(0.3)

    def delay(self, name, url, timeout_ms):
        q = urllib.parse.quote(name, safe="")
        u = (f"http://127.0.0.1:{self.port}/proxies/{q}/delay"
             f"?timeout={timeout_ms}&url={urllib.parse.quote(url, safe='')}")
        try:
            with urllib.request.urlopen(u, timeout=timeout_ms / 1000 + 5) as r:
                return int(json.load(r).get("delay", -1))
        except Exception:
            return -1

    def delay_many(self, names, url, timeout_ms, conc, label="延迟测试"):
        results = {}
        prog = Progress(label, len(names))
        alive = [0]

        def one(name):
            return name, self.delay(name, url, timeout_ms)

        with ThreadPoolExecutor(max_workers=conc) as ex:
            futs = [ex.submit(one, n) for n in names]
            for f in as_completed(futs):
                nm, d = f.result()
                results[nm] = d
                if d > 0:
                    alive[0] += 1
                prog.update(1, f"存活={alive[0]}")
        prog.close()
        return results


# ======================================================================
# 5. TCP 预筛 / 测速
# ======================================================================
def tcp_screen(nodes, timeout=2.0, conc=400):
    """纯 socket 连通性预筛: 便宜且能快速踢掉大量死 IP。返回可连通的节点列表。"""
    alive_keys = set()
    prog = Progress("TCP 预筛", len(nodes))

    def one(n):
        try:
            with socket.create_connection((n["address"], n["port"]), timeout=timeout):
                return f"{n['address']}:{n['port']}", True
        except Exception:
            return f"{n['address']}:{n['port']}", False

    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = [ex.submit(one, n) for n in nodes]
        for f in as_completed(futs):
            key, okc = f.result()
            if okc:
                alive_keys.add(key)
            prog.update(1, f"存活={len(alive_keys)}")
    prog.close()
    return [n for n in nodes if f"{n['address']}:{n['port']}" in alive_keys]


def speed_test(mh, nodes, names, per_round=2, conc=12):
    """经 mihomo 的独立监听端口下载 Cloudflare 测速, 多轮取最优。返回 {name: KB/s}。"""
    listeners = names[:128]
    if not listeners:
        return {}
    best = {n: 0.0 for n in listeners}

    def run(port):
        cmd = ["curl", "-s", "-o", os.devnull, "-w", "%{http_code} %{speed_download}",
               "--socks5-hostname", f"127.0.0.1:{port}", "-m", "15", SPEED_URL]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=22).stdout.strip().split()
            return float(out[1]) if out and out[0] == "200" else 0.0
        except Exception:
            return 0.0

    for r in range(per_round):
        targets = listeners if r == 0 else [n for n, v in best.items() if v <= 0]
        if not targets:
            break
        write_test_config(nodes, listeners=targets, controller_port=mh.port, out="config_speed.yaml")
        mh.start(os.path.join(RUNDIR, "config_speed.yaml"))
        prog = Progress(f"测速(第{r+1}轮)", len(targets))
        with ThreadPoolExecutor(max_workers=conc) as ex:
            futs = {ex.submit(run, 30001 + i): nm for i, nm in enumerate(targets)}
            for f in as_completed(futs):
                nm = futs[f]
                try:
                    best[nm] = max(best.get(nm, 0), f.result())
                except Exception:
                    pass
                prog.update(1)
        prog.close()
    return {n: round(v / 1024, 1) for n, v in best.items()}


# ======================================================================
# 6. 生成订阅(Clash.Meta)
# ======================================================================
def safe_name(n, idx, delay=None):
    host = f"{n['address']}:{n['port']}"
    base = f"{n['proto']}-{idx:03d}"
    tag = f"{base} {delay}ms {host}" if delay else f"{base} {host}"
    return tag[:80]


def build_subscription(nodes, stats, max_nodes=400):
    """stats: {mihomo名: {delay, speed}}。返回 (Clash.Meta 订阅 dict, {代理名: node})。"""
    ranked = sorted(
        nodes,
        key=lambda x: (
            0 if stats.get(x["_name"], {}).get("delay", -1) > 0 else 1,
            -stats.get(x["_name"], {}).get("speed", 0.0),
            stats.get(x["_name"], {}).get("delay", 9999),
        ),
    )[:max_nodes]
    proxies, keep, names = [], {}, []
    for i, n in enumerate(ranked):
        st = stats.get(n["_name"], {})
        name = safe_name(n, i, st.get("delay"))
        while name in keep:
            name += "'"
        p = to_clash(n, name)
        if p is None:
            continue
        proxies.append(p)
        keep[name] = n
        names.append(name)
    if not proxies:
        return None, {}
    group_select = {
        "name": GROUP_SELECT, "type": "select",
        "proxies": [GROUP_AUTO, GROUP_FALLBACK] + names + ["DIRECT"],
    }
    group_auto = {
        "name": GROUP_AUTO, "type": "url-test", "url": SCREEN_URL,
        "interval": 300, "tolerance": 50, "lazy": False, "proxies": list(names),
    }
    group_fallback = {
        "name": GROUP_FALLBACK, "type": "fallback", "url": SCREEN_URL,
        "interval": 300, "proxies": list(names),
    }
    cfg = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warn",
        "ipv6": False,
        "external-controller": "127.0.0.1:9090",
        "dns": {
            "enable": True, "ipv6": False, "enhanced-mode": "fake-ip",
            "default-nameserver": ["223.5.5.5", "119.29.29.29"],
            "nameserver": ["https://doh.pub/dns-query", "https://dns.alidns.com/dns-query"],
            "fallback": ["https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query"],
        },
        "proxies": proxies,
        "proxy-groups": [group_select, group_auto, group_fallback],
        "rules": [
            "GEOIP,LAN,DIRECT,no-resolve",
            "GEOIP,CN,DIRECT",
            f"MATCH,{GROUP_SELECT}",
        ],
    }
    return cfg, keep


def to_share_link(n, display_name=None):
    """把节点还原成 share link(用于 base64 通用订阅)。display_name 为订阅里显示的节点名。"""
    proto = n["proto"]
    raw_name = display_name or n.get("name") or f"{n['address']}:{n['port']}"
    name = urllib.parse.quote(raw_name)
    if proto == "vmess":
        obj = {"v": "2", "ps": raw_name, "add": n["address"], "port": str(n["port"]),
               "id": n["id"], "aid": str(n.get("aid", 0)), "scy": n.get("security", "auto"),
               "net": n.get("network", "tcp"), "type": n.get("header", ""), "host": n.get("host", ""),
               "path": n.get("path", ""), "tls": n.get("tls", ""), "sni": n.get("sni", ""),
               "alpn": n.get("alpn", ""), "fp": n.get("fp", "")}
        return "vmess://" + base64.b64encode(json.dumps(obj).encode()).decode()
    q = {}
    if proto == "vless":
        body = f"{n['id']}@{n['address']}:{n['port']}"
        for k, v in (("type", n.get("network")), ("security", n.get("tls")), ("sni", n.get("sni")),
                     ("host", n.get("host")), ("path", n.get("path")), ("alpn", n.get("alpn")),
                     ("fp", n.get("fp")), ("pbk", n.get("pbk")), ("sid", n.get("sid")),
                     ("flow", n.get("flow"))):
            if v:
                q[k] = v
    elif proto == "trojan":
        body = f"{n['id']}@{n['address']}:{n['port']}"
        for k, v in (("type", n.get("network")), ("security", n.get("tls") or "tls"),
                     ("sni", n.get("sni")), ("host", n.get("host")), ("path", n.get("path"))):
            if v:
                q[k] = v
    elif proto == "ss":
        user = base64.urlsafe_b64encode(f"{n['security']}:{n['id']}".encode()).decode().rstrip("=")
        return f"ss://{user}@{n['address']}:{n['port']}#{name}"
    elif proto == "hysteria2":
        body = f"{n['id']}@{n['address']}:{n['port']}"
        if n.get("sni"):
            q["sni"] = n["sni"]
        if n.get("obfs"):
            q["obfs"] = n["obfs"]
        if n.get("obfs_password"):
            q["obfs-password"] = n["obfs_password"]
    elif proto == "tuic":
        body = f"{n['id']}:{n.get('pwd','')}@{n['address']}:{n['port']}"
        if n.get("sni"):
            q["sni"] = n["sni"]
    else:
        return None
    tail = ("?" + urllib.parse.urlencode(q)) if q else ""
    return f"{proto}://{body}{tail}#{name}"


def generate_outputs(cfg, keep, stats, meta):
    os.makedirs(OUTDIR, exist_ok=True)
    subs = {}
    clash_path = os.path.join(OUTDIR, "karing.yaml")
    dump_yaml(cfg, clash_path)
    subs["clash"] = clash_path

    links = []
    nodes_meta = []
    for nm, node in keep.items():
        link = to_share_link(node, nm)
        if link:
            links.append(link)
        st = stats.get(node.get("_name", ""), {})
        nodes_meta.append({"name": nm, "proto": node["proto"], "address": node["address"],
                           "port": node["port"], "delay": st.get("delay", -1), "speed": st.get("speed", 0.0)})

    b64_path = os.path.join(OUTDIR, "sub_base64.txt")
    with open(b64_path, "w", encoding="utf-8") as f:
        f.write(base64.b64encode("\n".join(links).encode()).decode())
    subs["base64"] = b64_path

    plain_path = os.path.join(OUTDIR, "sub_plain.txt")
    with open(plain_path, "w", encoding="utf-8") as f:
        f.write("\n".join(links))
    subs["plain"] = plain_path

    meta = dict(meta)
    meta["nodes"] = nodes_meta
    json_path = os.path.join(OUTDIR, "meta.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    subs["meta"] = json_path
    return subs


# ======================================================================
# 7. 本地订阅服务
# ======================================================================
INDEX_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Karing 免费节点订阅</title>
<style>
 :root{--bg:#0f1115;--card:#171a21;--fg:#e8ebf0;--mut:#8b93a7;--acc:#4f9dff;--ok:#3ddc84}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);padding:24px;
   font-family:"Segoe UI",system-ui,-apple-system,"Noto Sans SC",sans-serif}
 h1{font-size:20px;margin:0 0 4px}
 .sub{color:var(--mut);font-size:13px;margin-bottom:20px}
 .card{background:var(--card);border:1px solid #232733;border-radius:12px;padding:16px;margin-bottom:14px}
 .row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 .label{font-size:12px;color:var(--mut);min-width:120px}
 code{flex:1;background:#0b0d12;border:1px solid #232733;border-radius:8px;padding:8px 10px;
   font-size:13px;overflow:auto;white-space:nowrap}
 button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:13px}
 button.ghost{background:#232733}
 .stat{display:inline-block;margin-right:18px;font-size:14px}.stat b{color:var(--ok);font-size:18px}
 table{width:100%;border-collapse:collapse;font-size:12px}
 th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #232733}
 th{color:var(--mut);font-weight:500}
</style></head><body>
<h1>Karing 免费节点订阅</h1>
<div class="sub">本服务常驻, 在 Karing 里「更新订阅」即可拿到最新筛选结果</div>
<div class="card"><div class="row"><span class="label">Clash.Meta 订阅</span>
  <code id="u1"></code><button onclick="cp('u1')">复制</button></div></div>
<div class="card"><div class="row"><span class="label">Base64 通用订阅</span>
  <code id="u2"></code><button class="ghost" onclick="cp('u2')">复制</button></div></div>
<div class="card"><div class="row"><span class="label">明文节点列表</span>
  <code id="u3"></code><button class="ghost" onclick="cp('u3')">复制</button></div></div>
<div class="card">
  <div class="stat">可用节点 <b id="s1">-</b></div>
  <div class="stat">快速节点 <b id="s2">-</b></div>
  <div class="stat">解析总数 <b id="s3">-</b></div>
  <div class="stat">上次刷新 <b id="s4">-</b></div></div>
<div class="card"><div style="margin-bottom:8px;color:var(--mut);font-size:12px">节点预览(按评分排序)</div>
<table id="t"><thead><tr><th>#</th><th>协议</th><th>地址</th><th>延迟</th><th>速度</th></tr></thead>
<tbody></tbody></table></div>
<script>
 var B = location.origin;
 document.getElementById('u1').textContent = B + '/karing.yaml';
 document.getElementById('u2').textContent = B + '/sub';
 document.getElementById('u3').textContent = B + '/plain';
 function cp(id){
   var t = document.getElementById(id).textContent;
   navigator.clipboard.writeText(t);
   var b = event.target; b.textContent = '已复制'; setTimeout(function(){ b.textContent = '复制'; }, 1200);
 }
 fetch('/health').then(function(r){ return r.json(); }).then(function(d){
   document.getElementById('s1').textContent = d.usable;
   document.getElementById('s2').textContent = d.fast;
   document.getElementById('s3').textContent = d.total;
   document.getElementById('s4').textContent = d.last_refresh || '-';
   var tb = document.querySelector('#t tbody');
   (d.top || []).forEach(function(n, i){
     tb.insertAdjacentHTML('beforeend',
       '<tr><td>' + (i+1) + '</td><td>' + n.proto + '</td><td>' + n.address + ':' + n.port +
       '</td><td>' + (n.delay > 0 ? n.delay + 'ms' : '-') +
       '</td><td>' + (n.speed ? Math.round(n.speed) + 'KB/s' : '-') + '</td></tr>');
   });
 });
</script></body></html>"""


class SubHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静音访问日志

    def _send(self, body, ctype="text/plain; charset=utf-8", code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Subscription-Userinfo", "upload=0; download=0; total=0; expire=0")
        self.send_header("Profile-Update-Interval", "6")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        route = {
            "/karing.yaml": ("karing.yaml", "text/yaml; charset=utf-8"),
            "/sub": ("sub_base64.txt", "text/plain; charset=utf-8"),
            "/plain": ("sub_plain.txt", "text/plain; charset=utf-8"),
        }.get(path)
        if path in ("/", "/index.html"):
            return self._send(INDEX_HTML, "text/html; charset=utf-8")
        if path == "/health":
            with STATE_LOCK:
                st = dict(STATE)
            return self._send(json.dumps(st, ensure_ascii=False), "application/json; charset=utf-8")
        if route:
            fp = os.path.join(OUTDIR, route[0])
            if not os.path.exists(fp):
                return self._send("尚未刷新完成, 请稍候", code=503)
            with open(fp, "rb") as fh:
                return self._send(fh.read(), route[1])
        return self._send("not found", code=404)


def start_server(host, port):
    srv = http.server.ThreadingHTTPServer((host, port), SubHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ======================================================================
# 8. 主流程
# ======================================================================
def refresh(args, mh):
    t0 = time.time()
    step(1, 6, "拉取 GitHub 节点文件" + ("(复用缓存)" if args.skip_fetch else ""))
    fetch_all(args.skip_fetch)

    step(2, 6, "解析去重")
    nodes = parse_all(args.limit)
    if not nodes:
        err("没有解析到任何节点(网络/镜像问题?), 本次保持原订阅")
        return False
    if args.limit and len(nodes) > args.limit:
        nodes = nodes[:args.limit]
    ok(f"共 {paint(len(nodes), C.GREEN)} 个去重节点")

    step(3, 6, f"TCP 预筛({len(nodes)} 个, {args.tcp_conc} 并发)")
    alive = tcp_screen(nodes, timeout=args.tcp_timeout, conc=args.tcp_conc)
    ok(f"TCP 可连通 {paint(len(alive), C.GREEN)}/{len(nodes)}")
    if not alive:
        err("没有任何节点 TCP 可连通, 本次保持原订阅")
        return False

    step(4, 6, "mihomo 真实可用性验证(google generate_204)")
    mh.ensure_port()
    keep = write_test_config(alive, controller_port=mh.port, out="config.yaml")
    mh.start(os.path.join(RUNDIR, "config.yaml"))
    d = mh.delay_many(list(keep), SCREEN_URL, 4000, args.verify_conc, "可用性验证")
    usable_names = sorted([n for n, v in d.items() if v > 0], key=lambda x: d[x])
    ok(f"真实可用 {paint(len(usable_names), C.GREEN)}")
    if not usable_names:
        err("本轮没有任何节点可用, 本次保持原订阅")
        return False

    step(5, 6, f"下载测速(延迟最低前 {args.top} 个, Cloudflare)")
    speed_names = usable_names[:args.top]
    spd = speed_test(mh, alive, speed_names, per_round=2, conc=args.speed_conc)
    mh.stop()
    ok(f"测速完成, 有效速度样本 {sum(1 for v in spd.values() if v > 0)}")

    stats = {nm: {"delay": d.get(nm, -1), "speed": spd.get(nm, 0.0)} for nm in usable_names}
    fast_names = [n for n in usable_names if 0 < d.get(n, 9999) <= args.fast_ms]
    for nm in keep:
        keep[nm]["_name"] = nm
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    meta = {"last_refresh": ts, "total": len(nodes), "usable": len(usable_names),
            "fast": len(fast_names), "fast_ms": args.fast_ms, "sources": [s[1] for s in SOURCES]}

    step(6, 6, "生成订阅(Clash.Meta / base64 / 明文)")
    cfg, sub_keep = build_subscription([keep[nm] for nm in usable_names], stats, max_nodes=args.max_nodes)
    if not cfg:
        err("可用节点都无法转换为有效代理, 本次保持原订阅")
        return False
    subs = generate_outputs(cfg, sub_keep, stats, meta)
    ok(f"Clash.Meta 订阅节点数 {paint(len(sub_keep), C.GREEN)} "
       f"(可用 {len(usable_names)}, 快速 {len(fast_names)})")

    top = [{"proto": sub_keep[nm]["proto"], "address": sub_keep[nm]["address"],
            "port": sub_keep[nm]["port"], "delay": stats[sub_keep[nm]["_name"]]["delay"],
            "speed": stats[sub_keep[nm]["_name"]]["speed"]} for nm in list(sub_keep)[:30]]
    with STATE_LOCK:
        STATE.update({"last_refresh": ts, "usable": len(usable_names), "fast": len(fast_names),
                      "total": len(nodes), "top": top, "subscription": subs})

    log()
    log(paint("  本次结果(前 15):", C.BOLD))
    for i, nm in enumerate(usable_names[:15]):
        n = keep[nm]
        log(f"    {i+1:>2}. {d[nm]:>4}ms  {stats[nm]['speed']:>7.1f}KB/s  "
            f"{n['proto']:<9} {n['address']}:{n['port']}")
    log()
    log(f"  耗时 {time.time()-t0:.0f}s, 输出目录: {OUTDIR}")
    return True


def main():
    global LOGFILE, COLOR
    _init_color()
    ap = argparse.ArgumentParser(
        description="Karing 免费节点一键刷新(产出可更新的订阅链接)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--serve", action="store_true", help="常驻运行: 提供订阅链接并定时自动刷新")
    ap.add_argument("--host", default="127.0.0.1", help="订阅服务监听地址(局域网用 0.0.0.0)")
    ap.add_argument("--port", type=int, default=20808, help="订阅服务端口")
    ap.add_argument("--interval", type=int, default=1800, help="--serve 模式下的刷新间隔(秒)")
    ap.add_argument("--skip-fetch", action="store_true", help="复用 cache/ 里的源文件")
    ap.add_argument("--limit", type=int, default=None, help="只解析前 N 个去重节点")
    ap.add_argument("--top", type=int, default=120, help="只对延迟最低的前 N 个测速")
    ap.add_argument("--max-nodes", type=int, default=400, help="写入订阅的最大节点数")
    ap.add_argument("--fast-ms", type=int, default=300, help="「快速」标记的延迟阈值(ms)")
    ap.add_argument("--tcp-conc", type=int, default=400, help="TCP 预筛并发")
    ap.add_argument("--tcp-timeout", type=float, default=2.0, help="TCP 预筛超时(秒)")
    ap.add_argument("--verify-conc", type=int, default=64, help="google204 验证并发")
    ap.add_argument("--speed-conc", type=int, default=12, help="测速并发")
    ap.add_argument("--mihomo", default=None, help="手动指定 mihomo.exe 路径")
    ap.add_argument("--no-download-core", action="store_true", help="不自动下载 mihomo 内核")
    ap.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    args = ap.parse_args()
    COLOR = not args.no_color and sys.stdout.isatty()

    os.makedirs(LOGDIR, exist_ok=True)
    LOGFILE = os.path.join(LOGDIR, "karing_" + time.strftime("%Y%m%d_%H%M%S") + ".log")
    log(paint("=== Karing 免费节点刷新 ===", C.BOLD + C.MAGENTA)
        + f"  日志: {os.path.basename(LOGFILE)}")

    try:
        mihomo_path = ensure_mihomo(args.mihomo, allow_download=not args.no_download_core)
    except Exception as e:
        err(str(e))
        return 1
    log(f"  mihomo 内核: {mihomo_path}")

    mh = Mihomo(mihomo_path)
    srv = None
    try:
        if args.serve:
            srv = start_server(args.host, args.port)
            host_show = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
            log()
            log(paint("  Karing 订阅服务已启动:", C.BOLD + C.GREEN))
            log(f"    Clash.Meta 订阅 : http://{host_show}:{args.port}/karing.yaml")
            log(f"    通用 base64 订阅: http://{host_show}:{args.port}/sub")
            log(f"    仪表盘          : http://{host_show}:{args.port}/")
            if args.host == "0.0.0.0":
                log("    手机/局域网     : 把 127.0.0.1 换成本机局域网 IP 后再访问")
            log("    在 Karing 里「添加订阅」粘贴链接, 之后点「更新订阅」即可刷新")
            log(f"    每 {args.interval}s 自动刷新一次, Ctrl+C 退出")
            while True:
                refresh(args, mh)
                time.sleep(args.interval)
        else:
            okr = refresh(args, mh)
            return 0 if okr else 1
    except KeyboardInterrupt:
        log("\n  已中断。")
        return 130
    finally:
        mh.stop()
        if srv:
            srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
