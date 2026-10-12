# Zampto Auto Renew（本地 / Docker 运行）

本仓库包含：
- `zampto_renew.py` — 自动续期脚本（配置全部走环境变量）
- `parse_proxy.py` — 节点链接解析器（生成 sing-box 配置，支持全部主流代理协议）
- `parse_mihomo.py` — ss:// 节点解析器（生成 mihomo 配置，原生支持 Shadowsocks+obfs，用于机场节点）
- `.github/workflows/zampto_renew.yml` — GitHub Actions 定时运行（内置 sing-box，自动下载）
- `Dockerfile` — 容器化本地运行

---

## 一、配置（环境变量）

| 变量 | 必填 | 说明 |
|------|------|------|
| `ZAMPTO_ACCOUNT` | 是 | 登录邮箱 |
| `ZAMPTO_PASSWORD` | 是 | 登录密码 |
| `TARGET_SERVERS` | 是 | JSON 数组，如 `[{"id":"4480","name":"java"},{"id":"4481","name":"python"}]` |
| `GOST_PROXY` | 否 | 上游节点链接，支持 **vless:// vmess:// trojan:// hysteria2:// (hy2://) tuic:// anytls:// socks5:// socks:// http:// https://**；不设则直连 |
| `TG_BOT` | 否 | Telegram 通知，`token:chatid` 格式 |

`TARGET_SERVERS` 也可用组合形式替代：
- `TARGET_IDS=4480,4481`
- `TARGET_NAMES=java,python`（缺省时以 id 作为名字）

> `GOST_PROXY` 名称保留兼容；支持两类输入：
> - 通用节点链接（vmess/vless/trojan/hy2 等）→ `parse_proxy.py` 生成 sing-box 配置
> - ss:// 节点链接（Shadowsocks，如机场节点）→ `parse_mihomo.py` 生成 mihomo 配置
> 两者都在 `127.0.0.1:1080` 起一个 mixed 入口（socks5+http 通用），脚本与 SeleniumBase 都走它。
> ⚠️ 注意：Zampto 会封数据中心 IP（GitHub Actions 的 Azure IP 会被 `/blocked` 拦截），
> 必须使用住宅/运营商 IP 出口（如台湾中华电信等），机场的普通机房节点不可用。

---

## 二、本地直接运行（需本机有 Chrome + Xvfb）

### 1. 安装系统依赖（Ubuntu/Debian）
```bash
sudo apt-get update -qq
sudo apt-get install -y google-chrome-stable xvfb x11-utils xdotool scrot
pip install seleniumbase
seleniumbase install chromedriver
```

### 2. 设置环境变量并运行
```bash
export ZAMPTO_ACCOUNT="your@email.com"
export ZAMPTO_PASSWORD="your_password"
export TARGET_SERVERS='[{"id":"4480","name":"java"},{"id":"4481","name":"python"}]'
# export GOST_PROXY="socks5://..."   # 可选
# export TG_BOT="123456:ABC-DEF:chatid"  # 可选

xvfb-run --auto-servernum --server-args="-screen 0 1920x1080x24" python zampto_renew.py
```

> Windows 本机没有 Xvfb，可改用 WSL2（Ubuntu）或 Docker 方式运行。

---

## 三、Docker 运行（推荐，跨平台）

### 1. 构建镜像
```bash
docker build -t zampto-renew .
```

### 2. 运行（环境变量通过 -e 传入）
```bash
docker run --rm \
  -e ZAMPTO_ACCOUNT="your@email.com" \
  -e ZAMPTO_PASSWORD="your_password" \
  -e TARGET_SERVERS='[{"id":"4480","name":"java"},{"id":"4481","name":"python"}]' \
  -e TG_BOT="123456:ABC-DEF:chatid" \
  zampto-renew
```

### 3. 用代理运行
```bash
docker run --rm \
  -e ZAMPTO_ACCOUNT="your@email.com" \
  -e ZAMPTO_PASSWORD="your_password" \
  -e TARGET_SERVERS='[{"id":"4480","name":"java"}]' \
  -e GOST_PROXY="socks5://user:pass@host:port" \
  zampto-renew
```

> 提示：把敏感变量写进 `.env` 文件，再用 `--env-file .env` 传入，避免密码出现在命令行历史。

---

## 四、本地定时运行（cron 示例，Linux）

编辑 crontab：
```bash
crontab -e
```
每天 09:00（北京时间）运行：
```cron
0 1 * * *  cd /path/to/order && ZAMPTO_ACCOUNT=xxx ZAMPTO_PASSWORD=xxx TARGET_SERVERS='[...]' xvfb-run --auto-servernum --server-args="-screen 0 1920x1080x24" python zampto_renew.py >> renew.log 2>&1
```

---

## 五、GitHub Actions 运行

将仓库推到 GitHub，在 `Settings → Secrets and variables → Actions` 中配置：
`ZAMPTO_ACCOUNT`、`ZAMPTO_PASSWORD`、`TARGET_SERVERS`（必填），`GOST_PROXY`、`TG_BOT`（可选）。
之后可在 Actions 页手动触发，或按 schedule 自动运行（每 3.5 小时 ≈ 北京时间 00:00 / 03:30 / 07:00 / 10:30 / 14:00 / 17:30 / 21:00）。
