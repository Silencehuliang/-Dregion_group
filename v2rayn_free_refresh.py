#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2rayN 免费节点一键刷新工具
============================
流程: 拉取 GitHub 免费节点(多镜像容错) → 解析去重 → mihomo 高并发延迟初筛
      → google generate_204 真实可用性验证(剔除出口屏蔽 google 的假活节点)
      → 下载测速(Cloudflare, 两轮取最优) → 写入 v2rayN 数据库(源分组+快速分组)
      → 重启 v2rayN → 自动激活最优节点并端到端验证(google/github)

用法:
    python v2rayn_free_refresh.py                  # 完整刷新
    python v2rayn_free_refresh.py --skip-fetch     # 复用上次拉取的节点文件(不联网抓取)
    python v2rayn_free_refresh.py --limit 3000     # 只初筛前 N 个节点(快速跑)
    python v2rayn_free_refresh.py --fast-ms 300    # 快速分组延迟阈值(默认 300)
    python v2rayn_free_refresh.py --no-restart     # 只更新数据库, 不重启/激活 v2rayN

依赖: Python 3.10+ 和 pyyaml (pip install pyyaml); mihomo 内核复用 v2rayN 自带的。
"""
import argparse, base64, json, os, random, re, shutil, socket, sqlite3, subprocess, sys, time
import urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import yaml
except ImportError:
    print("缺少 pyyaml: 请先执行  pip install pyyaml")
    sys.exit(1)

# ---------------- 基本路径 ----------------
BASE = os.path.dirname(os.path.abspath(__file__))
V2DIR = r"E:\Software\v2rayN-windows-64-desktop\v2rayN-windows-64"
V2EXE = os.path.join(V2DIR, "v2rayN.exe")
DB = os.path.join(V2DIR, "guiConfigs", "guiNDB.db")
CFGJSON = os.path.join(V2DIR, "guiConfigs", "guiNConfig.json")
MIHOMO = os.path.join(V2DIR, "bin", "mihomo", "mihomo.exe")
API = "http://127.0.0.1:19090"
RUNDIR = os.path.join(BASE, "rundir")
CACHE = os.path.join(BASE, "cache")
BACKUP = os.path.join(BASE, "backup")
FAST_NAME = "免费快速节点≤300ms"

SCREEN_URL = "http://www.gstatic.com/generate_204"          # 初筛: 快速区分死活
VERIFY_URL = "https://www.google.com/generate_204"          # 验证: 真实可用性(v2rayN 同款)
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=20000000"

EC = {"vmess": 1, "ss": 3, "vless": 5, "trojan": 6, "hysteria2": 7, "tuic": 8}
CORE_SINGBOX = 24  # v2rayN ECoreType.sing_box

# 免费节点源: (tag, 备注, raw路径 repo/branch/file)
SOURCES = [
    ("mhdi_mix", "GitHub-MhdiTaheri", "MhdiTaheri/V2rayCollector/main/sub/mix"),
    ("epodonios_all", "GitHub-Epodonios", "Epodonios/v2ray-configs/main/All_Configs_Sub.txt"),
    ("mfuu_sub", "GitHub-mfuu", "mfuu/v2ray/master/sub"),
    ("ermaozi_v2ray", "GitHub-ermaozi", "ermaozi/get_subscribe/main/subscribe/v2ray.txt"),
    ("ebrasha_all", "GitHub-ebrasha", "ebrasha/free-v2ray-public-list/main/all_extracted_configs.txt"),
    ("aiboboxx_sub", "GitHub-aiboboxx", "aiboboxx/v2rayfree/main/sub"),
    ("peasoft_raw", "GitHub-peasoft", "peasoft/NoMoreWalls/master/list_raw.txt"),
    ("pawdroid_sub", "GitHub-Pawdroid", "Pawdroid/Free-servers/main/sub"),
]
# 镜像模板, 依次尝试(镜像失效会自动换下一个)
MIRRORS = [
    "https://ghproxy.net/https://raw.githubusercontent.com/{raw}",
    "https://testingcf.jsdelivr.net/gh/{gh}",
    "https://ghproxy.net/https://raw.githubusercontent.com/{raw}",
    "https://cdn.jsdelivr.net/gh/{gh}",
]
GOOD_SCHEMES = ("vmess://", "vless://", "trojan://", "ss://", "hysteria2://", "hy2://", "tuic://")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PBK_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
FP_OK = {"chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random", "randomized", "macos", "opera"}
SS_CIPHERS = {
    "aes-128-gcm", "aes-192-gcm", "aes-256-gcm", "aes-128-cfb", "aes-192-cfb", "aes-256-cfb",
    "aes-128-ctr", "aes-192-ctr", "aes-256-ctr", "rc4-md5", "rc4", "chacha20", "chacha20-ietf",
    "chacha20-ietf-poly1305", "xchacha20", "xchacha20-ietf", "xchacha20-ietf-poly1305",
    "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm", "2022-blake3-chacha20-poly1305", "none",
}
LOGFILE = None


def log(msg):
    line = time.strftime("%H:%M:%S ") + str(msg)
    print(line, flush=True)
    if LOGFILE:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------- 1. 拉取 ----------------
def fetch_all(skip_fetch):
    os.makedirs(CACHE, exist_ok=True)
    for tag, remarks, path in SOURCES:
        dest = os.path.join(CACHE, tag + ".raw")
        if skip_fetch and os.path.exists(dest) and os.path.getsize(dest) > 100:
            log(f"  [缓存] {tag} ({os.path.getsize(dest)} bytes)")
            continue
        gh = path.replace("/", "/", 1).split("/")
        gh_fmt = f"{gh[0]}@{gh[1]}/{ '/'.join(gh[2:]) }" if len(gh) > 2 else path
        gh_fmt = f"{gh[0]}@{gh[1]}/" + "/".join(gh[2:])
        data = None
        for tpl in MIRRORS:
            url = tpl.format(raw=path, gh=gh_fmt)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "v2rayn-refresh"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    d = r.read()
                if len(d) > 100:
                    data = d
                    break
            except Exception:
                continue
        if data:
            open(dest, "wb").write(data)
            log(f"  [下载] {tag}: {len(data)} bytes")
        else:
            log(f"  [失败] {tag}: 所有镜像均不可用, 跳过")


# ---------------- 2. 解析 ----------------
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
            return [l.strip() for l in whole.decode("utf-8").splitlines() if l.strip().startswith(GOOD_SCHEMES)]
        except Exception:
            pass
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if len(ln) > 40 and "://" not in ln and not ln.startswith("http"):
            dec = try_b64(ln)
            if dec:
                try:
                    out += [l.strip() for l in dec.decode("utf-8").splitlines() if l.strip().startswith(GOOD_SCHEMES)]
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
    import hashlib
    for tag, remarks, path in SOURCES:
        p = os.path.join(CACHE, tag + ".raw")
        if not os.path.exists(p):
            continue
        kept = 0
        for link in extract_lines(open(p, "rb").read()):
            n = parse_link(link)
            if not n or n["port"] <= 0 or n["port"] > 65535 or not n["address"]:
                continue
            a = n["address"].lower()
            if a.startswith("127.") or a.startswith("0.") or a == "localhost":
                continue
            base = f"{n['proto']}|{a}|{n['port']}|{n.get('id','')}"
            if n["proto"] in ("vmess", "vless", "trojan"):
                base += f"|{n.get('network','')}|{n.get('path','')}|{n.get('sni','')}"
            k = hashlib.md5(base.encode()).hexdigest()
            if k in seen:
                continue
            seen[k] = True
            n["source"] = tag
            nodes.append(n)
            kept += 1
        log(f"  [解析] {tag}: 保留 {kept}")
        if limit and len(nodes) >= limit:
            break
    return nodes


# ---------------- 3. mihomo 配置 ----------------
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


def to_clash(idx, n):
    proto = n["proto"]
    p = {"name": f"n{idx:05d}", "server": n["address"], "port": n["port"], "udp": True}
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
        if n.get("flow") and not (n.get("tls") or "").lower() in ("tls", "reality"):
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


def write_mihomo_config(nodes, listeners=None, out="config.yaml"):
    """生成 mihomo 配置。返回 {mihomo名: node}"""
    os.makedirs(RUNDIR, exist_ok=True)
    for f in ("geoip.dat", "geosite.dat", "Country.mmdb"):
        src = os.path.join(V2DIR, "bin", f)
        dst = os.path.join(RUNDIR, f)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)
    proxies, keep = [], {}
    for i, n in enumerate(nodes):
        p = to_clash(i, n)
        if p is None:
            continue
        proxies.append(p)
        keep[p["name"]] = n
    cfg = {
        "mixed-port": 0, "mode": "direct", "log-level": "warning", "ipv6": False,
        "unified-delay": True, "tcp-concurrent": True, "external-controller": "127.0.0.1:19090",
        "dns": {"enable": True, "ipv6": False,
                "default-nameserver": ["223.5.5.5", "119.29.29.29"],
                "nameserver": ["223.5.5.5", "119.29.29.29"]},
        "proxies": proxies, "proxy-groups": [],
    }
    if listeners:
        cfg["listeners"] = [{"name": f"lt{i:03d}", "type": "mixed", "port": 30001 + i,
                             "proxy": nm, "udp": False} for i, nm in enumerate(listeners)]
    class QDumper(yaml.SafeDumper):
        pass
    QDumper.add_representer(str, lambda d, s: d.represent_scalar(
        "tag:yaml.org,2002:str", s, style="'" if DANGEROUS_YAML.fullmatch(s) else None))
    with open(os.path.join(RUNDIR, out), "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, Dumper=QDumper, allow_unicode=True, sort_keys=False)
    return keep


# ---------------- 4. mihomo 进程 ----------------
def kill_mihomo():
    pid = None
    out = runcmd(["netstat", "-ano"])
    for ln in out.splitlines():
        if "127.0.0.1:19090" in ln and "LISTENING" in ln:
            pid = ln.split()[-1]
            break
    if pid:
        runcmd(["taskkill", "/PID", str(pid), "/F"])
        time.sleep(1)


def start_mihomo(config="config.yaml"):
    kill_mihomo()
    logf = open(os.path.join(RUNDIR, "mihomo.log"), "w")
    proc = subprocess.Popen([MIHOMO, "-d", RUNDIR, "-f", os.path.join(RUNDIR, config)],
                            stdout=logf, stderr=subprocess.STDOUT)
    for _ in range(40):
        try:
            with urllib.request.urlopen(API + "/version", timeout=2) as r:
                json.load(r)
                return proc
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("mihomo 启动失败, 请查看 rundir/mihomo.log")


def delay_test(names, url, timeout_ms, conc, keep):
    """返回 {mihomo名: 延迟ms}, -1 表示失败"""
    results = {}

    def one(name):
        q = urllib.parse.quote(name, safe="")
        u = f"{API}/proxies/{q}/delay?timeout={timeout_ms}&url={urllib.parse.quote(url, safe='')}"
        try:
            with urllib.request.urlopen(u, timeout=timeout_ms / 1000 + 5) as r:
                return name, json.load(r).get("delay", -1)
        except Exception:
            return name, -1

    done = 0
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = [ex.submit(one, n) for n in names]
        for f in as_completed(futs):
            nm, d = f.result()
            results[nm] = d
            done += 1
            if done % 2000 == 0:
                log(f"    ... {done}/{len(names)} 可达={sum(1 for v in results.values() if v > 0)}")
    return results


def speed_test(nodes, names, keep):
    """通过 mihomo listeners 测下载速度, 两轮取最优。names 为 mihomo 节点名(基于 nodes 确定性编号)。"""
    listeners = names[:60]
    best = {n: 0.0 for n in listeners}

    def run(port):
        cmd = ["curl", "-s", "-o", os.devnull, "-w", "%{http_code} %{speed_download}",
               "--socks5-hostname", f"127.0.0.1:{port}", "-m", "12", SPEED_URL]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip().split()
            return float(out[1]) if out and out[0] == "200" else 0.0
        except Exception:
            return 0.0

    for round_i in (1, 2):
        targets = listeners if round_i == 1 else [n for n, v in best.items() if v <= 0]
        if round_i == 2 and not targets:
            break
        write_mihomo_config(nodes, listeners=targets, out="config_speed.yaml")
        start_mihomo("config_speed.yaml")
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(run, 30001 + i): nm for i, nm in enumerate(targets)}
            for f in as_completed(futs):
                nm = futs[f]
                best[nm] = max(best.get(nm, 0), f.result())
    kill_mihomo()
    return {n: round(v / 1024, 1) for n, v in best.items()}


# ---------------- 5. v2rayN 进程 ----------------
def runcmd(cmd, timeout=None):
    """执行命令返回解码后的 stdout(Windows 中文控制台 GBK/UTF-8 自适应, 解码失败不抛异常)"""
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    out = r.stdout
    if out is None:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return out.decode(enc)
        except UnicodeDecodeError:
            continue
    return out.decode("utf-8", errors="replace")


def v2_running():
    return "v2rayN.exe" in runcmd(["tasklist", "/FI", "IMAGENAME eq v2rayN.exe"])


def kill_v2():
    if not v2_running():
        return
    r = runcmd(["taskkill", "/IM", "v2rayN.exe", "/F"])
    if "v2rayN.exe" in r and ("成功" in r or "SUCCESS" in r.upper()):
        time.sleep(2)
        return
    # 多半是管理员权限运行, 走 UAC 提权终止
    log("  v2rayN 以管理员权限运行, 弹出 UAC 提权窗口终止它(请在弹窗点[是]) ...")
    ps1 = os.path.join(BASE, "_kill_v2.ps1")
    with open(ps1, "w", encoding="utf-8") as f:
        f.write("taskkill /IM v2rayN.exe /F\n")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Start-Process powershell -Verb RunAs -Wait -ArgumentList "
                        f"'-NoProfile','-ExecutionPolicy','Bypass','-File','{ps1}'"], timeout=120)
    except Exception as e:
        log(f"  UAC 提权失败: {e}")
    for _ in range(20):
        if not v2_running():
            break
        time.sleep(1)
    time.sleep(1)


def start_v2():
    subprocess.Popen(["cmd", "/c", "start", "", V2EXE], cwd=V2DIR, close_fds=True)


def get_local_port():
    try:
        cfg = json.load(open(CFGJSON, encoding="utf-8"))
        return int(cfg["Inbound"][0]["LocalPort"])
    except Exception:
        return 10808


def proxy_ok(port, url="https://www.google.com/generate_204", want="204", tries=3):
    for i in range(tries):
        try:
            out = subprocess.run(["curl", "-s", "-o", os.devnull, "-w", "%{http_code}",
                                  "--socks5-hostname", f"127.0.0.1:{port}", "-m", "12", url],
                                 capture_output=True, text=True, timeout=16).stdout.strip()
            if out == want:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


# ---------------- 6. 数据库 ----------------
def new_id(existing):
    while True:
        s = str(random.randint(4 * 10**18, 5 * 10**18))
        if s not in existing:
            existing.add(s)
            return s


def backup_db():
    os.makedirs(BACKUP, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP, ts)
    os.makedirs(dst, exist_ok=True)
    for f in (DB, CFGJSON):
        if os.path.exists(f):
            shutil.copy2(f, dst)
    # 只保留最近 5 份
    dirs = sorted(os.listdir(BACKUP))
    for d in dirs[:-5]:
        shutil.rmtree(os.path.join(BACKUP, d), ignore_errors=True)
    log(f"  已备份到 backup/{ts}")


def profile_row(n, subid, is_sub, remarks, idx):
    proto = n["proto"]
    row = {"IndexId": idx, "ConfigType": EC[proto], "ConfigVersion": 2,
           "Address": n["address"].lower(), "Port": n["port"], "Ports": None,
           "Id": n.get("id", ""), "AlterId": int(n.get("aid") or 0), "Security": "",
           "Network": "", "Remarks": remarks, "HeaderType": "", "RequestHost": "",
           "Path": "", "StreamSecurity": "", "AllowInsecure": "false", "Subid": subid,
           "IsSub": is_sub, "Flow": "", "Sni": "", "Alpn": "", "CoreType": None,
           "PreSocksPort": None, "Fingerprint": "", "DisplayLog": 1, "PublicKey": "",
           "ShortId": "", "SpiderX": "", "Mldsa65Verify": "", "Extra": None,
           "MuxEnabled": None, "Cert": None}
    net = (n.get("network") or "tcp").lower()
    if net == "raw":
        net = "tcp"
    host = (n.get("host") or "").strip()
    path = (n.get("path") or "").strip()
    sni = (n.get("sni") or "").strip()
    if proto == "vmess":
        row["Network"] = net
        row["Security"] = n.get("security") or "auto"
        row["RequestHost"], row["Path"] = host, path
        if (n.get("header") or "").lower() == "http":
            row["HeaderType"] = "http"
        if (n.get("tls") or "").lower() == "tls":
            row["StreamSecurity"] = "tls"
            row["Sni"] = sni or host
        row["Alpn"], row["Fingerprint"] = n.get("alpn", ""), n.get("fp", "")
    elif proto == "vless":
        row["Network"] = net
        row["Security"] = "none"
        row["RequestHost"], row["Path"] = host, path
        row["Flow"] = n.get("flow", "")
        row["Alpn"], row["Fingerprint"] = n.get("alpn", ""), n.get("fp", "")
        tls = (n.get("tls") or "").lower()
        if tls == "reality":
            row["StreamSecurity"] = "reality"
            row["Sni"] = sni or host
            row["PublicKey"], row["ShortId"] = n.get("pbk", ""), n.get("sid", "")
            if not row["Fingerprint"]:
                row["Fingerprint"] = "chrome"
        elif tls == "tls":
            row["StreamSecurity"] = "tls"
            row["Sni"] = sni or host
    elif proto == "trojan":
        row["Network"] = net if net != "tcp" else ""
        row["Id"] = n["id"]
        row["RequestHost"], row["Path"] = host, path
        row["Sni"] = sni or host
        row["StreamSecurity"] = "tls"
        row["Alpn"], row["Fingerprint"] = n.get("alpn", ""), n.get("fp", "")
    elif proto == "ss":
        row["Security"], row["Id"] = n["security"], n["id"]
    elif proto == "hysteria2":
        row["Id"] = n["id"]
        row["StreamSecurity"] = "tls"
        row["Sni"] = sni
        row["CoreType"] = CORE_SINGBOX
        if n.get("obfs") == "salamander":
            row["HeaderType"] = "salamander"
            row["Path"] = n.get("obfs_password", "")
        if str(n.get("allow_insecure", "")).lower() in ("1", "true"):
            row["AllowInsecure"] = "true"
    elif proto == "tuic":
        row["Id"], row["Security"] = n["id"], n.get("pwd", "")
        row["StreamSecurity"] = "tls"
        row["Sni"] = sni
        row["Alpn"] = n.get("alpn", "") or "h3"
        row["CoreType"] = CORE_SINGBOX
        if str(n.get("allow_insecure", "")).lower() in ("1", "true"):
            row["AllowInsecure"] = "true"
    return row


COLS = ["IndexId", "ConfigType", "ConfigVersion", "Address", "Port", "Ports", "Id", "AlterId",
        "Security", "Network", "Remarks", "HeaderType", "RequestHost", "Path", "StreamSecurity",
        "AllowInsecure", "Subid", "IsSub", "Flow", "Sni", "Alpn", "CoreType", "PreSocksPort",
        "Fingerprint", "DisplayLog", "PublicKey", "ShortId", "SpiderX", "Mldsa65Verify",
        "Extra", "MuxEnabled", "Cert"]


def insert_profile(cur, row, delay, speed_kbps):
    cur.execute(f"INSERT INTO ProfileItem ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                [row[c] for c in COLS])
    cur.execute("INSERT INTO ProfileExItem (IndexId,Delay,Speed,Sort,Message) VALUES (?,?,?,0,'')",
                (row["IndexId"], delay, round(speed_kbps / 1024, 2)))


def update_db(usable, fast_names, delays_g, speed_kb, keep, fast_ms):
    """usable: google204可达的 mihomo名列表; fast_names: 其中 ≤fast_ms 的。
    返回 (fast_sid, 候选列表[(mihomo名, IndexId), ...按激活优先序])"""
    con = sqlite3.connect(DB)
    cur = con.cursor()
    existing = {r[0] for r in cur.execute("SELECT IndexId FROM ProfileItem")}
    existing |= {r[0] for r in cur.execute("SELECT Id FROM SubItem")}

    # 源分组: 不存在则创建
    sub_ids = {}
    sort = 3
    for tag, remarks, path in SOURCES:
        r = cur.execute("SELECT Id FROM SubItem WHERE Remarks=?", (remarks,)).fetchone()
        if r:
            sub_ids[tag] = r[0]
        else:
            sid = new_id(existing)
            cur.execute("INSERT INTO SubItem (Id,Remarks,Url,Enabled,Sort,Memo) VALUES (?,?,?,1,?,NULL)",
                        (sid, remarks, f"https://ghproxy.net/https://raw.githubusercontent.com/{path}", sort))
            sub_ids[tag] = sid
        sort += 1
    # 快速分组
    r = cur.execute("SELECT Id FROM SubItem WHERE Remarks=?", (FAST_NAME,)).fetchone()
    fast_sid = r[0] if r else new_id(existing)
    if not r:
        cur.execute("INSERT INTO SubItem (Id,Remarks,Url,Enabled,Sort,Memo) VALUES (?,?,NULL,1,0,NULL)",
                    (fast_sid, FAST_NAME))

    # 源分组节点全量替换(只放本次实测可用的)
    for tag, remarks, path in SOURCES:
        cur.execute("DELETE FROM ProfileExItem WHERE IndexId IN (SELECT IndexId FROM ProfileItem WHERE Subid=?)",
                    (sub_ids[tag],))
        cur.execute("DELETE FROM ProfileItem WHERE Subid=?", (sub_ids[tag],))
    idx_of = {}
    for name in usable:
        n = keep[name]
        delay = delays_g.get(name, -1)
        idx = new_id(existing)
        idx_of[name] = idx
        row = profile_row(n, sub_ids[n["source"]], 1, n.get("name") or f"{n['address']}:{n['port']}", idx)
        insert_profile(cur, row, delay, speed_kb.get(name, 0))

    # 快速分组重建
    cur.execute("DELETE FROM ProfileExItem WHERE IndexId IN (SELECT IndexId FROM ProfileItem WHERE Subid=?)", (fast_sid,))
    cur.execute("DELETE FROM ProfileItem WHERE Subid=?", (fast_sid,))
    fast_sorted = sorted(fast_names, key=lambda x: (delays_g.get(x, 9999), -speed_kb.get(x, 0)))
    for name in fast_sorted:
        n = keep[name]
        label = f"{delays_g[name]}ms {speed_kb.get(name, 0)}KB/s"
        idx = new_id(existing)
        idx_of[name] = idx
        row = profile_row(n, fast_sid, 0, f"{label} {n.get('name') or n['address']}", idx)
        insert_profile(cur, row, delays_g[name], speed_kb.get(name, 0))

    # 更新订阅 Memo(本次统计)
    from collections import Counter
    cnt_src = Counter(keep[n]["source"] for n in usable)
    ts = time.strftime("%Y-%m-%d %H:%M")
    for tag, remarks, path in SOURCES:
        cur.execute("UPDATE SubItem SET Memo=? WHERE Id=?",
                    (f"最近刷新 {ts} | 实测可用 {cnt_src.get(tag, 0)} 个 | 直链 "
                     f"https://raw.githubusercontent.com/{path}", sub_ids[tag]))
    cur.execute("UPDATE SubItem SET Memo=? WHERE Id=?",
                (f"最近刷新 {ts} | google204 实测延迟≤{fast_ms}ms 的节点汇总", fast_sid))
    con.commit()
    con.close()
    # 激活候选: 快速组优先(延迟升序), 其次可用但超阈值的
    candidates = [(n, idx_of[n]) for n in fast_sorted]
    for n in sorted([u for u in usable if u not in fast_names], key=lambda x: delays_g.get(x, 9999)):
        candidates.append((n, idx_of[n]))
    return fast_sid, candidates


def set_active(idx, fast_sid):
    cfg = json.load(open(CFGJSON, encoding="utf-8"))
    cfg["IndexId"] = idx
    cfg["SubIndexId"] = fast_sid
    json.dump(cfg, open(CFGJSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ---------------- 主流程 ----------------
def main():
    global LOGFILE
    ap = argparse.ArgumentParser(description="v2rayN 免费节点一键刷新")
    ap.add_argument("--skip-fetch", action="store_true", help="复用 cache/ 里上次拉取的节点文件")
    ap.add_argument("--limit", type=int, default=None, help="只初筛前 N 个去重节点")
    ap.add_argument("--fast-ms", type=int, default=300, help="快速分组延迟阈值, 默认 300")
    ap.add_argument("--screen-conc", type=int, default=200, help="初筛并发, 默认 200")
    ap.add_argument("--no-restart", action="store_true", help="只更新数据库, 不重启 v2rayN / 不激活节点")
    args = ap.parse_args()

    os.makedirs(LOGDIR := os.path.join(BASE, "logs"), exist_ok=True)
    LOGFILE = os.path.join(LOGDIR, "refresh_" + time.strftime("%Y%m%d_%H%M%S") + ".log")
    log(f"=== v2rayN 免费节点刷新 开始 (日志: {os.path.basename(LOGFILE)}) ===")

    t0 = time.time()
    # 1. 拉取
    log("[1/6] 拉取 GitHub 节点文件 ...")
    fetch_all(args.skip_fetch)
    # 2. 解析
    log("[2/6] 解析去重 ...")
    nodes = parse_all(args.limit)
    if not nodes:
        log("  没有解析到任何节点, 退出(网络问题? 请检查镜像)")
        return 1
    log(f"  共 {len(nodes)} 个去重节点")
    # 3. 初筛延迟
    log(f"[3/6] mihomo 初筛延迟({args.screen_conc} 并发, {SCREEN_URL}) ...")
    keep = write_mihomo_config(nodes)
    start_mihomo("config.yaml")
    d1 = delay_test(list(keep), SCREEN_URL, 2500, args.screen_conc, keep)
    survivors = [n for n, v in d1.items() if v > 0]
    log(f"  初筛存活: {len(survivors)}/{len(keep)}")
    # 4. google204 真实可用性
    log("[4/6] google generate_204 可用性验证(剔除出口屏蔽 google 的节点) ...")
    d2 = delay_test(survivors or [], VERIFY_URL, 4000, 10, keep)
    usable = sorted([n for n, v in d2.items() if v > 0], key=lambda x: d2[x])
    log(f"  真实可用: {len(usable)}")
    if not usable:
        kill_mihomo()
        log("  本轮没有任何节点可用, 保持 v2rayN 原状, 退出。可稍后重试。")
        return 1
    for n in usable:
        log(f"    {n}: {d2[n]}ms {keep[n]['proto']:8} {keep[n]['address']}:{keep[n]['port']}")
    # 5. 测速
    log("[5/6] 下载测速(Cloudflare, 两轮取最优) ...")
    speed_kb = speed_test(nodes, usable, keep)
    kill_mihomo()
    fast_names = [n for n in usable if d2[n] <= args.fast_ms]
    log(f"  快速节点(≤{args.fast_ms}ms): {len(fast_names)}")
    for n in fast_names:
        log(f"    {n}: {d2[n]}ms {speed_kb.get(n, 0)}KB/s {keep[n]['address']}:{keep[n]['port']}")
    # 6. 写库 + 重启激活
    log("[6/6] 写入 v2rayN 数据库 ...")
    backup_db()
    fast_sid, candidates = update_db(usable, fast_names, d2, speed_kb, keep, args.fast_ms)
    log(f"  源分组+快速分组已更新 (可用 {len(usable)}, 快速 {len(fast_names)})")

    if args.no_restart:
        log("--no-restart: 不重启 v2rayN。打开 v2rayN 主界面即可看到新分组。")
    else:
        port = get_local_port()
        activated = None
        for i, (name, idx) in enumerate(candidates[:4]):
            log(f"  激活候选 {i+1}/{min(4, len(candidates))}: {keep[name]['address']}:{keep[name]['port']} "
                f"({d2[name]}ms {speed_kb.get(name, 0)}KB/s)")
            kill_v2()
            set_active(idx, fast_sid)
            start_v2()
            ok = False
            for _ in range(12):  # 等 socks 端口起来
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    ok = True
                    break
                except OSError:
                    time.sleep(1)
            if ok and proxy_ok(port):
                activated = name
                log("  ✓ 激活成功且验证通过: google204 OK")
                break
            log("  该候选验证失败(可能刚下线), 换下一个 ...")
        if activated is None and candidates:
            log("  ⚠ 所有候选都没验证通过(节点波动), 分组已写入, 请在 v2rayN 里手动测延迟后选择。")
    log(f"=== 完成, 用时 {time.time()-t0:.0f}s ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
