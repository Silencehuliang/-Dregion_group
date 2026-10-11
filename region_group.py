#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合并上游订阅 + 按地区分组
============================
把若干条「公开的上游免费节点订阅」拉下来, 去重合并, 然后输出**一份**带
地区分组的 Clash.Meta 订阅(mihomo / Karing 兼容)。Karing 里只需导入这一个文件/链接。

与 karing_free_refresh.py 的分工:
  - karing_free_refresh.py: 自己抓 GitHub 节点源 -> 测速 -> 生成订阅(自建路线)
  - region_group.py        : 直接吃别人做好的订阅 -> 合并 -> 按地区分好组(省事路线)

地区识别: 按节点名里的国旗 emoji / 国家关键字(中文或英文), 识别不到的归入「🌐 其他」。

用法:
    python region_group.py                     # 拉取 -> 合并 -> 输出 output/karing_region.yaml
    python region_group.py --skip-fetch        # 复用 cache/*.raw, 不重新联网抓源(快)
    python region_group.py --min-region 3      # 地区节点数 <3 就不单独建分组
    python region_group.py --max-nodes 800     # 合并后最多保留前 800 个
    python region_group.py --out xxx.yaml      # 自定义输出文件
    python region_group.py --no-color

依赖: Python 3.10+、pyyaml; 复用同目录 karing_free_refresh.py 的解析/YAML 工具。
"""
import argparse
import bisect
import json
import os
import re
import sys
import time
import socket
import ipaddress
import urllib.request
import concurrent.futures

try:
    import yaml
except ImportError:
    print("缺少 pyyaml: 请先执行  pip install pyyaml")
    sys.exit(1)

try:
    import karing_free_refresh as k
except Exception as e:  # 复用解析/配置生成工具
    print(f"无法导入 karing_free_refresh.py(需与本文件同目录): {e}")
    sys.exit(1)


# ======================================================================
# 配置区(想加/换上游订阅, 改这里)
# ======================================================================
BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = k.CACHE
OUTDIR = k.OUTDIR
DEFAULT_OUT = os.path.join(OUTDIR, "karing_region.yaml")
META_OUT = os.path.join(OUTDIR, "karing_region_meta.json")

OTHER = "🌐 其他"

# 上游订阅源: (缓存名, 格式, url)
#   格式 links = base64 或明文 share link 列表;  yaml = 现成的 Clash.Meta 配置(含 proxies 列表)
SOURCES = [
    ("matin_super", "links",
     "https://raw.githubusercontent.com/MatinGhanbari/v2ray-configs/main/subscriptions/v2ray/super-sub.txt"),
    ("barryfar",    "links",
     "https://raw.githubusercontent.com/barry-far/V2ray-Config/main/Sub1.txt"),
    ("nomorewalls", "yaml",
     "https://raw.githubusercontent.com/peasoft/NoMoreWalls/master/list.yml"),
    ("bestclash",   "yaml",
     "https://raw.githubusercontent.com/PuddinCat/BestClash/refs/heads/main/proxies.yaml"),
    ("epodonios",   "links",
     "https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Sub1.txt"),
]

# 追加 karing_free_refresh.py 里更丰富、更新更勤的源(对国内可用性更好)。
_seen_urls = {u for _, _, u in SOURCES}
for _cache, _label, _raw in getattr(k, "SOURCES", []):
    _u = f"https://raw.githubusercontent.com/{_raw}"
    if _u in _seen_urls:
        continue
    _seen_urls.add(_u)
    SOURCES.append((f"k_{_cache}", "links", _u))

# 二位国家码 -> 分组显示名(带国旗)
REGION_NAMES = {
    "HK": "🇭🇰 香港", "TW": "🇹🇼 台湾", "MO": "🇲🇴 澳门",
    "JP": "🇯🇵 日本", "KR": "🇰🇷 韩国", "SG": "🇸🇬 新加坡",
    "US": "🇺🇸 美国", "CA": "🇨🇦 加拿大", "MX": "🇲🇽 墨西哥", "BR": "🇧🇷 巴西",
    "AR": "🇦🇷 阿根廷", "CL": "🇨🇱 智利",
    "GB": "🇬🇧 英国", "DE": "🇩🇪 德国", "FR": "🇫🇷 法国", "NL": "🇳🇱 荷兰",
    "CH": "🇨🇭 瑞士", "SE": "🇸🇪 瑞典", "FI": "🇫🇮 芬兰", "NO": "🇳🇴 挪威",
    "DK": "🇩🇰 丹麦", "PL": "🇵🇱 波兰", "UA": "🇺🇦 乌克兰", "RU": "🇷🇺 俄罗斯",
    "IT": "🇮🇹 意大利", "ES": "🇪🇸 西班牙", "PT": "🇵🇹 葡萄牙", "IE": "🇮🇪 爱尔兰",
    "AT": "🇦🇹 奥地利", "BE": "🇧🇪 比利时", "CZ": "🇨🇿 捷克", "RO": "🇷🇴 罗马尼亚",
    "TR": "🇹🇷 土耳其", "IL": "🇮🇱 以色列", "AE": "🇦🇪 阿联酋", "SA": "🇸🇦 沙特",
    "IN": "🇮🇳 印度", "ID": "🇮🇩 印尼", "VN": "🇻🇳 越南", "TH": "🇹🇭 泰国",
    "MY": "🇲🇾 马来西亚", "PH": "🇵🇭 菲律宾", "PK": "🇵🇰 巴基斯坦",
    "AU": "🇦🇺 澳大利亚", "NZ": "🇳🇿 新西兰", "ZA": "🇿🇦 南非",
    "CN": "🇨🇳 中国", "UK": "🇬🇧 英国",
    "IR": "🇮🇷 伊朗", "KZ": "🇰🇿 哈萨克斯坦", "UZ": "🇺🇿 乌兹别克斯坦",
    "AZ": "🇦🇿 阿塞拜疆", "GE": "🇬🇪 格鲁吉亚", "AM": "🇦🇲 亚美尼亚",
    "MN": "🇲🇳 蒙古", "NP": "🇳🇵 尼泊尔", "BD": "🇧🇩 孟加拉",
    "LK": "🇱🇰 斯里兰卡", "MM": "🇲🇲 缅甸", "KH": "🇰🇭 柬埔寨",
    "LA": "🇱🇦 老挝", "BN": "🇧🇳 文莱",
    "EE": "🇪🇪 爱沙尼亚", "LV": "🇱🇻 拉脱维亚", "LT": "🇱🇹 立陶宛",
    "MD": "🇲🇩 摩尔多瓦", "BY": "🇧🇾 白俄罗斯", "UA": "🇺🇦 乌克兰",
    "PL": "🇵🇱 波兰", "CZ": "🇨🇿 捷克", "SK": "🇸🇰 斯洛伐克",
    "HU": "🇭🇺 匈牙利", "RO": "🇷🇴 罗马尼亚", "BG": "🇧🇬 保加利亚",
    "GR": "🇬🇷 希腊", "HR": "🇭🇷 克罗地亚", "SI": "🇸🇮 斯洛文尼亚",
    "RS": "🇷🇸 塞尔维亚", "CY": "🇨🇾 塞浦路斯", "MT": "🇲🇹 马耳他",
    "LU": "🇱🇺 卢森堡", "IS": "🇮🇸 冰岛",
    "UM": "🇺🇲 美国外岛",
}

# 关键字兜底 (正则, 国家码)。先匹配长词/中文, 最后才用二位码。
KEYWORDS = [
    (r"香港|hong[\s+_\-]*kong", "HK"),
    (r"澳门|澳門|macau|macao", "MO"),
    (r"台湾|台灣|taiwan", "TW"),
    (r"日本|japan|东京|東京|大阪", "JP"),
    (r"韩国|韓國|korea|首尔|首爾", "KR"),
    (r"新加坡|singapore|狮城|獅城", "SG"),
    (r"美国|美國|united[\s+_\-]*states|洛杉矶|洛杉磯|圣何塞|聖何塞|西雅图|西雅圖|纽约|紐約|达拉斯|凤凰城|硅谷|迈阿密|芝加哥|拉斯维加斯", "US"),
    (r"加拿大|canada|多伦多|多倫多", "CA"),
    (r"墨西哥|mexico", "MX"),
    (r"英国|英國|united[\s+_\-]*kingdom|london|伦敦|倫敦|曼彻斯特", "GB"),
    (r"德国|德國|germany|法兰克福|法蘭克福", "DE"),
    (r"法国|法國|france|巴黎", "FR"),
    (r"荷兰|荷蘭|netherlands|阿姆斯特丹", "NL"),
    (r"瑞士|switzerland", "CH"),
    (r"瑞典|sweden", "SE"),
    (r"芬兰|芬蘭|finland", "FI"),
    (r"挪威|norway", "NO"),
    (r"丹麦|丹麥|denmark", "DK"),
    (r"波兰|波蘭|poland", "PL"),
    (r"乌克兰|烏克蘭|ukraine", "UA"),
    (r"俄罗斯|俄羅斯|russia|莫斯科", "RU"),
    (r"意大利|italy", "IT"),
    (r"西班牙|spain", "ES"),
    (r"葡萄牙|portugal", "PT"),
    (r"爱尔兰|愛爾蘭|ireland", "IE"),
    (r"奥地利|奧地利|austria", "AT"),
    (r"比利时|比利時|belgium", "BE"),
    (r"捷克|czech", "CZ"),
    (r"罗马尼亚|羅馬尼亞|romania", "RO"),
    (r"土耳其|turkey", "TR"),
    (r"以色列|israel", "IL"),
    (r"阿联酋|阿聯酋|uae|dubai|迪拜", "AE"),
    (r"沙特|沙烏地|saudi", "SA"),
    (r"印度|india|孟买|孟買", "IN"),
    (r"印尼|印度尼西亚|印度尼西亞|indonesia", "ID"),
    (r"越南|vietnam", "VN"),
    (r"泰国|泰國|thailand", "TH"),
    (r"马来西亚|馬來西亞|malaysia", "MY"),
    (r"菲律宾|菲律賓|philippines", "PH"),
    (r"巴基斯坦|pakistan", "PK"),
    (r"澳大利亚|澳大利亞|australia|悉尼", "AU"),
    (r"新西兰|紐西蘭|new[\s+_\-]*zealand", "NZ"),
    (r"南非|south[\s+_\-]*africa", "ZA"),
    (r"巴西|brazil", "BR"),
    (r"阿根廷|argentina", "AR"),
    (r"智利|chile", "CL"),
    (r"中国|中國|china|大陆|大陸", "CN"),
    (r"伊朗|iran|tehran|德黑兰", "IR"),
    (r"哈萨克|kazakh|阿拉木图", "KZ"),
    (r"乌兹别克|uzbek|塔什干", "UZ"),
    (r"阿塞拜疆|azerbaijan|巴库", "AZ"),
    (r"格鲁吉亚|georgia|第比利斯", "GE"),
    (r"亚美尼亚|armenia|埃里温", "AM"),
    (r"蒙古|mongolia|乌兰巴托", "MN"),
    (r"尼泊尔|nepal|加德满都", "NP"),
    (r"孟加拉|bangladesh|达卡", "BD"),
    (r"斯里兰卡|sri[\s+_\-]*lanka|科伦坡", "LK"),
    (r"缅甸|myanmar|仰光", "MM"),
    (r"柬埔寨|cambodia|金边", "KH"),
    (r"老挝|laos|万象", "LA"),
    (r"文莱|brunei", "BN"),
    (r"爱沙尼亚|estonia|塔林", "EE"),
    (r"拉脱维亚|latvia|里加", "LV"),
    (r"立陶宛|lithuania|维尔纽斯", "LT"),
    (r"摩尔多瓦|moldova", "MD"),
    (r"白俄罗斯|belarus|明斯克", "BY"),
    (r"捷克|czech|布拉格", "CZ"),
    (r"斯洛伐克|slovakia|布拉迪斯拉发", "SK"),
    (r"匈牙利|hungary|布达佩斯", "HU"),
    (r"保加利亚|bulgaria|索菲亚", "BG"),
    (r"希腊|greece|雅典", "GR"),
    (r"克罗地亚|croatia|萨格勒布", "HR"),
    (r"斯洛文尼亚|slovenia|卢布尔雅那", "SI"),
    (r"塞尔维亚|serbia|贝尔格莱德", "RS"),
    (r"塞浦路斯|cyprus|尼科西亚", "CY"),
    (r"马耳他|malta", "MT"),
    (r"卢森堡|luxembourg", "LU"),
    (r"冰岛|iceland|雷克雅未克", "IS"),
]

# 二位码兜底匹配(带单词边界)。分两池:
#   BARE_SAFE  -> 不大小写敏感(如 hk/jp/sg, 拼在长名里也认)
#   BARE_UPPER -> 只认大写, 因为这些码是小写英文常用词(us/in/it/at/no/my/is...), 小写会误伤
_BARE_SAFE = [
    "HK", "TW", "MO", "JP", "KR", "SG", "CA", "GB", "UK", "DE", "FR", "NL",
    "CH", "SE", "FI", "DK", "PL", "UA", "RU", "ES", "PT", "IE", "CZ", "RO",
    "TR", "IL", "AE", "SA", "VN", "TH", "PH", "PK", "AU", "NZ", "ZA", "BR",
    "AR", "CL", "MX", "IR", "KZ", "UZ", "AZ", "MN", "NP", "BD", "LK", "KH",
    "BN", "EE", "LV", "LT", "MD", "BY", "SK", "HU", "HR", "SI", "RS", "GR",
    "CY", "MT", "LU", "IS",
]
_BARE_UPPER = [
    "US", "IT", "IN", "ID", "AT", "AM", "GE", "MM", "LA",
]
_CODE_RE_CI = re.compile(
    r"(?<![A-Za-z])(" + "|".join(sorted(_BARE_SAFE)) + r")(?![A-Za-z])", re.I)
_CODE_RE_UC = re.compile(
    r"(?<![A-Za-z])(" + "|".join(sorted(_BARE_UPPER)) + r")(?![A-Za-z])")


# ======================================================================
# 终端
# ======================================================================
COLOR = True
LOGFILE = None


def _init_color():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        os.system("")


def paint(s, color):
    return f"{color}{s}{k.C.RESET}" if COLOR else str(s)


def log(msg=""):
    line = time.strftime("%H:%M:%S ") + str(msg)
    print(line, flush=True)
    if LOGFILE:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def warn(msg):
    log("  " + paint("⚠ ", k.C.YELLOW) + str(msg))


def ok(msg):
    log("  " + paint("✓ ", k.C.GREEN) + str(msg))


# ======================================================================
# 地区识别
# ======================================================================
def _flag_code(name: str):
    """从名字里解出国旗 emoji 对应的二位国家码(区域指示符 -> A-Z)。"""
    n = len(name)
    for i in range(n - 1):
        a, b = ord(name[i]), ord(name[i + 1])
        if 0x1F1E6 <= a <= 0x1F1FF and 0x1F1E6 <= b <= 0x1F1FF:
            return chr(a - 0x1F1E6 + ord("A")) + chr(b - 0x1F1E6 + ord("A"))
    return None


def detect_region(name: str) -> str:
    """返回地区分组显示名; 识别不到为 🌐 其他。"""
    if not name:
        return OTHER
    code = _flag_code(name)
    if code:
        return REGION_NAMES.get(code, code)
    for pat, c in KEYWORDS:
        if re.search(pat, name, re.I):
            return REGION_NAMES.get(c, c)
    m = _CODE_RE_CI.search(name) or _CODE_RE_UC.search(name)
    if m:
        return REGION_NAMES.get(m.group(1).upper(), OTHER)
    return OTHER


# ======================================================================
# GeoIP —— 按节点服务器 IP(或域名解析出的 IP)判断国家, 比名字可靠
# ======================================================================
GEOIP_CACHE = os.path.join(CACHE, "geoip_cache.json")
GEOIP_DB = os.path.join(CACHE, "geoip_country_ipv4.csv")
GEOIP_DB_URLS = [
    "https://raw.githubusercontent.com/sapics/ip-location-db/main/geolite2/geolite2-country-ipv4.csv",
    "https://raw.githubusercontent.com/sapics/ip-location-db/main/dbip-country/dbip-country-ipv4.csv",
    "https://raw.githubusercontent.com/sapics/ip-location-db/main/geo-whois-asn-country/geo-whois-asn-country-ipv4.csv",
]


def _ip2int(s):
    """点分 IPv4 或已转成整数的字符串 -> int; 失败返回 None。"""
    s = s.strip().strip('"')
    if not s:
        return None
    try:
        if "." in s:
            parts = s.split(".")
            if len(parts) != 4:
                return None
            v = 0
            for p in parts:
                n = int(p)
                if not 0 <= n <= 255:
                    return None
                v = (v << 8) | n
            return v
        return int(s)
    except ValueError:
        return None


class IPDB:
    """离线 GeoIP 库: CSV(起始IP,结束IP,国家码) -> 二分查找。"""

    def __init__(self, starts, ends, codes):
        self.starts, self.ends, self.codes = starts, ends, codes
        self.size = len(starts)

    @classmethod
    def load(cls, path):
        rows = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] == "#":
                    continue
                parts = line.split(",")
                if len(parts) < 3:
                    continue
                a, b = _ip2int(parts[0]), _ip2int(parts[1])
                cc = parts[2].strip()
                if a is None or b is None or not cc:
                    continue
                rows.append((a, b, cc))
        rows.sort(key=lambda r: r[0])
        return cls([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])

    def lookup(self, ip):
        v = _ip2int(ip)
        if v is None or not self.starts:
            return None
        i = bisect.bisect_right(self.starts, v) - 1
        if i >= 0 and v <= self.ends[i]:
            return self.codes[i]
        return None


def _fetch_geoip_db(url):
    """用 k 的多镜像回退下载离线库。"""
    for u in _candidates(url):
        try:
            data = k._http_get(u, timeout=60)
            if data and len(data) > 100000:
                how = "直连" if u == url else ("镜像:" + u.split("/")[2])
                return data, how
        except Exception:
            continue
    return None, "失败"


def ensure_geoip_db(force=False):
    """确保离线库存在并载入, 返回 IPDB 或 None。"""
    ok_file = os.path.exists(GEOIP_DB) and os.path.getsize(GEOIP_DB) > 1000
    if force or not ok_file:
        for url in GEOIP_DB_URLS:
            data, how = _fetch_geoip_db(url)
            if data:
                with open(GEOIP_DB, "wb") as f:
                    f.write(data)
                log(f"  GeoIP 离线库已下载 ({how}, {len(data)/1024/1024:.1f} MB)")
                break
        else:
            warn("GeoIP 离线库下载失败, 将改用在线 API")
            return None
    try:
        t = time.time()
        db = IPDB.load(GEOIP_DB)
        log(f"  离线 GeoIP 库就绪: {db.size} 条, 载入 {time.time()-t:.1f}s")
        return db
    except Exception as e:
        warn(f"GeoIP 离线库解析失败: {e}")
        return None


def _is_ip(s):
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _resolve_many(servers, workers=48, deadline=25.0):
    """并发把域名解析成 IP, 已是 IP 的跳过。返回 {server: ip}。"""
    need = [s for s in servers if not _is_ip(s)]
    out = {}
    if not need:
        return out

    def _go(s):
        try:
            return s, socket.gethostbyname(s)
        except Exception:
            return s, None

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futs = [ex.submit(_go, s) for s in need]
    try:
        for f in concurrent.futures.as_completed(futs, timeout=deadline):
            try:
                s, ip = f.result()
            except Exception:
                continue
            if ip:
                out[s] = ip
    except Exception:
        pass
    ex.shutdown(wait=False)
    return out


def _ipapi_batch(ips):
    """ip-api.com 批量查询(免费, 100 个/次)。返回 {ip: 国家码}。"""
    res = {}
    url = "http://ip-api.com/batch?fields=status,countryCode,query"
    for i in range(0, len(ips), 100):
        chunk = ips[i:i + 100]
        try:
            req = urllib.request.Request(
                url, data=json.dumps([{"query": ip} for ip in chunk]).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                arr = json.loads(r.read().decode("utf-8"))
            for item in arr:
                q, cc = item.get("query"), item.get("countryCode")
                if q and cc:
                    res[q] = cc.upper()
        except Exception:
            continue
    return res


def _country_is(ip):
    """备用 GeoIP(api.country.is, 单个查询)。"""
    try:
        req = urllib.request.Request(f"https://api.country.is/{ip}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            cc = json.loads(r.read().decode("utf-8")).get("country")
        return cc.upper() if cc else None
    except Exception:
        return None


def _country_is_many(ips, workers=24):
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for ip, cc in zip(ips, ex.map(_country_is, ips)):
            if cc:
                out[ip] = cc
    return out


def geoip_countries(servers, db=None):
    """输入 server 列表(域名或 IP), 返回 {server: 国家码}。
    有离线库先本地查, 查不到再走在线 API。命中结果写入本地缓存。"""
    cache = {}
    if os.path.exists(GEOIP_CACHE):
        try:
            with open(GEOIP_CACHE, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    uniq = list({s for s in servers if s})
    todo = [s for s in uniq if s not in cache]
    if todo:
        resolved = _resolve_many(todo)
        online = {}          # ip -> [servers]
        for s in todo:
            ip = s if _is_ip(s) else resolved.get(s)
            if not ip:
                continue
            cc = db.lookup(ip) if db else None
            if cc:
                cache[s] = cc
            else:
                online.setdefault(ip, []).append(s)
        if online:
            ips = list(online.keys())
            got = _ipapi_batch(ips)
            miss = [ip for ip in ips if ip not in got]
            if miss:
                got.update(_country_is_many(miss))
            for ip, cc in got.items():
                for s in online.get(ip, []):
                    cache[s] = cc
        try:
            with open(GEOIP_CACHE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=0)
        except Exception:
            pass
    return {s: cache.get(s) for s in servers}


def apply_geoip(merged, db=None):
    """优先用 GeoIP 结果定位地区; 查不到再退回名字识别。返回统计。"""
    geo = geoip_countries([it["proxy"]["server"] for it in merged], db=db)
    stats = {"ip": 0, "name": 0}
    for it in merged:
        code = geo.get(it["proxy"]["server"])
        if code:
            it["region"] = REGION_NAMES.get(code, code)
            stats["ip"] += 1
        else:
            it["region"] = detect_region(it["name"]) or OTHER
            stats["name"] += 1
    return stats


# ======================================================================
# 拉取
# ======================================================================
def _candidates(url):
    """直连 URL + 各镜像(基于 k.MIRROR_TEMPLATES)。"""
    seen = set()
    yield url
    seen.add(url)
    prefix = "https://raw.githubusercontent.com/"
    if not url.startswith(prefix):
        return
    parts = url[len(prefix):].split("/")
    if len(parts) >= 4 and parts[1] == "refs" and parts[2] == "heads":
        owner, branch, path = parts[0], parts[3], "/".join(parts[4:])
    elif len(parts) >= 3:
        owner, branch, path = parts[0], parts[1], "/".join(parts[2:])
    else:
        return
    raw = f"{owner}/{branch}/{path}"
    gh = f"{owner}@{branch}/{path}"
    for tpl in k.MIRROR_TEMPLATES:
        u = tpl.format(raw=raw, gh=gh)
        if u not in seen:
            seen.add(u)
            yield u


def fetch_source(tag, url, skip_fetch):
    """返回 (bytes|None, 说明)。命中镜像会写入 cache/<tag>.raw。"""
    dest = os.path.join(CACHE, tag + ".raw")
    if skip_fetch and os.path.exists(dest) and os.path.getsize(dest) > 100:
        with open(dest, "rb") as f:
            return f.read(), "cache"
    last = ""
    for u in _candidates(url):
        try:
            data = k._http_get(u, timeout=25)
            if data and len(data) > 100:
                with open(dest, "wb") as f:
                    f.write(data)
                how = "直连" if u == url else ("镜像:" + u.split("/")[2])
                return data, how
            last = "内容过短"
        except Exception as e:
            last = type(e).__name__
    return None, last


# ======================================================================
# 解析
# ======================================================================
def _cred_key(p):
    return f"{p.get('uuid', '')}|{p.get('password', '')}"


def _dedupe_key(p):
    return "|".join([
        str(p.get("type", "")).lower(),
        str(p.get("server", "")).lower(),
        str(p.get("port", "")),
        _cred_key(p),
    ])


def _accept(p):
    """校验一个 Clash proxy dict 是否可用。"""
    if not isinstance(p, dict):
        return False
    if not p.get("name") or not p.get("server") or not p.get("port"):
        return False
    srv = str(p["server"])
    if any(ch in srv for ch in " /\t") or srv.lower() in ("127.0.0.1", "localhost"):
        return False
    try:
        p["port"] = int(p["port"])
    except Exception:
        return False
    return 0 < p["port"] <= 65535


def parse_links(raw):
    """base64/明文 share link -> Clash proxy dict 列表, 附带原始名字。"""
    out = []
    for link in k.extract_lines(raw):
        n = k.parse_link(link)
        if not n or not n.get("address") or not n.get("port"):
            continue
        name = n.get("name") or f"{n['address']}:{n['port']}"
        p = k.to_clash(n, name)
        if p is None:
            continue
        out.append((p, name))
    return out


def parse_yaml_proxies(raw):
    """现成 Clash.Meta YAML -> (proxy dict, 原始名字) 列表。"""
    try:
        data = yaml.safe_load(raw)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for p in (data.get("proxies") or []):
        if isinstance(p, dict):
            out.append((p, str(p.get("name", ""))))
    return out


# ======================================================================
# 合并 + 生成
# ======================================================================
def load_all(skip_fetch):
    seen = set()
    merged = []          # [{"proxy":dict, "region":str, "name":str}]
    per_source = {}
    for tag, fmt, url in SOURCES:
        raw, how = fetch_source(tag, url, skip_fetch)
        if not raw:
            warn(f"{tag:<12} 拉取失败({how}), 跳过")
            per_source[tag] = 0
            continue
        if fmt == "yaml":
            items = parse_yaml_proxies(raw)
        else:
            items = parse_links(raw)
        kept = 0
        for p, name in items:
            if not _accept(p):
                continue
            key = _dedupe_key(p)
            if key in seen:
                continue
            seen.add(key)
            region = detect_region(name) or OTHER
            merged.append({"proxy": p, "region": region, "name": name})
            kept += 1
        per_source[tag] = kept
        log(f"  {tag:<12} {how:<24} {len(raw)/1024:7.0f} KB  保留 {paint(kept, k.C.GREEN)}")
    return merged, per_source


def uniquify_names(merged):
    """保证 proxy 名全局唯一且 <=80 字符。"""
    used = set()
    for item in merged:
        base = (item["name"] or f"{item['proxy']['server']}:{item['proxy']['port']}")[:80]
        name = base
        i = 2
        while name in used:
            suffix = f"-{i}"
            name = base[: 80 - len(suffix)] + suffix
            i += 1
        used.add(name)
        item["proxy"]["name"] = name
        item["name"] = name


def build_config(merged, min_region):
    """生成 Clash.Meta 配置 + 地区统计。"""
    proxies = [it["proxy"] for it in merged]
    all_names = [it["proxy"]["name"] for it in merged]

    regions = {}
    for it in merged:
        regions.setdefault(it["region"], []).append(it["proxy"]["name"])

    # 其他 永远排最后; 其余按节点数降序
    order = sorted(regions.keys(), key=lambda r: (r == OTHER, -len(regions[r])))
    region_groups = []
    region_stats = {}
    for r in order:
        members = regions[r]
        region_stats[r] = len(members)
        if len(members) < min_region:
            continue
        region_groups.append({
            "name": r, "type": "url-test", "url": k.SCREEN_URL,
            "interval": 300, "tolerance": 50, "proxies": members,
        })

    group_auto = {
        "name": k.GROUP_AUTO, "type": "url-test", "url": k.SCREEN_URL,
        "interval": 300, "tolerance": 50, "proxies": list(all_names),
    }
    group_fallback = {
        "name": k.GROUP_FALLBACK, "type": "fallback", "url": k.SCREEN_URL,
        "interval": 300, "proxies": list(all_names),
    }
    group_select = {
        "name": k.GROUP_SELECT, "type": "select",
        "proxies": [k.GROUP_AUTO, k.GROUP_FALLBACK]
                   + [g["name"] for g in region_groups] + ["DIRECT"],
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
        "proxy-groups": [group_select, group_auto, group_fallback] + region_groups,
        "rules": [
            "GEOIP,LAN,DIRECT,no-resolve",
            "GEOIP,CN,DIRECT",
            f"MATCH,{k.GROUP_SELECT}",
        ],
    }
    return cfg, region_stats


# ======================================================================
# 可用性验证 (TCP 预筛 + mihomo 真连)
# ======================================================================
def verify_nodes(merged, mihomo_exe=None, timeout_ms=5000, conc=64):
    """先用 socket 做便宜的 TCP 预筛, 再用 mihomo 逐个真连 google generate_204。
    返回 (存活节点列表[按延迟升序], {mihomo名: 延迟ms})。"""
    total = len(merged)
    probe = [{"address": it["proxy"]["server"], "port": int(it["proxy"]["port"])}
             for it in merged]
    alive = k.tcp_screen(probe, timeout=2.0, conc=400)
    keys = {(a["address"], int(a["port"])) for a in alive}
    merged = [it for it in merged
              if (it["proxy"]["server"], int(it["proxy"]["port"])) in keys]
    ok(f"TCP 预筛: {paint(len(merged), k.C.GREEN)}/{total} 端口可连通")
    if not merged:
        return [], {}

    exe = k.ensure_mihomo(mihomo_exe)
    mh = k.Mihomo(exe)
    port = mh.ensure_port()
    os.makedirs(k.RUNDIR, exist_ok=True)
    path = os.path.join(k.RUNDIR, "config_verify.yaml")
    k.dump_yaml(k.build_test_config([it["proxy"] for it in merged],
                                    controller_port=port), path)
    mh.start(path)
    try:
        names = [it["proxy"]["name"] for it in merged]
        delays = mh.delay_many(names, k.SCREEN_URL, timeout_ms, conc, label="真连验证")
    finally:
        mh.stop()

    alive_items = []
    for it in merged:
        d = delays.get(it["proxy"]["name"], -1)
        if d and d > 0:
            it["delay"] = d
            alive_items.append(it)
    alive_items.sort(key=lambda x: x["delay"])
    ok(f"mihomo 真连验证: {paint(len(alive_items), k.C.GREEN)}/{len(merged)} 真实可用")
    return alive_items, {it["proxy"]["name"]: it["delay"] for it in alive_items}


# ======================================================================
# 主流程
# ======================================================================
def main():
    global COLOR, LOGFILE
    _init_color()
    ap = argparse.ArgumentParser(
        description="合并上游订阅并按地区分组, 生成一份 Clash.Meta 订阅",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--skip-fetch", action="store_true", help="复用 cache/*.raw, 不重新联网抓源")
    ap.add_argument("--no-geoip", action="store_true", help="不按 IP 做 GeoIP, 只用节点名识别地区")
    ap.add_argument("--update-geoip-db", action="store_true", help="强制重新下载 GeoIP 离线库")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出订阅文件")
    ap.add_argument("--min-region", type=int, default=1, help="地区节点数少于该值就不单独建分组")
    ap.add_argument("--max-nodes", type=int, default=0, help="合并后最多保留节点数(0=不限)")
    ap.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    ap.add_argument("--no-verify", action="store_true",
                    help="跳过可用性验证(默认做 TCP 预筛 + mihomo 真连)")
    ap.add_argument("--mihomo", default=None, help="指定 mihomo 内核路径(默认自动探测/下载)")
    ap.add_argument("--delay-timeout", type=int, default=5000,
                    help="单节点真连验证超时(毫秒)")
    ap.add_argument("--verify-conc", type=int, default=64, help="真连验证并发数")
    args = ap.parse_args()
    COLOR = not args.no_color and sys.stdout.isatty()

    os.makedirs(OUTDIR, exist_ok=True)
    os.makedirs(CACHE, exist_ok=True)
    LOGFILE = os.path.join(k.LOGDIR, "region_" + time.strftime("%Y%m%d_%H%M%S") + ".log")
    os.makedirs(k.LOGDIR, exist_ok=True)

    t0 = time.time()
    log(paint("=== 合并上游订阅 + 按地区分组 ===", k.C.BOLD + k.C.MAGENTA)
        + f"  日志: {os.path.basename(LOGFILE)}")

    log(f"[1/5] 拉取 {len(SOURCES)} 个上游订阅" + ("(复用缓存)" if args.skip_fetch else ""))
    merged, per_source = load_all(args.skip_fetch)
    if not merged:
        warn("没有任何源解析出可用节点, 不覆盖已有结果, 退出。")
        return 1

    if args.max_nodes and len(merged) > args.max_nodes:
        merged = merged[:args.max_nodes]
    uniquify_names(merged)
    ok(f"共 {paint(len(merged), k.C.GREEN)} 个去重节点")

    vstats = {"enabled": not args.no_verify, "tcp_total": len(merged),
              "alive": len(merged)}
    if args.no_verify:
        log("[2/5] 跳过可用性验证 (--no-verify)")
    else:
        log("[2/5] 可用性验证 (TCP 预筛 + mihomo 真连)")
        try:
            merged, _vdelays = verify_nodes(
                merged, args.mihomo, args.delay_timeout, args.verify_conc)
            vstats["alive"] = len(merged)
        except Exception as e:
            warn(f"验证失败({e}), 跳过验证并保留全部节点")
            vstats["enabled"] = False
            vstats["error"] = str(e)
        if not merged:
            warn("验证后无可用节点, 不覆盖已有结果, 退出。")
            return 1

    if args.no_geoip:
        log("[3/5] 按节点名归类 (--no-geoip)")
        for it in merged:
            it["region"] = detect_region(it["name"]) or OTHER
        gstats = {"ip": 0, "name": len(merged)}
    else:
        log("[3/5] 按服务器 IP 做 GeoIP 归类(名字兜底)")
        db = ensure_geoip_db(force=args.update_geoip_db)
        gstats = apply_geoip(merged, db=db)
        ok(f"IP 命中 {paint(gstats['ip'], k.C.GREEN)} / 名字兜底 {gstats['name']}")

    log("[4/5] 建组统计")
    cfg, region_stats = build_config(merged, args.min_region)
    for r, c in sorted(region_stats.items(), key=lambda kv: (kv[0] == OTHER, -kv[1])):
        log(f"    {r:<12} {c}")

    log("[5/5] 写出订阅")
    k.dump_yaml(cfg, args.out)
    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_nodes": len(merged),
        "sources": per_source,
        "regions": region_stats,
        "classify": gstats,
        "verify": vstats,
    }
    with open(META_OUT, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    ok(f"订阅文件: {args.out}")
    ok(f"统计文件: {META_OUT}")
    log(f"  分组: {k.GROUP_SELECT} / {k.GROUP_AUTO} / {k.GROUP_FALLBACK}"
        f" + {len(cfg['proxy-groups']) - 3} 个地区分组")
    log(f"  耗时 {time.time()-t0:.0f}s。在 Karing 里「添加订阅 → 从文件导入」选上面这个 yaml 即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
