# 部署：GitHub Actions + Cloudflare 自动更新订阅

目标：让 `region_group.py` 在云端每 15 分钟自动跑一次，生成的订阅通过
**你自己的域名**对外提供，国内可直连、更新快。

```
GitHub Actions (每 15 分钟)                       Cloudflare
  region_group.py  拉取+合并+GeoIP   ──KV──►  Worker(/sub)  ◄── sub.你的域名
  publish_cloudflare.py  写入 KV               (只读转发, 60s 缓存)
  顺带把 yaml 提交回仓库(备份镜像: raw / jsdelivr)
```

为什么这么分：
- **重活放 Actions**：免费 Worker 只有 10ms CPU / 50 次子请求，跑不动拉取+合并+GeoIP；Actions 跑完整 Python 没有限制。
- **分发放 Cloudflare**：KV + Worker 在全球边缘，秒级；绑自己的域名国内能直连（`*.workers.dev` 在国内 100% 被墙）。
- **公开仓库**：Actions 免费不限量；还顺带避免“仓库 60 天不活动 → 定时任务被禁用”。

---

## 0. 前置条件

- 一个 **public** 的 GitHub 仓库（把这个目录推上去）。
- 一个 **已托管在 Cloudflare** 的域名（NS 指向 Cloudflare）。
- Cloudflare 账号。

---

## 1. 推到 GitHub

```bash
cd free_vpn
git init
git add .
git commit -m "init: karing region subscription"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

仓库必须设为 **Public**（Settings → General → Danger Zone → Change visibility）。

---

## 2. 建 KV 命名空间

Cloudflare 控制台 → **Workers & Pages → KV → Create namespace**，名字填 `karing-sub`。
创建后复制它的 **Namespace ID**（形如 `a1b2c3d4...`）。

---

## 3. 建 API Token

控制台右上角头像 → **My Profile → API Tokens → Create Token → Create Custom Token**：

- Name：`karing-sub-kv`
- Permissions：**Account → Workers KV Storage → Edit**
- Account Resources：选你的账号

生成后**立刻复制 token**（只显示一次）。
同时在控制台 URL 或账号概览里找到 **Account ID**。

---

## 4. 在仓库里配置 Secrets

仓库 → **Settings → Secrets and variables → Actions → New repository secret**，加三个：

| Secret 名 | 值 |
|---|---|
| `CF_ACCOUNT_ID` | 你的 Cloudflare Account ID |
| `CF_KV_NAMESPACE_ID` | 第 2 步的 KV Namespace ID |
| `CF_API_TOKEN` | 第 3 步的 API Token |

（可选）再加一个 `CF_KV_KEY` 可自定义 KV 键名，默认 `karing_region.yaml`。

---

## 5. 先手动跑一次

仓库 → **Actions → Update subscription → Run workflow**。
跑完后：

- 到 Cloudflare KV 里能看到 `karing_region.yaml`（几百 KB）。
- 仓库里 `output/karing_region.yaml` 也会被自动提交（备份镜像）。
- 之后每 15 分钟自动更新。

> 首次运行会下载 GeoIP 离线库（~10MB），稍慢；之后走缓存。

---

## 6. 部署 Worker

```bash
cd cloudflare
npm i -g wrangler      # 或直接用 npx wrangler
wrangler login
```

编辑 `cloudflare/wrangler.toml`：

1. 把 `id = "REPLACE_WITH_YOUR_KV_NAMESPACE_ID"` 换成第 2 步的 KV ID。
2. 把 `pattern = "sub.example.com"` 换成你自己的子域名（该域名须已在此 CF 账号下）。

然后部署：

```bash
wrangler deploy
```

`custom_domain = true` 会自动把这个子域名绑到 Worker 上。

---

## 7. 添加订阅

Karing → 添加订阅 → 填 URL：

```
https://sub.你的域名/sub
```

- `.../sub?b64=1` → 返回 base64 版本
- `.../meta` → 查看节点/地区统计
- `.../healthz` → 健康检查

---

## 8. 关于更新频率与漂移

- GitHub Actions 的 `schedule` 是**尽力而为**：高峰期（尤其整点）会延迟，偶尔也会整次跳过。已用带偏移的 cron（`7,22,37,52`）缓解，但不要期望“精确 15 分钟”。实际通常 1 小时内会刷新到。
- Worker 与 KV 设了 **60 秒缓存**，所以一旦 KV 更新，约 1 分钟内对外生效。
- 想更稳/更快（进阶）：可在 Cloudflare 用一个 1 分钟的 Cron Worker 去调 GitHub 的 `repository_dispatch` 触发本工作流。

---

## 常见问题

- **国内打不开 `https://xxx.workers.dev/sub`**：正常，`workers.dev` 被墙。必须用第 6 步的自定义域名。
- **刚上线域名偶尔慢**：新的域名/子域首次走 SNI 可能较慢，用几天后会稳定。
- **改了订阅地址会不会变**：不会，Worker 每次从 KV 读最新值，地址恒定。
- **想换 KV 键名**：改 workflow 里的 `CF_KV_KEY` 与 `wrangler.toml` 的 `SUB_KEY`，保持一致即可。
- **不想用 Cloudflare 时**：只跑到第 5 步即可，仓库里的 `output/karing_region.yaml` 也能用（用 raw 代理/jsdelivr 访问，但国内不稳，仅作兜底）。
