#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把生成的订阅推送到 Cloudflare KV
=================================
读取本地产物 (默认 output/karing_region.yaml 与 output/karing_region_meta.json),
通过 Cloudflare REST API 写入 KV, 供 cloudflare/worker.js 对外提供订阅地址。

环境变量(在 GitHub Actions 里用 Secrets 注入):
    CF_ACCOUNT_ID        Cloudflare 账户 ID
    CF_KV_NAMESPACE_ID   KV 命名空间 ID
    CF_API_TOKEN         具有 "Workers KV Storage: Edit" 权限的 API Token
    CF_KV_KEY            (可选) 订阅在 KV 里的键名, 默认 karing_region.yaml

用法:
    python publish_cloudflare.py                     # 推订阅 + meta
    python publish_cloudflare.py --key sub.yaml --file output/xxx.yaml
    python publish_cloudflare.py --dry-run

说明:
    若缺少 CF_* 环境变量, 脚本打印提示并以 0 退出(不阻断 GitHub 备份提交)。
    仅依赖标准库, 无需额外安装。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"
BASE = os.path.dirname(os.path.abspath(__file__))
DEF_FILE = os.path.join(BASE, "output", "karing_region.yaml")
DEF_META = os.path.join(BASE, "output", "karing_region_meta.json")


def _load_dotenv(path):
    """从同目录 .env 读取 KEY=VALUE, 仅在环境变量尚未设置时填充。"""
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_dotenv(os.path.join(BASE, ".env"))


def _put(account, ns, key, data, token, attempts=4):
    """PUT 一个值到 KV(失败重试)。返回 (成功?, 说明)。"""
    url = f"{API}/accounts/{account}/storage/kv/namespaces/{ns}/values/{key}"
    last = ""
    for i in range(1, attempts + 1):
        req = urllib.request.Request(
            url, data=data, method="PUT",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            last = f"HTTP {e.code} {detail}"
            if 400 <= e.code < 500 and e.code != 429:
                return False, last          # 鉴权/参数错误, 重试没意义
        except Exception as e:
            last = f"{type(e).__name__}: {e}"   # 含 RemoteDisconnected / 超时
        else:
            try:
                j = json.loads(body)
                if j.get("success"):
                    return True, ("ok" if i == 1 else f"ok(第{i}次)")
                last = json.dumps(j.get("errors") or j, ensure_ascii=False)[:300]
            except Exception:
                return True, (body[:200] or "ok")
        if i < attempts:
            print(f"    连接抖动, 重试 {i}/{attempts - 1} ({last}) ...")
            time.sleep(2 * i)
    return False, last


def main():
    ap = argparse.ArgumentParser(description="推送订阅到 Cloudflare KV")
    ap.add_argument("--file", default=DEF_FILE, help="订阅文件")
    ap.add_argument("--meta-file", default=DEF_META, help="统计文件")
    ap.add_argument("--key", default=os.environ.get("CF_KV_KEY", "karing_region.yaml"),
                    help="订阅在 KV 里的键名")
    ap.add_argument("--meta-key", default="karing_region_meta.json",
                    help="统计在 KV 里的键名")
    ap.add_argument("--dry-run", action="store_true", help="只打印, 不上传")
    args = ap.parse_args()

    account = os.environ.get("CF_ACCOUNT_ID", "").strip()
    ns = os.environ.get("CF_KV_NAMESPACE_ID", "").strip()
    token = os.environ.get("CF_API_TOKEN", "").strip()

    if not os.path.exists(args.file):
        print(f"x 找不到订阅文件: {args.file}")
        return 1
    with open(args.file, "rb") as f:
        data = f.read()

    if not (account and ns and token):
        print("! 未配置 CF_ACCOUNT_ID / CF_KV_NAMESPACE_ID / CF_API_TOKEN, "
              "跳过上传 (GitHub 备份提交不受影响)。")
        return 0

    if args.dry_run:
        print(f"(dry-run) 将上传 {len(data)} 字节 -> KV[{args.key}] @ {account}/{ns}")
        return 0

    all_ok = True
    good, msg = _put(account, ns, args.key, data, token)
    print(("OK " if good else "ERR") + f" 上传 {args.key}: {msg}")
    all_ok = all_ok and good

    if os.path.exists(args.meta_file):
        with open(args.meta_file, "rb") as f:
            mdata = f.read()
        good, msg = _put(account, ns, args.meta_key, mdata, token)
        print(("OK " if good else "ERR") + f" 上传 {args.meta_key}: {msg}")
        all_ok = all_ok and good

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
