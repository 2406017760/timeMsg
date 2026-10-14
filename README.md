# TimeMsg — 每天早上随机时刻，给自己发一句 AI 写的随机文案

电脑上常驻一个小程序：每天在你设定的时间窗口内**随机挑一个时刻**，
调用 AI 接口写一句随机主题的话，存进本地数据库；
你 iPhone 上的「快捷指令」到点去拉，然后**给自己发一条 iMessage（免费）**。

```
  Windows 电脑（常驻）                                   iPhone
┌──────────────────────────────┐                 ┌────────────────────────┐
│ 06:30~08:30 之间随机时刻      │  Cloudflare     │ 个人自动化 06:40/07:10 │
│  ↓ 调 DeepSeek 生成一句话     │  Tunnel (HTTPS) │      07:40/08:10/08:40 │
│  ↓ 存入 SQLite（当天只生成一次）│ ◄────────────── │  ↓ 有内容就「发送信息」│
│  HTTP 服务 127.0.0.1:8765     │  /today?once=1  │   给自己（iMessage）   │
│  + cloudflared 子进程托管      │ ──────────────► │                        │
└──────────────────────────────┘                 └────────────────────────┘
        出门用蜂窝网络也能拉取，无需公网 IP、无需开放防火墙端口
```

为什么让手机发最后一步：Apple 不向 Windows 开放 iMessage 发送接口，
而国内云短信 API 个人账号连签名实名报备都过不了，发不出 AI 自由生成的正文。
所以「电脑生成 + 手机发送」是零成本方案里唯一可靠的分工。

---

## 1. 快速开始

### 1.1 安装 Python

```powershell
winget install -e --id Python.Python.3.12 --scope user
```

装完**新开一个 PowerShell 窗口**（让 PATH 生效），验证：

```powershell
python --version
```

> 本项目**零第三方依赖**，只用 Python 标准库，不需要 pip install 任何东西。

### 1.2 写配置

```powershell
python tools\init_config.py                  # 自动生成 config.json 并填入随机 token
python tools\init_config.py --api-key sk-xxx  # 顺便把你的 API key 写进去
```

生成后按需修改 `config.json`：

| 字段 | 说明 |
| --- | --- |
| `token` | 必改。随便一串随机字符，防止同一个网络里别人也能读到你的文案 |
| `ai.api_key` | 你的 API key（DeepSeek / OpenAI / 任何 OpenAI 兼容服务）。留空则用本地兜底句子 |
| `ai.base_url` `ai.model` | 换服务商时改这两项，例如 `https://api.openai.com` + `gpt-4o-mini` |
| `window_start` `window_end` | 随机发送窗口，默认 `06:30`~`08:30`。想固定时间就写成相同值 |
| `listen_port` | HTTP 端口，默认 8765 |
| `tunnel.mode` | `off` / `quick` / `named`，见第 3 节 |
| `tunnel.named_tunnel` | `named` 模式下的隧道名（本地托管方式用） |
| `tunnel.token` | `named` 模式下的隧道 token（控制台托管方式用，推荐） |
| `tunnel.public_url` | `named` 模式下的固定域名，如 `https://timemsg.你的域名.com` |
| `tunnel.bind_localhost` | 开隧道时只监听 `127.0.0.1`，默认 `true`（建议保持） |

也可以用环境变量 `TIMEMSG_API_KEY` 临时提供 key，优先级低于配置文件。

### 1.2.1 放行防火墙（仅局域网模式需要）

**如果你用 Cloudflare Tunnel（推荐，见第 3 节），可以跳过这一步**——隧道是从电脑主动连出去的，
不需要任何入站端口。

局域网模式才需要放行，用**管理员** PowerShell 运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\allow-firewall.ps1
```

### 1.3 先试跑

```powershell
python -m app.main test-api      # 只测 AI 接口通不通（会打印一条真实生成结果）
python -m app.main context       # 看 AI 读到的个人资料全文（强烈建议先跑这个）
python -m app.main generate      # 立刻生成今天的内容，不加 --force 不会覆盖已有的
python -m app.main generate --force   # 强制重新生成（会重置「已取走」标记）
python -m app.main peek          # 看今天的状态（目标时刻、是否已生成、是否已被取走）
python -m app.main prompt list   # 看有哪些提示词版本、当前用的是哪个
```

### 1.4 常驻运行

```powershell
run.bat               # 前台运行，能看日志，适合调试
start-background.bat  # 后台静默运行，日志在 logs\timemsg.log
```

浏览器打开 `http://127.0.0.1:8765/health` 应看到 JSON 状态。

---

## 2. iPhone 端配置（关键步骤）

### 2.1 先创建「给自己」的会话

打开「信息」App，给自己（自己的手机号或 Apple ID）发一条消息。
这一步是为了避免快捷指令里出现「收件人值无效」——这是快捷指令最常见的坑。

### 2.2 建一个快捷指令

「快捷指令」→ 新建 → 命名 `每日一句话`，依次加三个动作：

1. **获取 URL 内容**
   - URL：从 `http://127.0.0.1:8765/setup` 页面复制（Cloudflare Tunnel 的地址）
   - 方法：`GET`，超时：`15` 秒
2. **如果**：把上一步的结果作为输入，条件选「**包含**」，值填 `__NOTHING__`
   - 在「如果」里面加 **停止此快捷指令**
3. **否则**（「如果」下面）：
   - **发送信息**：收件人 = 你自己；信息内容 = 点一下输入框，插入「URL 内容」变量
   - 把该动作里的「**发送时询问**」关掉

> 电脑 IP 怎么找：程序启动时会打印「手机可访问地址」，或者在电脑上运行 `ipconfig`，
> 找无线局域网适配器的 IPv4 地址（形如 `192.168.1.23`）。

### 2.3 建 5 个个人自动化，做出「随机时刻」的效果

iOS 的自动化只能按固定时刻触发，所以随机是这样实现的：

- **自动化 1**：「时间」→ `06:40` → 重复「每天」→ 运行快捷指令 `每日一句话`
  → **关闭「运行前询问」**；iOS 17 及以上选「**立即运行**」
- 复制这个自动化，时间分别改成 `07:10`、`07:40`、`08:10`、`08:40`

原理：电脑在 06:30~08:30 之间随机某一刻才生成内容。
第一个「拉到了内容」的自动化会发出消息，后面几个拉到的是 `__NOTHING__` → 停止。
所以每天**只发一条**，而发出的时刻是随机的（误差不超过 30 分钟）。

### 2.4 一定要实测一次锁屏情况

把手机锁屏放一晚上，第二天看有没有收到。iOS 的时间自动化在锁屏下通常会正常执行，
但「发送信息」这个动作在个别机型/版本上会要求解锁或弹确认框。
如果发现收不到，见下面「常见问题」第 3 条。

---

## 3. 网络：手机怎么访问到电脑

### 3.1 推荐：Cloudflare Tunnel

好处很明确：**手机在蜂窝网络下也能拉取**（不用同 Wi-Fi），**不用开防火墙端口**，
而且电脑不需要公网 IP。隧道进程由 TimeMsg 自己托管，跟着自启一起起来。

先装 cloudflared（装一次就够）：

```powershell
winget install -e --id Cloudflare.cloudflared
```

如果 winget 卡在管理员提权对话框（本机就遇到过），用 Cloudflare 官方免安装二进制，
放到 `%LOCALAPPDATA%\cloudflared\cloudflared.exe` 即可，程序会自动找到它：

```powershell
$dir = "$env:LOCALAPPDATA\cloudflared"; New-Item -ItemType Directory -Force -Path $dir | Out-Null
Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
  -OutFile "$dir\cloudflared.exe"
Get-AuthenticodeSignature "$dir\cloudflared.exe" | Select-Object Status, SignerCertificate
```

第二条命令会显示签名主体，应该是 `CN="Cloudflare, Inc."`。**务必确认签名有效再运行**。

#### 方式 A：quick 模式（免账号，先跑通）

`config.json`：

```json
"tunnel": { "mode": "quick", "cloudflared": "", "named_tunnel": "", "public_url": "", "bind_localhost": true }
```

启动后程序会自动拉出一个 `https://xxx-yyy-zzz.trycloudflare.com` 地址，把它填进快捷指令即可。

```powershell
python -m app.main serve
# 日志里会出现：快捷指令 URL：https://xxx.trycloudflare.com/today?k=...&once=1&plain=1
```

> ⚠️ **quick 模式的硬伤**：每次 cloudflared 重启，域名都会变（电脑一重启就变）。
> 域名一变，快捷指令就静默失效。所以 quick 模式只适合**先验证流程通不通**。

#### 方式 B：named 模式（长期无人值守该用这个）

需要一个托管在 Cloudflare 的域名。**域名固定不变**，快捷指令配一次就再也不用动，
重启电脑、重启隧道都不影响。

有两种建法，**推荐 B1**（本项目实测跑通的就是这条，不需要 `cert.pem`）。

**B1：控制台托管（token 方式，推荐）**

1. 打开 https://one.dash.cloudflare.com/ → **Networks → Tunnels → Create a tunnel**
2. Connector 选 **Cloudflared**，名字填 `timemsg` → Save
3. 在 "Install and run a connector" 页面选 **Windows** 标签，复制命令里那串 `eyJ...`（隧道 token）
   > ⚠️ **不要执行那条 `service install` 命令**。程序会自己托管 cloudflared 进程，
   > 装成 Windows 服务会导致两个进程跑同一条隧道，日志和进程管理都会乱掉。
4. 回到隧道详情 → **Public Hostname** → Add a public hostname → 选 **Published application**

   | 字段 | 值 |
   | --- | --- |
   | Subdomain | `timemsg` |
   | Domain | 你的域名 |
   | Path | 留空 |
   | Service Type | `HTTP` |
   | URL | `http://127.0.0.1:8765` |

   > URL **必须带 `http://` 前缀**，否则新版界面会报
   > `Invalid service URL format (must start with protocol like https://, tcp://, etc.)`
5. `config.json`：

```json
"tunnel": {
  "mode": "named",
  "token": "这里粘贴隧道 token（控制台里那串 eyJ 开头的长字符串）",
  "named_tunnel": "",
  "public_url": "https://timemsg.你的域名.com",
  "bind_localhost": true
}
```

重启程序后，日志里出现这几行就说明成功了：

```
隧道模式：named
cloudflared | INF Registered tunnel connection connIndex=0 location=lax10 protocol=quic
cloudflared | INF Updated to new configuration config="{\"ingress\":[{\"hostname\":\"timemsg.你的域名.com\", ...
```

**B2：本地托管（cert.pem 方式）**

```powershell
cloudflared tunnel login                                   # 浏览器里授权并选择域名
cloudflared tunnel create timemsg                          # 创建隧道
cloudflared tunnel route dns timemsg timemsg.你的域名.com    # 绑定固定域名
```

`config.json` 里把隧道名填到 `named_tunnel`，`token` 留空即可。

> 实测提示：`tunnel login` 在某些网络下回跳会失败（本项目遇到过：一直停在
> `Waiting for login...`，`~/.cloudflared/cert.pem` 始终不生成）。遇到就直接用 B1。

#### 排障：域名建好了但本机解析不了

`timemsg.你的域名.com` 刚创建时，如果本机 DNS 之前查过一次「不存在」，
可能会把否定回答缓存住（Cloudflare SOA 的负缓存最长 30 分钟）。
判断方法：直接问权威 NS 或公共 DNS。

```powershell
Resolve-DnsName timemsg.你的域名.com -Server 1.1.1.1        # 能解析 → 记录是好的
Resolve-DnsName timemsg.你的域名.com -Server zod.ns.cloudflare.com
ipconfig /flushdns                                          # 刷本机缓存
```

如果本机配的 DNS 服务器（有些路由器或内网 DNS 会缓存几十秒到半小时）仍然返回
`DNS name does not exist`，那就是它在负缓存，等一会儿就好；
手机换用蜂窝网络可以立刻绕过。注意 `ipconfig /flushdns` 只清 Windows 自己的缓存，
清不掉上游 DNS 服务器的缓存。

### 3.2 怎么拿到「该填进快捷指令的 URL」

不用去翻日志。程序启动后，在**电脑浏览器**打开：

```
http://127.0.0.1:8765/setup
```

这个页面会直接把完整 URL 打出来给你复制。它**只对本机浏览器开放**：
如果请求是经过 Cloudflare 隧道进来的，会直接返回 403，避免 token 泄到公网。

### 3.3 安全提醒（接口已经暴露在公网了）

1. **`token` 必须是够长的随机串**（`tools/init_config.py` 生成的默认是 24 位随机字符）。
   拿到 URL 的人就能读到你的文案，但读不到历史之外的任何东西。
2. 开隧道时程序**只监听 `127.0.0.1`**（`bind_localhost`），
   同一个 Wi-Fi 下的人无法绕过 Cloudflare 直接访问这个端口。
3. 未鉴权的 `/health` 只返回 `{"ok": true}`，不会泄露你的作息时刻和用量统计。
4. 想再上一层，可以用 **Cloudflare Access**（Zero Trust，免费额度够个人用）：
   在 Cloudflare 后台为零信任应用加上邮箱验证或 Service Token，
   快捷指令的「获取 URL 内容」里补上 `CF-Access-Client-Id` / `CF-Access-Client-Secret` 请求头即可。
   这样即使 URL 和 token 都泄露了，别人也进不来。

### 3.4 备选：局域网直连

不想装 cloudflared 时可以用：手机和电脑连同一个 Wi-Fi，URL 填电脑局域网 IP
（`http://192.168.1.23:8765/...`）。缺点很明显：**手机在蜂窝网络下就拉不到**，
而且需要跑 `allow-firewall.ps1` 放行端口。首次访问时 iPhone 可能弹「允许访问本地网络」，选允许。

---

## 4. 开机自启与电源设置

```powershell
# 注册开机自启（登录后静默后台运行）
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1

# 取消自启
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1 -Remove
```

**务必让电脑早上别处于睡眠状态**，否则手机拉不到：

```powershell
# 可选：设置为永不睡眠（需要时可手动改回）
powershell -ExecutionPolicy Bypass -File .\set-no-sleep.ps1
```

> ⚠️ **只有 `tunnel.mode` 是 `named` 时才建议开机自启。**
> quick 模式每次重启都会换公网域名，自启之后快捷指令反而会静默失效。

## 4.1 日常运维

| 想做什么 | 怎么做 |
| --- | --- |
| 后台启动 | `start-background.bat` |
| 前台看日志 | `run.bat` |
| 看今天状态 / 待复制的 URL | 浏览器打开 `http://127.0.0.1:8765/setup` |
| 停止服务 | `Stop-Process -Name pythonw,cloudflared` |
| 看日志 | `logs\timemsg.log` |
| 换公网地址后 | 重新打开 `/setup` 复制新 URL |

## 4.2 改代码的人请注意（编码坑）

在这台中文 Windows 上踩过两个坑，改文件时别踩回去：

- **`.bat` 文件必须保持纯 ASCII**。`cmd.exe` 按系统代码页（GBK）逐字节解析批处理，
  文件里的中文会把命令行拆坏，报出 `'p0"' is not recognized` 这类莫名其妙的错误。
- **`.ps1` 文件必须带 UTF-8 BOM**。Windows PowerShell 5.1 默认按 ANSI 读取无 BOM 的脚本，
  中文会变乱码。仓库里的三个脚本都已经带了 BOM，用编辑器另存时注意别把 BOM 去掉。

---

## 5. 它每天具体怎么工作

1. 程序启动时（或当天第一次访问接口时），为当天随机抽一个时刻写进数据库，例如 `07:12`。
   这个时刻**当天不会再变**，即使程序重启。
2. 到点后调用 AI，写一句话存库，并记下模型与来源。
3. 手机自动化来拉取时命中「once」逻辑：第一次取走会打标记；
   15 分钟内允许再次取到同一句（万一发送失败能重试）；超过宽限期就不再返回。
4. 同一天**只生成一次**。如果电脑到 09:00 才开机，程序会立刻补生成，当天照样能收到。

AI 侧做了防重复：请求里会带上最近若干条已发出的内容，并要求模型换角度；
同时程序随机抽取 1~3 个主题词、1~2 个风格词注入提示词，再配合较高的 temperature。
断网或接口报错时会让 API 重试 3 次，最终失败则改用本地兜底内容，
保证你**每天早上一定有东西收到**。

---

## 6. 个人资料目录：让问候带上你自己的生活

这是这个项目最有意思的部分。程序每天生成前会把你放在 `profile/` 目录里的资料读一遍，
作为上下文交给 AI，于是问候会提到你昨晚几点睡、今天有什么安排、周三是不是要去健身。

```
profile/
├── 关于我.md       长期不变的个人情况（称呼、城市、职业、喜欢的语气、不喜欢听到的话）
├── 每日作息.md     固定作息 + 最下面「今天」那一段每天改
└── 睡眠记录.md     每天睡前或醒来追加一行
```

**读取规则**

- 只读 `.md` 和 `.txt`，**子目录也会读**，隐藏文件和其它后缀跳过
- 每次生成前重新读，所以**你改完下一次生成就生效**，不用重启程序
- **自动纯文本化**（`context.strip_markdown`，默认开）：`#` 标题、`*`/`-` 列表标记、
  `**加粗**`、`> 引用`、`` `代码` ``、`[文字](链接)` 都会去掉，`00\.` 这类转义还原成 `00.`，
  连续空行折叠成一个。也就是说**文件里随便用什么格式写，AI 只会看到干净的纯文本**
- **私有备注**：行首以 `//` 开头的行，以及 `<!-- -->` 注释，**不会发给 AI**。
  模板说明、给自己的提醒用这个方式留着就行
- 单个文件超过 `max_file_chars`（默认 20000 字）会保留**开头和结尾**，中间标注省略——
  开头通常是身份与核心情况，结尾是最新状态
- 总量超过 `max_total_chars`（默认 30000 字）会忽略后面的文件，避免提示词和费用失控
- 目录不存在也不影响运行，只是问候会变通用

> DeepSeek 的上下文窗口很大，上面的默认值足够放下几万字的档案，
> 而每天一次调用的成本仍然不到一分钱。真需要调就改 `config.json` 里那两个上限。

**确认 AI 到底读到了什么**

```powershell
python -m app.main context
```

它会打印命中的文件、总字数，以及 **AI 实际会看到的全文**。
如果资料里还有 `［］` 这种没替换的模板占位符，它会明确警告你——
这个警告很重要，见下面「一个真实的坑」。

**一个真实的坑**

模板里我用 `［例如 上午评审，下午写方案］`、`［例如 记得带伞］` 这类占位符做示范。
如果直接跑，AI 会把这些**示例当成你的真实资料**，于是你会收到一条提到"上午评审""记得带伞"
的问候——而这些事情根本不存在。所以第一件事就是把三个文件里的 `［］` 内容全部换成你自己的。
自检命令会提示你哪些文件还没改。

**写资料的几条建议**

- 越具体越有用。「周三晚上健身、周五通常加班到很晚」比「喜欢运动」有用得多
- **写出你不想要什么同样重要**。例如「不要说教、不要用'加油'」，效果立竿见影
- 睡眠记录用固定格式，模型才好算：`2026-10-14 | 01:20 | 07:40 | 睡前刷手机太久`
- 记录太长就删掉早一些的，留最近两三周足够
- ⚠️ **隐私**：这些内容会随每次生成发给 AI 服务商（默认是 DeepSeek 的接口）。
  身份证号、密码、他人隐私这类东西不要往里面放

**调整长度与语气**（都在 `config.json` 的 `prompt` 段）

| 字段 | 作用 |
| --- | --- |
| `system_file` | **提示词文件**，默认 `prompts/greeting.txt`。改风格就改这个纯文本文件，不用碰 JSON 转义 |
| `system` | 内联提示词；只有 `system_file` 为空时才用它 |
| `min_chars` / `max_chars` | 目标字数，默认 170~280 |
| `keep_newlines` | `true` 保留段落（长问候）；`false` 只取第一行（每日一句话模式） |
| `hard_max_chars` | 硬上限，超了会在句子边界截断，默认 600 |
| `themes` / `styles` | 每天随机抽几个注入提示词，让内容有变化 |

`config.json` 的 `context` 段控制资料读取：

| 字段 | 作用 |
| --- | --- |
| `enabled` | 关掉就完全不读资料，只写通用内容 |
| `directory` | 资料目录，默认 `profile` |
| `strip_markdown` | 是否把 markdown 转成纯文本，默认 `true` |
| `max_file_chars` / `max_total_chars` | 单文件 / 总量上限（字符数） |
| `extensions` | 读哪些后缀，默认 `.md` 和 `.txt` |

> **字数会双向纠正。** 模型经常写不够，也经常收不住：
> 短于 `min_chars` 就追加"请扩写"的指令重试；超过 `max_chars + 10` 就追加"请压缩"的指令重试。
> 两者都最多重试到 `ai.attempts` 次。实测下来，原本 300~400 字的结果会被压回 230 字左右，
> 比只在提示词里强调字数可靠得多。

**提示词版本管理**

风格这种东西是要反复试的，所以提示词可以存档、可以来回切：

```powershell
python -m app.main prompt list              # 看有哪些版本、当前生效的是哪个
python -m app.main prompt save 对话延续版     # 把当前生效的提示词存进版本库
python -m app.main prompt use 对话延续版      # 切换（只改 config.json 的 system_file）
```

版本文件放在 `prompts/versions/` 下。几句话说明：

- `save` 是**逐字节复制**，不会被换行规范化或吃掉首尾空行（Windows 上很容易踩这个坑）
- 同名版本不会被静默覆盖，会直接报错
- 切换只改 `config.json` 里的一行，**下一次生成就生效，不用重启**
- 切换后 `prompts/greeting.txt` 就不再生效了——`prompt list` 会标出当前用的是哪个

> 提示词里的 `{min_chars}` / `{max_chars}` 支持偏移写法，例如 `{min_chars-50}`、`{max_chars+100}`，
> 代码会算出真实数字再替换。用了偏移写法的话，改 `config.json` 的字数区间时两个版本会一起变。

### 6.1 从 Apple 健康导出提炼数据（可选）

想让问候带上你的睡眠、步数、心率、锻炼，别把 Apple 健康的原始导出直接丢进 `profile/`——
那个 `导出.xml` 动辄一两百 MB，读不动也毫无重点。用附带的小工具先提炼成一份摘要：

```powershell
# iPhone：健康 App → 右上角头像 → 导出所有健康数据 → 解压到 profile\apple_health_export\
python tools\health_summary.py                  # 默认统计最近 30 天
python tools\health_summary.py --days 90        # 想统计更久
```

它会流式扫描（不会把 200MB 读进内存），几秒到几十秒后写出 `profile/健康数据.md`，内容大概长这样：

```
步数：最近 30 天平均 10,070 步/天，最高 46,276 步（2026-10-02）
静息心率：最近 30 天平均 65 bpm，最近一次 64 bpm（2026-10-04）
睡眠：最近 30 天里 12 天有记录，平均 6 小时 13 分/晚
最近 7 个完整日（数据截止 2026-10-13，此后的数据尚未完整）：
　2026-10-13　步数 13,432　睡眠 7 小时 47 分…
```

三个设计上的取舍，都是踩过坑才这么做的：

- **统计口径不含今天**。今天还没过完，把它算进去会把半天数据当成"低活动日"，也会拉低平均值。
- **一律用绝对日期，不写「今天/昨天」**。摘要是一次生成、之后每天被读，相对词第二天就过期了，
  模型会拿过期标签误配日期。
- **明确标出「数据截止」日期并告诉模型别引用之后的数据**。
- 只提炼趋势数字，不搬原始记录；`profile/` 里超过 5MB 的文件会被直接跳过并警告。

> 摘要是离线快照，建议**每周重跑一次**。另外，健康数据会随每次生成发给 AI 服务商，
> 敏感的话可以在 `健康数据.md` 里把某些行用 `//` 注释掉，或者直接删掉这个文件
> （程序不会报错，只是问候少一层依据）。

---

## 7. 常见问题

1. **早上没收到**：按顺序查 —— 电脑是否睡眠 → 服务是否在跑（`/health`）→
   `logs\timemsg.log` 有没有记录 → 手机是否连对网络（Wi-Fi 或 Tailscale）→ `token` 是否一致。
2. **收到两条**：确认 URL 里有 `once=1`；把 5 个自动化时间间隔拉开到 30 分钟以上；
   确认 `once_grace_minutes`（默认 15）小于自动化间隔。
3. **锁屏时没自动发送**：把该自动化的「运行前询问」打开，这样会有声音提醒你点一下；
   或者接受「到达时间取决于你何时看到通知」。也可以在 `信息` 里确认收件人选择的是联系人里的「我自己」。
4. **想改成固定时间**：`window_start` 和 `window_end` 写成同一个值，只需保留一个自动化。
5. **想发真短信而不是 iMessage**：需要一张国内 SIM 卡和 USB 短信硬件（约 100~250 元，
   注意**别买物联网卡**，多数禁用点对点短信）。那种方案下电脑可以直接发，手机端不用配置。
   当前版本的程序只提供 HTTP 通道，加短信通道需要新增一个发送模块。
6. **收到的是绿色短信而不是蓝色 iMessage**：你关闭了 iMessage，或者当时对方设备不可达，
   会退回运营商短信（约 0.1 元/条）。给自己发通常走 iMessage，免费。
7. **手机连不上电脑**：先用手机浏览器直接打开那个 URL 试一下。
   - 走隧道时打不开 → 看 `logs\timemsg.log` 里 cloudflared 的报错；
     确认 `cloudflared --version` 能跑、`config.json` 里的 `named_tunnel` 与 `public_url` 对得上。
   - 走局域网时打不开 → 跑一次 `allow-firewall.ps1`（管理员），并确认电脑网络是「专用网络」；
     同一个 Wi-Fi 下还打不开就检查路由器是否开了「AP 隔离」。
8. **quick 模式的域名变了**：这是 quick 模式的固有问题。要么每次改快捷指令里的 URL，
   要么换 named 模式拿固定域名（见第 3.1 节）。
9. **快捷指令报「收件人值无效」**：先在「信息」里给自己发一条消息创建会话，
   再回到快捷指令里从联系人中选自己。

---

## 8. 文件说明

| 文件 | 作用 |
| --- | --- |
| `app/config.py` | 配置加载与校验 |
| `app/context.py` | 读取 `profile/` 个人资料目录，拼成 AI 上下文 |
| `app/store.py` | SQLite：随机时刻、当天内容、取走标记、历史、用了哪些资料 |
| `app/generator.py` | 调 AI 生成 + 提示词随机化 + 防重复 + 本地兜底 |
| `app/server.py` | HTTP 接口 `/health` `/today` `/pull` `/history` `/generate` `/setup` |
| `app/tunnel.py` | 托管 cloudflared 子进程、解析并保存公网地址、崩溃自动重启 |
| `app/main.py` | 常驻调度器与命令行（`serve` `generate` `peek` `context` `test-api` `history`） |
| `profile/` | **你自己维护的个人资料目录**，AI 每天生成前会读它 |
| `prompts/greeting.txt` | **当前生效的提示词**（风格、语气、禁忌都写在这里），纯文本随时改 |
| `prompts/versions/` | **提示词版本库**，用 `prompt save/use` 存档和切换 |
| `tools/init_config.py` | 生成 `config.json` 并自动填入随机 token |
| `tools/health_summary.py` | 把 Apple 健康导出提炼成一份简短摘要 |
| `run.bat` / `start-background.bat` | 前台调试运行 / 后台静默运行 |
| `install-autostart.ps1` | 注册/删除开机自启计划任务 |
| `allow-firewall.ps1` | 放行局域网访问端口（需管理员） |
| `set-no-sleep.ps1` | 让电脑不睡眠（可选） |
| `tests/smoke_test.py` | 离线自检：`python tests/smoke_test.py` |
