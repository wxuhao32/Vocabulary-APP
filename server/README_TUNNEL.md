# Vocabulary APP — 公网 Tunnel 接入指南

让异地用户（不同 Wi-Fi / 不同城市）通过公网访问你开发电脑上的 FastAPI 后端。
**不购买云服务器、不迁移后端、不修改业务逻辑**，仅通过 Tunnel 将本地服务映射到公网。

```
异地 Android 手机
   ↓ Internet
公网 Tunnel 地址（https://xxx.trycloudflare.com）
   ↓
开发电脑 cloudflared（Cloudflare Tunnel 客户端）
   ↓
FastAPI（http://127.0.0.1:8000）
   ↓
SQLite
```

---

## 一、总体流程（三步）

| 步骤 | 做什么 | 对应脚本 |
|---|---|---|
| 1 | 启动 FastAPI 本地服务器 | `启动CET4Prep服务器.bat`（已有，桌面或项目根） |
| 2 | 下载 Tunnel 客户端（仅首次） | `server\tunnel\下载cloudflared.bat` |
| 3 | 启动公网 Tunnel | `启动Vocabulary公网Tunnel.bat`（项目根） |

之后把启动脚本输出的公网地址填入 App「我的 → 账户与安全 → 服务器地址」即可。

---

## 二、Tunnel 如何启动

### 2.1 首次：下载 cloudflared 客户端（约 60MB，一次性）

双击运行：`server\tunnel\下载cloudflared.bat`
- 自动从 GitHub Release 下载 `cloudflared.exe`，失败会尝试备用源；
- 下载成功会打印版本号（如 `cloudflared version 2026.x.x`）；
- 若自动下载失败（网络原因），请手动下载后改名为 `cloudflared.exe`
  放到 `server\tunnel\` 目录：
  `https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe`

### 2.2 每次：启动 Tunnel

双击运行：`启动Vocabulary公网Tunnel.bat`（项目根目录）

启动后等待 10~30 秒，脚本会自动解析出公网地址并打印：

```
✅ 公网地址已生成：https://xxxx-xxxx.trycloudflare.com
✅ 已写入配置：server\tunnel_config.json

▶ Android App 设置：
   我的 → 账户与安全 → 服务器地址 → 填入上面公网地址（末尾不带 /）
▶ WebSocket 公网地址：
   wss://xxxx-xxxx.trycloudflare.com/ws/echo
```

**保持该窗口开着**（Tunnel 即持续运行）。按 `Ctrl+C` 停止隧道。

> 注意：免费快速隧道每次启动会生成**新的随机地址**（旧的失效）。
> 若需固定地址，需 Cloudflare 账户 + 自有域名（命名隧道），见文末「进阶」。

---

## 三、FastAPI 如何启动

保持现有方式不变：

```
双击 启动CET4Prep服务器.bat
（等价于：cd server && .venv\Scripts\python run.py --host 0.0.0.0 --port 8000）
```

要求 FastAPI 监听 **127.0.0.1:8000**（Tunnel 默认映射这个地址，见
`server\tunnel_config.json` 的 `local_host / local_port`；如果改成别的端口，
同步改 tunnel_config.json 或给启动脚本传 `--local-url http://127.0.0.1:新端口`）。

---

## 四、Android App 配置哪个公网地址

App 内单点配置（不硬编码，符合集中配置要求）：

1. 打开 App → 「我的」→「账户与安全」→ 找到「服务器地址」行（齿轮图标，消息通知下方）→ 点击修改；
2. 填入公网地址（不带末尾斜杠）：
   `https://xxxx-xxxx.trycloudflare.com`
3. 保存后 App 的所有请求（登录/注册/好友/聊天/文件/通知）都会走公网地址。

> - 也可以未登录时在登录页底部点「服务器地址：xxx 修改」直接改（同一处配置）。
> - 本地开发时想切回局域网：把服务器地址改回 `http://电脑局域网IP:8000` 或 `http://127.0.0.1:8000` 即可，开发/公网切换互不影响。
> - 地址保存在 localStorage（`cet4deck.api`），修改即时生效，无需重启 App。

**集中配置位置一览**（`server/tunnel_config.json`，Tunnel 启动时自动更新）：

```json
{
  "local_host": "127.0.0.1",          // FastAPI 监听地址
  "local_port": 8000,                  // FastAPI 监听端口
  "public_base_url": "https://xxxx.trycloudflare.com",  // 公网 HTTP API 地址
  "public_ws_url": "wss://xxxx.trycloudflare.com/ws/echo", // 公网 WebSocket 地址
  "tunnel_provider": "cloudflare",
  "updated_at": "2026-08-14 06:40:00"
}
```

---

## 五、如何验证异地设备能否注册和登录

### 5.1 快速连通性检查（任何联网设备）
浏览器 / 手机访问：`https://xxxx.trycloudflare.com/api/health`
返回 `{"ok": true, "service": "cet4prep-auth", ...}` 即公网可达。

### 5.2 注册与登录（异地用户流程）
1. 异地朋友手机安装 App（或浏览器打开公网地址的 `/app`）；
2. 设置服务器地址为公网地址；
3. 「注册」→ 输入手机号/用户名/密码 → 获取短信验证码：
   - 本项目短信是 **Mock Provider**（本地模拟），验证码不会真的发到手机；
   - 验证码由**开发者（你）**在开发电脑上查询后告知朋友：
     - 方法A：本机浏览器访问
       `http://127.0.0.1:8000/api/auth/debug/last-sms?phone=朋友手机号&purpose=register`
       （注意：该调试接口**仅本机可访问**，公网访问会被拒绝——这是刻意保护）；
     - 方法B：看 FastAPI 启动窗口日志
       `[MOCK-SMS] to=1xxxxxxxxxx purpose=register code=123456`；
4. 朋友填入验证码 → 完成注册 → 登录 → 搜索你的公开数字 ID → 加好友 → 聊天。

### 5.3 安全校验（重要，确认保护生效）
在**非本机**（如手机浏览器）访问以下地址，应返回 **404**（而不是验证码明文）：
```
https://xxxx.trycloudflare.com/api/auth/debug/last-sms?phone=1xxxxxxxxxx&purpose=register
https://xxxx.trycloudflare.com/api/auth/debug/captcha?captcha_id=xxxx
```
同时确认：
- SQLite 数据库（`server/data/vocab_auth.db`）与本地文件目录**不会**出现在公网 URL 下；
- 管理员接口 `/api/admin/*` 仍需双因素认证（管理员登录页 Logo 连点 5 次进入），Token 失效即拒绝。

---

## 六、如何验证 WebSocket 是否正常连接

Tunnel 已具备 WebSocket 转发能力，提供两个端点：

| 端点 | 用途 |
|---|---|
| `/ws/echo` | 连通性验证（v9.100）：发任意文本，回显 `echo:<msg>`，不参与业务 |
| `/ws` | **业务实时通道（v9.110）**：聊天消息 / 通知实时推送，鉴权 `?token=<access_token>`（登录后 App 自动建立） |

### 6.1 网页测试（推荐，无需安装）
浏览器打开开发者工具 Console，粘贴运行：

```js
const ws = new WebSocket("wss://xxxx-xxxx.trycloudflare.com/ws/echo");
ws.onopen = () => { console.log("已连接"); ws.send("hello-from-mobile"); };
ws.onmessage = (e) => console.log("收到:", e.data);   // 期望: echo:hello-from-mobile
ws.onclose = () => console.log("连接已关闭");
```

- `onopen` 触发 → 公网 WebSocket 连接成功；
- 收到 `echo:hello-from-mobile` → 双向传输正常；
- 断开网络/关闭 Tunnel 再恢复 → `onclose` 触发，App 客户端自动重连（退避 1~30s），
  重连成功后自动补齐离线期间的消息（消息已持久化在 SQLite，WS 只负责实时传输）。

### 6.2 命令行测试（开发电脑上）
```bash
cd server
.venv\Scripts\python -c "
import asyncio, websockets
async def t():
    async with websockets.connect('wss://xxxx-xxxx.trycloudflare.com/ws/echo') as ws:
        await ws.send('ping')
        print(await ws.recv())   # 期望输出 echo:ping
asyncio.run(t())
"
```

---

## 七、Tunnel 地址变化时改哪里

免费快速隧道每次启动地址都会变。地址变化后：

1. **App 端**：我的 → 账户与安全 → 服务器地址 → 改成新的公网地址（只改这一处，其它页面全部自动跟随）；
2. **配置记录**：`server/tunnel_config.json` 的 `public_base_url / public_ws_url` 会在下次启动 Tunnel 时自动更新；
3. 代码里**没有任何地方硬编码**公网地址（前端只有默认 `http://127.0.0.1:8000` 的本地兜底值，可改可不改）。

> 想彻底告别地址变化：注册 Cloudflare 账户 + 绑定一个域名，把
> `cloudflared tunnel --url http://127.0.0.1:8000` 换成命名隧道
> （`cloudflared tunnel login` → `tunnel create` → 配置 DNS），地址即固定为你的域名。

---

## 八、常见问题

| 现象 | 原因与处理 |
|---|---|
| 双击启动 Tunnel 报「未找到 cloudflared.exe」 | 先运行 `server\tunnel\下载cloudflared.bat` |
| 启动后长时间没有生成公网地址 | 本机无法访问外网/防火墙拦截 cloudflared；本地 FastAPI 未启动；端口与 tunnel_config.json 不一致 |
| App 公网访问提示「网络错误」 | Tunnel 窗口被关闭或电脑睡眠；重新启动 Tunnel 并更新 App 服务器地址 |
| 公网访问 debug 接口返回 404 | ✅ 正常，这是刻意保护（调试接口仅本机开放） |
| 异地用户注册收不到短信验证码 | Mock 短信无真实通道，验证码由开发者在电脑上查日志/debug 接口后告知（见 5.2） |
| 局域网手机还能不能用 | 能。手机连同一 Wi-Fi 时把服务器地址改回 `http://电脑IP:8000` 即可 |

## 九、安全说明（个人测试环境定位）

- ✅ 登录鉴权（Access 15min / Refresh 7d 旋转）、账号封禁、管理员双因素全部保留；
- ✅ 调试接口（短信/图形验证码明文）经公网一律 404，仅本机/局域网直连可用；
- ✅ SQLite 数据库与本地文件目录未挂载到 FastAPI，公网无法访问；
- ⚠️ 免费隧道地址是公开的（任何人拿到 URL 都能访问你的 API），仅用于个人测试/朋友体验，
  不要上传到公开场合；结束测试后关闭 Tunnel 窗口即可下线。
