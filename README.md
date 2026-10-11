# Karing 免费节点一键刷新工具

把「找 GitHub 免费节点 → 筛选 → 测速 → 生成一份**可重复更新的订阅**」整条流程固化成脚本,
专门为 [Karing](https://karing.app) 这类兼容 Clash.Meta / sing-box / V2ray 的客户端设计。

和旧版(v2rayN 版)最大的不同:**不再往 v2rayN 的数据库里塞节点**,
而是产出一份 Clash.Meta 订阅 + 一个本地订阅服务,Karing 里点「更新订阅」就能一直拿到最新结果。

## 快速开始

**方式一:跑一次,拿到订阅文件(最省事)**

双击 `刷新免费节点.bat`(约 3~15 分钟)。结束后:

- 订阅文件:`output\karing.yaml`
- 通用 base64 订阅:`output\sub_base64.txt`

在 Karing 里 **添加订阅 → 从文件导入**,选 `output\karing.yaml` 即可(以后重跑脚本再导入一次)。

**方式二:常驻订阅服务,得到一条固定链接(推荐)**

双击 `启动订阅服务.bat`。窗口会打印一条链接,例如:

```
Clash.Meta 订阅 : http://127.0.0.1:20808/karing.yaml
通用 base64 订阅: http://127.0.0.1:20808/sub
仪表盘          : http://127.0.0.1:20808/
```

在 Karing 里 **添加订阅 → 输入链接**,粘贴上面的订阅地址。之后:

- Karing 里点「更新订阅」→ 立刻拿到脚本最近一次筛选的结果;
- 服务每 30 分钟自动重跑一遍(可用 `--interval` 调整),链接**始终不变**;
- 打开仪表盘页面可以看可用节点数、延迟/速度排行,并一键复制订阅地址。

> 手机/其它设备要用的话,把监听地址改成局域网:`--host 0.0.0.0`,
> 再用本机局域网 IP 替换 `127.0.0.1`。

## 为什么是「订阅」而不是写数据库

Karing 是 Clash.Meta / sing-box 系客户端,天然支持订阅 URL,并且支持 `url-test` / `fallback` 分组
**自己在本地自动选最快、自动故障转移**。所以脚本不需要像以前那样"硬挑一个节点再重启客户端激活":

1. 脚本只负责**筛出真实可用的节点**(这才是免费节点里最难的部分);
2. 生成的订阅里包含:
   - `🎯 自动选择`(url-test,每 300s 自动测延迟选最快)
   - `♻️ 故障转移`(fallback,当前节点挂了自动换下一个)
   - `🚀 节点选择`(手动选择,默认走"自动选择")
3. 选哪个节点交给 Karing 动态决定,比脚本写死一个更稳。

这是比"生成一次性文件"更好的方案:节点波动时,Karing 端自动换,不用重跑脚本。

## 它做什么

1. **拉取**:并发从多个每日更新的 GitHub 免费节点仓库抓取(`ghproxy.net` / `gh-proxy.com` /
   `ghfast.top` / `jsdelivr` 等多镜像依次容错,本机被墙也能拉到)。
2. **解析去重**:支持 vmess / vless / trojan / ss / hysteria2 / tuic;自动剔除非法参数
   (reality 假密钥、ss2022 坏密钥等)。
3. **TCP 预筛**:纯 socket 高并发(默认 400 并发)快速踢掉大量根本连不通的 IP,给后面省时间。
4. **真实可用性验证**:用 mihomo 内核测 `google generate_204`,
   把"能通 TCP 但出口屏蔽 google"的假活节点剔掉。
5. **下载测速**:对延迟最低的前 N 个,通过 mihomo 独立端口从 Cloudflare 下载测速,多轮取最优。
6. **生成订阅**:按 速度/延迟 评分排序,产出 Clash.Meta 订阅(`url-test`/`fallback` 分组)+
   base64 通用订阅 + 明文 share link + `meta.json`(统计/排查用)。

## 常用参数

```bat
python karing_free_refresh.py                    :: 完整刷新一次
python karing_free_refresh.py --serve            :: 常驻服务 + 定时刷新(推荐)
python karing_free_refresh.py --serve --interval 900
python karing_free_refresh.py --skip-fetch       :: 复用 cache/,不重新联网抓源(快很多)
python karing_free_refresh.py --limit 3000       :: 只解析前 3000 个去重节点(快速跑)
python karing_free_refresh.py --top 200          :: 只对延迟最低的前 200 个测速
python karing_free_refresh.py --fast-ms 300      :: 「快速」标记的延迟阈值,默认 300ms
python karing_free_refresh.py --host 0.0.0.0     :: 让局域网/手机也能订阅
python karing_free_refresh.py --mihomo PATH      :: 手动指定 mihomo.exe
python karing_free_refresh.py --no-color        :: 关闭彩色输出
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `karing_free_refresh.py` | 主脚本(自包含) |
| `刷新免费节点.bat` | 双击:完整刷新一次,产出订阅文件 |
| `启动订阅服务.bat` | 双击:常驻订阅服务 + 每 30 分钟自动刷新 |
| `output/` | 产物:`karing.yaml`(Clash.Meta 订阅)、`sub_base64.txt`、`sub_plain.txt`、`meta.json` |
| `cache/` | 拉取的源文件缓存(`--skip-fetch` 复用) |
| `logs/` | 每次运行的日志 |
| `bin/` | 找不到 mihomo 时会自动下载到这里 |
| `rundir/` | mihomo 测试运行目录(自动生成) |
| `v2rayn_free_refresh.py` | **旧版**(写 v2rayN 数据库),已不再使用,保留备查 |

## 注意

- 免费节点波动极快(几分钟内上下线),脚本筛出的节点过一会儿也可能掉线。
  用「订阅服务」模式时,Karing 的 `url-test` 会自动切到当时最快的存活节点;也可以手动点「更新订阅」。
- 公开免费节点本来就大部分是死的(通常 <1%~5% 可用),筛出几个能用就很正常。
- 想加/换节点源:编辑 `karing_free_refresh.py` 顶部的 `SOURCES` 列表(格式 `仓库/分支/文件`)。
  想加镜像:编辑 `MIRROR_TEMPLATES`。
- 依赖:Python 3.10+、`pip install pyyaml`;mihomo 内核会自动复用本机客户端的,找不到则自动下载到 `bin/`。
- 只监听 `127.0.0.1` 时订阅仅本机可用;用 `--host 0.0.0.0` 暴露到局域网时请自行注意安全。
