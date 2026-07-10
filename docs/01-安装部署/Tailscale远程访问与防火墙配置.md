# 用 Tailscale 从外面安全访问家里的 Aether

有时候你在外面，也想用手机看看家里的智能设备、跟 Aether 管家聊两句。但直接把 Aether 后端暴露到公网？那可太危险了——任何人扫到你家的 IP 都能进来乱搞。

这篇文档教你用 **Tailscale** 给 Aether 加一条"私人隧道"：只有你自己 Tailscale 网络里的设备能连进来，外人连门都摸不到。同时配合 Windows 防火墙，把后端端口锁死到只对 Tailscale 网段开放。

---

## 这套方案安全在哪？

先说说为什么这样做安全，再动手。

### 传统做法的坑

很多人远程访问家里服务，要么用端口转发（把 8010 映射到公网），要么干脆关防火墙。这两种都有大问题：

| 做法 | 问题 |
|------|------|
| 路由器端口转发 | 把 8010 直接暴露到公网，全世界都能扫到，密码爆破、漏洞利用随便来 |
| 关掉 Windows 防火墙 | 局域网里任何设备都能连，咖啡店 WiFi、室友电脑都能进 |
| 仅靠 CORS | CORS 只是浏览器层的限制，用 curl/脚本直接发请求根本不认，挡不住攻击者 |

### Tailscale + 防火墙的做法

Tailscale 是基于 WireGuard 的点对点 VPN。装上之后，你的每台设备会分到一个 `100.x.x.x` 的内网 IP（Tailscale 专用网段 `100.64.0.0/10`），设备之间走加密隧道直连，流量不经过公网明文。

我们在此基础上再加一道 Windows 防火墙规则，**只允许 Tailscale 网段（100.64.0.0/10）访问后端 8010 端口**：

```
手机(100.88.25.58) ──加密隧道──> 电脑(100.125.129.111:8010) ──> Aether 后端
       │                                    │
       │                              Windows 防火墙
       │                              只放行 100.64.0.0/10
       │                                    │
   局域网其他设备/公网 ────X 被挡 ────────┘
```

这样形成三重保护：

1. **Tailscale 隧道**：没加入你 Tailnet 的设备，连隧道都进不来（需要你的 Tailscale 账号授权）
2. **Windows 防火墙**：即便 somehow 进了隧道网段以外的来源，8010 端口直接拒绝
3. **Aether 自身加固**：JWT 登录认证 + CORS 收紧 + 全局限流 + MCP 白名单（见各自的部署/运维文档）

> 一句话：**只有你 Tailscale 网络里的设备，才能摸到 8010 端口的门。** 而且进门之后还要登录，登录之后还有限流防滥用。

---

## 准备工作

### 1. 装 Tailscale

去 [tailscale.com](https://tailscale.com) 下载客户端，电脑和手机都装上，用同一个账号登录（Google/Microsoft/GitHub 都行，免费版够用）。

装完之后每台设备会分到一个 `100.x.x.x` 的 IP。在电脑上可以这么查：

```powershell
# 用 PowerShell
tailscale ip -4
```

或者在任务栏的 Tailscale 图标上也能看到。

### 2. 确认 Aether 后端绑在 0.0.0.0

这是关键前提。如果后端只绑 `127.0.0.1`（localhost），那就算防火墙放行了，Tailscale 也连不进来——因为后端根本不监听外部地址。

Aether 的启动脚本 `run_demo_fixed.bat` 已经把后端绑在 `0.0.0.0`（所有网卡），所以正常启动就行。启动后可以用这条命令确认：

```powershell
netstat -ano | findstr "LISTENING" | findstr ":8010"
```

应该看到 `0.0.0.0:8010` 而不是 `127.0.0.1:8010`。如果是后者，说明启动脚本的 `--host` 参数没改对，检查 `run_demo_fixed.bat` 里 uvicorn 那一行是不是 `--host 0.0.0.0`。

---

## 配置 Windows 防火墙（核心步骤）

这一步是重点：建一条规则，**只允许 Tailscale 网段访问 8010**。

### 为什么必须配这条规则？

Windows 防火墙默认对入站连接是"挡"的（`DefaultInboundAction` 没配置时等同 Block）。Aether 后端是 Python 跑的，第一次启动时 Windows 可能弹窗问"要不要放行 Python"——如果你点了允许，那条规则往往是**放行所有来源**的，等于把 8010 暴露给整个局域网。

我们不要用那种粗放的规则，而是手动建一条精确的：

### 操作方法

**以管理员身份**打开 PowerShell（开始菜单搜 PowerShell → 右键"以管理员身份运行"），粘贴运行：

```powershell
New-NetFirewallRule -DisplayName "Aether Backend 8010 (Tailscale only)" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8010 `
  -RemoteAddress 100.64.0.0/10 -Profile Private
```

参数解释：

| 参数 | 含义 |
|------|------|
| `-Direction Inbound` | 入站规则（别人连进来） |
| `-Action Allow` | 允许 |
| `-Protocol TCP -LocalPort 8010` | 只针对 8010 端口的 TCP 连接 |
| `-RemoteAddress 100.64.0.0/10` | **只允许 Tailscale 网段**（100.64.0.0 ~ 100.127.255.255）作为来源 |
| `-Profile Private` | 只在"专用网络"配置文件下生效（Tailscale 网卡默认就是 Private） |

### 验证规则建好了

```powershell
Get-NetFirewallRule -DisplayName "Aether Backend 8010 (Tailscale only)" | Format-List DisplayName,Enabled,Direction,Action
```

应该看到 `Enabled: True`、`Direction: Inbound`、`Action: Allow`。

### 如果以后想删掉这条规则

```powershell
Remove-NetFirewallRule -DisplayName "Aether Backend 8010 (Tailscale only)"
```

---

## 实际使用：从手机访问 Aether

配置完之后，在手机（已加入 Tailscale）上：

1. **打开手机上的 Tailscale App**，确认已连接（显示你的手机 IP，比如 `100.88.25.58`）
2. **打开手机浏览器**，访问：

   ```
   http://<电脑的Tailscale IP>:8010/
   ```

   比如电脑是 `100.125.129.111`，那就访问 `http://100.125.129.111:8010/`

3. 应该能看到 Aether 前端页面，正常登录使用

> 小提示：电脑的 Tailscale IP 用 `tailscale ip -4` 查。这个 IP 是固定的，只要不主动改 Tailscale 配置就不会变，可以存成书签。

### 为什么访问 8010，而不是 5173？

这是最常被问到的问题，也很容易踩坑，单独讲清楚。

Aether 跑起来后，电脑上其实有**两个**能出前端页面的端口：

| 端口 | 是什么 | 监听地址 | 手机能不能访问 |
|------|--------|----------|----------------|
| **5173** | Vite 开发服务器 | `127.0.0.1:5173` | ❌ **不能** |
| **8010** | Aether 后端（也提供前端页面） | `0.0.0.0:8010` | ✅ **能** |

**5173 是给开发用的**。当你在电脑上改前端代码（`.vue` 文件）时，5173 提供热更新——改一行代码浏览器自动刷新，方便开发。它只监听 `127.0.0.1`（本机回环），意思是**只有电脑自己能连，外部任何设备（包括 Tailscale 进来的手机）都连不上**。这不是防火墙能解决的，是 Vite 压根没监听外部网卡。

**8010 是日常使用和远程访问该用的**。它有两个身份：

```
手机浏览器访问 http://100.125.129.111:8010/
                    │
                    ▼
        后端 8010（监听 0.0.0.0，Tailscale 防火墙放行）
                    │
          ┌───────┴────────┐
          ▼                ▼
    前端页面             API 接口
    / 返回 HTML        /api/* 处理业务请求
    /assets/*.js       （带 JWT cookie 认证）
    （构建好的静态文件，
     全部从本机加载，
     不依赖公网 CDN）
```

1. **提供前端页面**：前端代码用 `npm run build` 编译后，产物放在 `app/static/frontend/`，后端在 `app/main.py` 里用 `StaticFiles` 把它挂在 8010 上。访问 `http://...:8010/` 就是拿这些构建好的页面。
2. **处理 API 请求**：前端的 JS 调 `/api/*` 时，自动指向当前页面所在的地址（也就是 8010），形成闭环。

所以**记住一个原则：不管本机用还是手机远程用，都访问 8010。5173 只在你电脑上开发前端代码时临时用，远程访问完全不涉及它。**

> 那什么时候会用到 5173？只有当你想**修改前端代码并实时预览**时，在电脑上打开 `http://localhost:5173` 调试。日常使用和手机访问，永远走 8010。

### 怎么判断是不是真的走 Tailscale？

在手机浏览器访问的时候，把电脑的 WiFi 临时断开（保留 Tailscale）。如果还能访问 Aether，说明确实走的 Tailscale 隧道（因为 Tailscale 会自动切换到移动数据/DERP 中继）。

---

## 常见问题排查

### 手机 ping 得通电脑，但浏览器访问 8010 转圈/超时

这是最典型的情况，**99% 是 Windows 防火墙没放行 8010**。按上面"配置 Windows 防火墙"那一节建好规则就行。

> ping 走的是 ICMP 协议，Windows 默认放行 ICMP；但 8010 走 TCP，没规则就会被挡。所以"ping 得通但连不上服务"是防火墙的典型表现。

### 防火墙规则建了还是连不上

按顺序检查：

1. **后端在不在监听 0.0.0.0**：`netstat -ano | findstr ":8010"` 看 `0.0.0.0:8010`
2. **Tailscale 连没连上**：手机 App 和电脑客户端都要显示"Connected"
3. **IP 对不对**：手机访问的是电脑的 Tailscale IP，不是局域网 IP
4. **规则是不是 Enabled**：`Get-NetFirewallRule -DisplayName "Aether Backend 8010 (Tailscale only)"` 看 Enabled 是不是 True

### 规则建了、Enabled 也是 True，还是连不上（profile 的坑）

这是 Windows 多网卡环境下的一个经典坑，**很容易中招**。

Windows 防火墙规则可以绑定到特定的网络配置文件（Domain / Private / Public）。本文档最初建议的命令带 `-Profile Private`，因为 Tailscale 网卡默认归到 Private。但问题是：**如果你电脑同时连着 WiFi，而 WiFi 网卡被系统判为 Public profile**（很常见，公用 WiFi 默认就是 Public），那 Windows 在判定入站流量时可能不把 Tailscale 流量归到 Private profile，导致 `-Profile Private` 的规则不生效——表现就是"规则明明建了、Enabled 也是 True，手机就是连不上"。

**解决办法**：把规则的 profile 改成 `Any`（不限制配置文件），但 **`-RemoteAddress` 仍然锁死 Tailscale 网段**，安全性完全不降级——因为来源 IP 已经卡死在 100.64.0.0/10，profile 放开只是不再受网卡类型干扰。

在管理员 PowerShell 里：

```powershell
Set-NetFirewallRule -DisplayName "Aether Backend 8010 (Tailscale only)" `
  -Profile Any -RemoteAddress 100.64.0.0/10
```

> 怎么判断是不是这个坑？用 `Get-NetConnectionProfile` 看各网卡的 `NetworkCategory`，如果 Tailscale 是 Private、WiFi 是 Public，基本就是这个原因。

### 能连上但页面"通一半"、转圈加载不进去

如果你访问的是 `http://<IP>:8010/docs`（Swagger API 文档页面）出现这种情况——**能拿到一点点内容但页面转圈出不来**，那不是网络问题。

`/docs` 这个页面本身只有不到 1KB 的 HTML 壳，真正的样式和脚本是从**公网 CDN（`cdn.jsdelivr.net`）**拉的。手机连 Aether 走 Tailscale 没问题，但手机要去公网拉这个 CDN 资源时，走的是手机自己的网络——如果手机网络访问不了 jsdelivr（国内经常被墙或很慢），页面就卡住。

**解决办法**：不要访问 `/docs`，那是给开发者调试 API 用的。**直接访问根路径 `http://<IP>:8010/`**，那是 Aether 前端页面，所有 JS/CSS 都从本机加载，不依赖任何公网 CDN。详见上面"为什么访问 8010，而不是 5173？"那一节。

### 想顺便从手机访问 Home Assistant 面板

HA 跑在 8123 端口，同样可以建一条 Tailscale-only 规则（注意 HA 默认有自己的登录认证）：

```powershell
New-NetFirewallRule -DisplayName "Home Assistant 8123 (Tailscale only)" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8123 `
  -RemoteAddress 100.64.0.0/10 -Profile Any
```

> 注意：这里用 `-Profile Any` 而不是 `-Profile Private`，避免多网卡环境下 Tailscale 流量被归到别的 profile 而规则不生效（见上面"profile 的坑"那一节）。`-RemoteAddress` 仍锁死 Tailscale 网段，安全性不降级。

然后手机访问 `http://<电脑Tailscale IP>:8123`。

> 注意：MQTT 端口 1884 一般不需要从外部访问（设备和 HA 在同一台机器/Docker 网络内通信），不建议对外开放。

### 之前误点了"允许 Python 访问"的弹窗，要不要清理？

建议清理掉那种粗放规则，只保留我们这条精确的。在管理员 PowerShell 里：

```powershell
# 先看看有哪些和 Python 相关的入站规则
Get-NetFirewallRule | Where-Object { $_.DisplayName -like "*Python*" -and $_.Direction -eq "Inbound" } | Select-Object DisplayName,Enabled,Action

# 确认无误后，按 DisplayName 删掉放行所有来源的那些
# Remove-NetFirewallRule -DisplayName "Python"   # 按实际显示的名字删
```

删之前看清楚，别误删了系统需要的规则。

---

## 安全性小结

这套配置的安全模型：

| 防线 | 挡什么 | 怎么配置 |
|------|--------|----------|
| Tailscale 账号授权 | 没加入你 Tailnet 的人 | Tailscale 管理后台控制设备授权 |
| Tailscale 加密隧道 | 中途窃听 | WireGuard 自动加密，无需配置 |
| Windows 防火墙（Tailscale-only） | 非 Tailscale 网段的来源 | 本文档的 `New-NetFirewallRule` |
| Aether JWT 登录 | 未登录用户 | `docs/08-运维排查/API Token安全鉴权.md` |
| CORS 收紧 | 浏览器跨站请求来源 | 后端 `app/main.py` 的 `allow_origin_regex` |
| 全局限流 | 单 IP 滥刷接口 | 后端 `global_rate_limit` 中间件，120 次/分钟 |
| MCP 命令白名单 | 运行时执行任意命令（RCE） | `config.json` 的 `external_mcp` 预声明 |

七层防线层层递进，就算某一层被突破，后面还有兜底。这也是为什么我们坚持"防火墙只放行 Tailscale 网段"而不是"放行所有来源"——**最小权限原则，能不放行就不放行**。
