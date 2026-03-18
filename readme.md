# Grok 账号批量注册工具

基于 [DrissionPage](https://github.com/g1879/DrissionPage) 的 Grok (x.ai) 账号自动注册脚本，使用 [DuckMail](https://duckmail.sbs) 临时邮箱接收验证码，通过 Chrome 扩展修复 CDP `MouseEvent.screenX/screenY` 缺陷绕过 Cloudflare Turnstile。

注册完成后自动推送 SSO token 到 [grok2api](https://github.com/chenyme/grok2api) 号池。

## 特性

- DuckMail 临时邮箱（`curl_cffi` TLS 指纹伪装）
- Cloudflare Turnstile 自动绕过（Chrome 扩展 patch `MouseEvent.screenX/screenY`）
- 无头服务器支持（Xvfb 虚拟显示器，自动检测 Linux 环境）
- 中英文界面自动适配
- 自动推送 SSO token 到 grok2api（支持 append 合并模式）
- **Web 管理控制台**（Flask 服务，实时日志、任务控制、配置编辑、SSO 管理）

---

## 环境要求

- Python 3.10+
- Chromium 或 Chrome 浏览器
- [DuckMail](https://duckmail.sbs) 账号（用于创建临时邮箱）
- 可选：[grok2api](https://github.com/chenyme/grok2api) 实例（用于自动导入 SSO token）

---

## 安装

```bash
pip install -r requirements.txt
```

无头服务器（Linux）额外安装：

```bash
apt install -y xvfb
pip install PyVirtualDisplay
# 推荐用 playwright 装 chromium（避免 snap 版 AppArmor 限制）
pip install playwright && python -m playwright install chromium && python -m playwright install-deps chromium
```

---

## 配置文件（config.json）

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
    "run": { "count": 10 },
    "duckmail_api_base": "https://api.duckmail.sbs",
    "duckmail_bearer": "<your_duckmail_bearer_token>",
    "proxy": "",
    "browser_proxy": "",
    "api": {
        "endpoint": "",
        "token": "",
        "append": true
    }
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `run.count` | int | 注册轮数，`0` 为无限循环，可通过 `--count` 覆盖 |
| `duckmail_api_base` | string | DuckMail API 地址，默认 `https://api.duckmail.sbs` |
| `duckmail_bearer` | string | DuckMail Bearer Token（[获取方式](#获取-duckmail-bearer-token)） |
| `proxy` | string | DuckMail API 请求代理（可选） |
| `browser_proxy` | string | 浏览器代理，无头服务器需翻墙时填写（可选） |
| `api.endpoint` | string | grok2api 管理接口地址，留空跳过推送 |
| `api.token` | string | grok2api 的 `app_key` |
| `api.append` | bool | `true` 合并线上已有 token，`false` 覆盖 |

---

## 获取 DuckMail Bearer Token

1. 打开 [duckmail.sbs](https://duckmail.sbs) 并注册登录
2. 打开浏览器开发者工具 (F12) → Network
3. 刷新页面，找到任意发往 `api.duckmail.sbs` 的请求
4. 复制请求头中 `Authorization: Bearer <token>` 里的 token
5. 填入 `config.json` 的 `duckmail_bearer` 字段

---

## 启动方式

### 方式一：Web 管理控制台（推荐）

```bash
python web_server.py
```

启动后访问 `http://localhost:7860`，通过网页界面完成所有操作：

- **任务控制**：设置注册轮数，一键启动/停止
- **实时日志**：SSE 流式推送，即时显示注册进度
- **基础配置**：在线编辑 `config.json`，保存即生效
- **SSO Token**：查看本轮收集的 token，手动推送到 grok2api
- **SSO 文件**：浏览并查看历史 `sso/*.txt` 文件内容
- **历史日志**：查看历史 `logs/*.log` 运行记录
- **测试连接**：一键检测 grok2api 服务连通性

### 方式二：命令行直接运行

```bash
# 按 config.json 中 run.count 执行（默认 10 轮）
python DrissionPage_example.py

# 指定轮数
python DrissionPage_example.py --count 50

# 无限循环
python DrissionPage_example.py --count 0
```

无头服务器会自动启用 Xvfb，无需额外配置。

---

## Web 管理控制台 API

`web_server.py` 提供以下 REST API，可供外部程序调用：

| 路由 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 获取任务状态（运行中/空闲/停止中）|
| `/api/start` | POST | 启动注册任务，参数：`{"count": N, "extract_numbers": bool}` |
| `/api/stop` | POST | 停止当前任务 |
| `/api/config` | GET | 读取 config.json |
| `/api/config` | POST | 保存 config.json |
| `/api/sso` | GET | 获取本次收集的 SSO token 列表 |
| `/api/sso/push` | POST | 手动推送 token 到 grok2api |
| `/api/sso/files` | GET | 列出 `sso/` 目录下的文件 |
| `/api/sso/files/<name>` | GET | 读取指定 SSO 文件内容 |
| `/api/logs` | GET | 列出 `logs/` 目录下的日志文件 |
| `/api/log/files/<name>` | GET | 读取指定日志文件内容 |
| `/api/log/stream` | GET | SSE 实时日志流 |
| `/api/ping` | POST | 测试 grok2api 连通性 |

---

## 输出文件

```
sso/
  sso_<timestamp>.txt     ← 每行一个 SSO token
logs/
  run_<timestamp>.log     ← 每轮注册的邮箱、密码和结果
  web.log                 ← Web 服务运行日志
```

目录在首次运行时自动创建。

---

## 文件结构

```
├── DrissionPage_example.py     # 注册主脚本
├── web_server.py               # Web 管理控制台后端（Flask）
├── email_register.py           # DuckMail 临时邮箱封装
├── config.json                 # 配置文件（不入库，从 config.example.json 复制）
├── config.example.json         # 配置模板
├── requirements.txt            # Python 依赖
├── templates/
│   └── index.html              # Web 控制台前端页面
├── turnstilePatch/             # Chrome 扩展（Turnstile patch）
│   ├── manifest.json
│   └── script.js
├── sso/                        # SSO token 输出（自动创建）
└── logs/                       # 运行日志（自动创建）
```

---

## 无头服务器部署注意

- snap 版 chromium 在 root 下有 AppArmor 限制，推荐用 playwright 安装的 chromium
- 服务器直连 x.ai 可能被墙，需在 `browser_proxy` 填写代理地址
- 脚本自动检测 Linux 环境并启用 Xvfb + playwright chromium 路径
- Web 控制台默认监听 `0.0.0.0:7860`，可通过防火墙限制外部访问

---

## 致谢

- [kevinr229/grok-maintainer](https://github.com/kevinr229/grok-maintainer) — 原始项目
- [grok2api](https://github.com/chenyme/grok2api) — Grok API 代理
- [DuckMail](https://duckmail.sbs) — 临时邮箱服务
