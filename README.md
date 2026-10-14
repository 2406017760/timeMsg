# TimeMsg

每天早上，让 AI 给你发一条问候短信。

电脑上常驻一个小程序：每天在设定时间窗内**随机挑一个时刻**，读取你放在 `profile/`
里的个人资料，调用 AI 写一段话；你 iPhone 上的「快捷指令」到点去取，然后给自己发一条 iMessage。

```
Windows 电脑                                        iPhone
┌───────────────────────────┐                  ┌──────────────────────┐
│ 06:30~08:30 之间随机时刻    │  Cloudflare      │ 个人自动化 × 5        │
│  ↓ 读 profile/，调 AI      │  Tunnel (HTTPS)  │ 06:40/07:10/07:40/   │
│  ↓ 存 SQLite（当天只生成一次）│ ◄─────────────── │ 08:10/08:40          │
│  HTTP 服务 127.0.0.1:8765  │  /pull?k=TOKEN   │  ↓ 有内容就发给自己    │
│  + cloudflared 子进程       │ ───────────────► │    （iMessage，免费） │
└───────────────────────────┘                  └──────────────────────┘
```

不需要公网 IP，不用开放防火墙端口，手机在蜂窝网络下也能取。
给自己发 iMessage 不花短信费。

---

# 部署

按顺序做完这九步，大约 20 分钟。只需要一个 Cloudflare 账号，和一个托管在 Cloudflare 的域名。

## 1. 装 Python

```powershell
winget install -e --id Python.Python.3.12 --scope user
```

装完**新开一个 PowerShell 窗口**，确认 `python --version` 能跑。

> 本项目零第三方依赖，只用 Python 标准库，不需要 pip install。

## 2. 装 cloudflared

```powershell
winget install -e --id Cloudflare.cloudflared
```

> 如果卡在管理员提权对话框，可以改用官方免安装二进制，放到 `%LOCALAPPDATA%\cloudflared\`
> 就行，程序会自动找到它：
> ```powershell
> $d = "$env:LOCALAPPDATA\cloudflared"; mkdir $d -Force | Out-Null
> Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile "$d\cloudflared.exe"
> ```

## 3. 生成配置

```powershell
python tools\init_config.py
```

会生成 `config.json` 并自动填入一串随机 token。**这个文件不要提交到 git**（`.gitignore` 已排除）。

## 4. 填 AI 的 API key

```powershell
python tools\init_config.py --force --api-key sk-你的key
```

默认用 DeepSeek（`https://api.deepseek.com`）。换服务商改 `config.json` 里的
`ai.base_url` 和 `ai.model` 就行，任何 OpenAI 兼容接口都可以。不填 key 程序也能跑，
只是发的是内置兜底句子。

## 5. 建 Cloudflare 隧道

1. 打开 https://one.dash.cloudflare.com/ → **Networks → Tunnels → Create a tunnel**
2. Connector 选 **Cloudflared**，名字填 `timemsg` → Save
3. 在 "Install and run a connector" 页面选 **Windows**，复制命令里那串 `eyJ...`（隧道 token）

   > ⚠️ **不要执行那条 `service install` 命令。** 程序会自己托管 cloudflared 进程，
   > 装成 Windows 服务会导致两个进程跑同一条隧道。

4. 回到隧道详情 → **Public Hostname** → Add a public hostname → 选 **Published application**

   | 字段 | 值 |
   | --- | --- |
   | Subdomain | `timemsg`（随便起，但别用太容易猜到的名字）|
   | Domain | 你的域名 |
   | Path | 留空 |
   | Service Type | `HTTP` |
   | URL | `http://127.0.0.1:8765` |

   > URL **必须带 `http://` 前缀**，否则新版界面会报 `Invalid service URL format`。

5. 把 token 和域名填进 `config.json`：

```json
"tunnel": {
  "mode": "named",
  "token": "这里粘贴隧道 token（控制台里那串 eyJ 开头的长字符串）",
  "public_url": "https://timemsg.你的域名.com",
  "bind_localhost": true
}
```

`bind_localhost: true` 会让 HTTP 服务只监听 `127.0.0.1`，同一个 Wi-Fi 下的人绕不过 Cloudflare。

## 6. 启动并验证

```powershell
run.bat
```

日志里出现这几行就算成功：

```
HTTP 服务已启动：http://127.0.0.1:8765/
隧道模式：named
cloudflared | INF Registered tunnel connection connIndex=0 ... protocol=quic
```

然后在**电脑浏览器**打开 <http://127.0.0.1:8765/setup>，它会直接把该填进快捷指令的完整 URL
打出来给你复制。这个页面只对本机开放，从隧道进来的请求会返回 403。

## 7. 配 iPhone 快捷指令

先打开「信息」App 给自己发一条消息（建立会话，避免后面出现「收件人值无效」）。

然后新建快捷指令，放两个动作：

1. **获取 URL 内容** — URL 从上面的 `/setup` 页面复制，方法 `GET`，超时 `15` 秒
2. **如果** `URL 内容` **不包含** `__NOTHING__`
   - 里面放 **发送信息**：收件人 = 你自己，内容 = 「URL 内容」变量，关掉「发送时询问」

`__NOTHING__` 是「今天已经发过了」的标记，所以这个条件为假时直接跳过，不会重复发。

## 8. 建 5 个自动化

iOS 的自动化只能按固定时刻触发，所以「随机时间」是这样实现的：

「快捷指令」→ 自动化 → 新建 → **时间** → `06:40` → 重复「每天」→
**选「立即运行」**（不要选"运行前询问"，否则每天要手动点）→ 运行快捷指令。

然后复制这个自动化，时间改成 `07:10`、`07:40`、`08:10`、`08:40`，一共 5 个。

原理：电脑在 06:30~08:30 之间随机某一刻才生成，**第一个取到内容的自动化负责发送**，
后面几个拿到 `__NOTHING__` 就停。所以每天只发一条，而时刻是随机的。

## 9. 开机自启与电源

```powershell
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1   # 登录时静默启动
powershell -ExecutionPolicy Bypass -File .\set-no-sleep.ps1        # 让电脑别睡眠
```

> **电脑睡眠 = 早上收不到。** 这两条别跳过。

---

# 日常使用

## 个人资料（决定内容质量的关键）

程序每天生成前会读一遍 `profile/` 目录：

```
profile/
├── 关于我.md          你的情况、喜欢的语气、不喜欢听到什么
├── 每日作息.md        固定作息 + 最下面「今天」那段每天改
├── 睡眠记录.md        每天追加一行：日期 | 入睡 | 起床 | 备注
└── 我跟它说过的话.md   你回它的话（可选，用来让它「接着上次聊」）
```

几条规则：

- 只读 `.md` 和 `.txt`，子目录也会读，隐藏文件跳过
- **自动纯文本化**：`#` 标题、`-` 列表、`**加粗**`、链接语法都会去掉，所以文件里随便写
- **行首以 `//` 开头的行不会发给 AI**，可以拿来写自己的备注
- 改完下一次生成就生效，不用重启

写完用这个命令确认 AI 到底读到了什么（会打印实际送进模型的全文）：

```powershell
python -m app.main context
```

> ⚠️ 这些内容会随每次生成发给 AI 服务商。身份证号、密码、他人隐私不要放进去。

## 从 Apple 健康导出提炼数据（可选）

别把 Apple 健康的原始导出直接丢进 `profile/`——那个 `导出.xml` 有一两百 MB，读不动也没重点。
用附带工具先提炼成一份摘要：

```powershell
# 健康 App → 右上角头像 → 导出所有健康数据 → 解压到 profile\apple_health_export\
python tools\health_summary.py              # 统计最近 30 天，写出 profile\健康数据.md
python tools\health_summary.py --days 90    # 想统计更久
```

流式扫描，200MB 也就几十秒。摘要是离线快照，**建议每周重跑一次**。

## 调整提示词

风格全在 `prompts/greeting.txt`，纯文本，**改完下一次生成就生效，不用重启**。
字数区间、随机主题、日常提醒概率在 `config.json` 的 `prompt` 段。

提示词可以存档、来回切：

```powershell
python -m app.main prompt list              # 看有哪些版本、当前用哪个
python -m app.main prompt save 我的版本      # 存档当前提示词
python -m app.main prompt use 我的版本       # 切换
```

## 常用命令

```powershell
run.bat                              # 前台运行，能看日志（调试用）
start-background.bat                 # 后台静默运行
python -m app.main peek              # 今天的状态：抽到几点、是否已生成、是否已被取走
python -m app.main generate --force  # 立刻重新生成今天的内容（会重置「已取走」标记）
python -m app.main test-api          # 只测 AI 接口，打印一条生成结果
python -m app.main context           # 看 AI 读到的个人资料全文
python tests\smoke_test.py           # 离线自检，57 项，不联网
Stop-Process -Name pythonw,cloudflared   # 停止服务
```

> `config.json` 里 `prompt` / `ai` / `context` 三段的改动会**自动热加载**，不用重启；
> 监听地址、端口、隧道这些要重启才生效。

---

# 安全

接口是暴露在公网上的，所以：

1. **token 要够随机**。`tools/init_config.py` 生成的是 24 位随机字符，别改成 `123456`。
2. **隧道模式下只监听 `127.0.0.1`**（`bind_localhost`），同网段的人绕不过 Cloudflare。
3. 未鉴权的 `/health` 只返回 `{"ok": true}`，不泄露作息和用量。
4. `/setup` 从隧道访问会返回 403，token 不会泄露到公网。
5. 想再加固可以用 **Cloudflare Access**（Zero Trust 免费额度够个人用），
   在快捷指令的「获取 URL 内容」里补上 `CF-Access-Client-Id` / `CF-Access-Client-Secret` 请求头。

---

# 常见问题

**早上没收到？** 依次查：电脑是否睡眠 → `python -m app.main peek` 看今天是否已生成 →
`logs\timemsg.log` → 手机能不能打开 `/setup` 里那个 URL。

**域名刚建好，本机解析不了？** 本机 DNS 可能缓存了"不存在"的否定回答（最长 30 分钟）。
用 `Resolve-DnsName timemsg.你的域名.com -Server 1.1.1.1` 能分辨是记录没建好还是缓存问题。
手机换蜂窝网络可以立刻绕过。

**收到两条？** 确认 URL 里有 `once=1`（`/pull` 短地址已包含），并让 5 个自动化间隔 ≥30 分钟。

**锁屏时没自动发送？** 把该自动化的「运行前询问」打开，会有声音提醒你点一下。
这是整套方案里唯一没法在电脑上替你验证的环节，建议先锁屏实测一晚。

**快捷指令报「收件人值无效」？** 先在「信息」里给自己发一条消息建立会话，
再回到快捷指令里从联系人中选自己。

---

# 文件说明

| 文件 | 作用 |
| --- | --- |
| `app/config.py` | 配置加载、提示词加载、可热加载参数 |
| `app/context.py` | 读 `profile/`，纯文本化，拼成 AI 上下文 |
| `app/generator.py` | 调 AI、随机主题、防重复、字数双向纠正、本地兜底 |
| `app/store.py` | SQLite：随机时刻、当天内容、取走标记、历史 |
| `app/server.py` | HTTP 接口 `/health` `/pull` `/today` `/history` `/setup` |
| `app/tunnel.py` | 托管 cloudflared 子进程，崩溃自动重启 |
| `app/main.py` | 常驻调度器与命令行 |
| `prompts/greeting.txt` | **提示词**，改风格就改它 |
| `profile/` | **你的个人资料**，AI 每天生成前会读 |
| `tools/init_config.py` | 生成 `config.json` |
| `tools/health_summary.py` | 把 Apple 健康导出提炼成摘要 |
| `tests/smoke_test.py` | 离线自检 |
| `install-autostart.ps1` | 注册/取消开机自启 |
| `set-no-sleep.ps1` | 让电脑不睡眠 |

## 改代码的人请注意（Windows 编码坑）

- **`.bat` 必须保持纯 ASCII**。`cmd.exe` 按系统代码页（GBK）逐字节解析批处理，
  中文会把命令行拆坏，报出莫名其妙的错误。
- **`.ps1` 必须带 UTF-8 BOM**。Windows PowerShell 5.1 默认按 ANSI 读取无 BOM 的脚本，
  中文会变乱码。仓库里的脚本都已带 BOM，另存时别把 BOM 去掉。
