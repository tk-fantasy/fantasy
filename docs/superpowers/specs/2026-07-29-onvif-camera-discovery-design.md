# ONVIF 摄像头自动发现设计

- **日期**: 2026-07-29
- **状态**: 已批准(待实现)
- **目标**: 摄像头 DHCP 换 IP 后,Aether 能自动找回并恢复 RTSP 画面 + PTZ 云台控制。

## 1. 背景与问题

用户 TP-Link 云台摄像头在路由器/摄像头重启后,DHCP 重新分配 IP,导致:

- `config.json` 里 `vision.rtsp_url`(`rtsp://192.168.4.38:554/stream2`)指向的旧 IP 失效,RTSP 流连不上,画面不恢复。
- `ptz.ip`(`192.168.4.38`)同样失效,云台控制失灵。
- Aether 现有重连逻辑(`camera_stream.py` `_worker`)只是用**同一个** `rtsp_url` 反复重试 + 指数退避,**没有任何"重新发现 IP"的回路**,所以 IP 变了永远连不回来。

商业摄像头软件(IP Camera Viewer / NVR / ONVIF Device Manager)换 IP 能恢复,是因为它们**不记 IP,靠 ONVIF 自动发现认设备**。本设计就是给 Aether 补上这个能力 —— 让它"认设备不认 IP"。

### 1.1 关键约束(来自需求澄清)

| # | 约束 | 影响 |
|---|------|------|
| 1 | Docker 容器运行 | **不能依赖 UDP 多播广播**(Docker 桥接网络默认丢多播,与现有 RTSP-over-UDP 被 NAT 丢同源),必须走子网单播 |
| 2 | 单设备为主,以后可能加 | 第一版只做单设备发现,但身份证用 MAC,为多设备预留(不堵死扩展路) |
| 3 | 身份证用 MAC 地址 | MAC 不随 IP 变,比型号唯一,比 IP 可靠 |
| 4 | 被动触发 + 手动兜底 | 平时零开销,掉线自动找回;发现失败用户可手动填 IP |
| 5 | 限子网扫描 | 配合约束 1,避开多播,扫 config 旧 IP 所在 /24 |
| 6 | RTSP + PTZ 同步更新 | 解决两处独立硬编码,画面和云台同时恢复 |
| 7 | 首次靠现有 IP 读 MAC 自动捕获 | 对用户零操作:现有 IP 还能用时跑一次即配好 |

### 1.2 现状资产(可直接复用)

- `onvif-zeep-async>=4.0.0` 已安装(`requirements.txt`),PTZ 服务已用 `ONVIFCamera(ip, port, user, pwd)`。
- `app/services/config_probes.py`:
  - `probe_rtsp(url, user, pwd)` —— TCP 端口探测 + cv2 读帧验证
  - `probe_ptz(ip, port, user, pwd)` —— 临时 ONVIFCamera + GetProfiles(可直接用于 Stage2 验证候选设备)
- `app/services/ptz_service.py`:
  - `extract_host_from_url(url)` —— 从 RTSP URL 提 IP(推子网用)
  - 模块级单例 `ptz_service = PtzService()`,懒加载连接
- `app/core/config.py`:
  - `get_config(path, default)` —— 内存全局字典读
  - `update_config_section(section, values)` —— 写 config.json + 同步内存(发现后回写用)

## 2. 方案选型

对比三种发现机制:

| 方案 | 做法 | 优点 | 缺点 | 结论 |
|------|------|------|------|------|
| **A. 子网单播 ONVIF probe 扫描** | 对 /24 每个 IP 并发探测端口,再对候选做 ONVIF probe 读 MAC | ✅ 绕过 Docker 多播坑;✅ 不装新包;✅ MAC 匹配天然支持多设备 | ⚠️ /24 扫 254 IP 有几秒延迟(掉线恢复场景可接受) | **采用** |
| B. WS-Discovery 多播广播 | 调 `onvif.discovery` 走 UDP 多播 3702 | 设计最优雅 | ❌ Docker 桥接网络默认丢多播,大概率发现不到,需 `network_mode: host` 破坏隔离 | 否决 |
| C. ARP 表 + ONVIF 验证 | 读 ARP 表查 MAC→IP,再单点验证 | 发现最快 | ❌ 容器读不到宿主 ARP 表,需 scapy + raw socket 权限;ARP 条目会过期,过期后仍需 ONVIF 兜底 | 否决 |

**决定:方案 A。** 它是唯一确定能在 Docker 环境下可靠工作、且不引入新依赖/新权限的方案。

## 3. 架构设计

### 3.1 新增组件

```
app/services/
  camera_discovery_service.py   ← 新增:ONVIF 设备发现(子网单播 + MAC 匹配)
```

`CameraDiscoveryService` 职责单一:

1. 首次从现有 IP 读 MAC 并回填 config
2. 掉线时子网扫描找回 IP
3. 更新 config(`vision.rtsp_url` + `ptz.ip`)
4. 通知 camera_stream / ptz_service 重连

### 3.2 职责划分(单向依赖,避免循环)

| 组件 | 职责 | 不做什么 |
|------|------|---------|
| `CameraDiscoveryService` | 发现 + MAC 匹配 + config 更新 + 通知重连 | 不管开流、不管云台动作、不持有摄像头连接 |
| `CameraStream._worker` | 检测连续开流失败时**调用** discovery 找新 IP,拿到后照常重连 | 不实现发现逻辑本身 |
| `PtzService` | 提供 `notify_ip_changed(new_ip)` 钩子,IP 变了主动重连 | 不主动发现 |

**设计原则**:discovery 是**被动被调用**的服务 —— 不自己跑后台线程,不持有摄像头连接。这样它无状态、易测、不与 worker 生命周期耦合。

### 3.3 调用关系

```
bootstrap 启动
  └─ 首次 MAC 捕获:有 IP 没 MAC → 连一次 ONVIF 读 MAC → 写回 config
        (失败不影响启动,下次掉线时 fallback 到子网全扫)

CameraStream._worker (现有重连循环)
  └─ 连续开流失败 N 次 + discovery_enabled
        └─ discovery.find_camera()  ← 被动调用
              ├─ 命中 → 更新 config(vision.rtsp_url + ptz.ip)
              │         → 通知 ptz_service.notify_ip_changed()
              │         → worker 下一轮读到新 rtsp_url,重连
              └─ 未命中 → worker 照旧指数退避,等下一轮再试

手动发现按钮(新增路由)
  └─ discovery.find_camera()(同上,失败时允许手动填 IP)
```

## 4. 发现算法详细流程

```
触发发现(掉线自动 / 手动按钮)
   │
   ├─ 读 config:已知 MAC?已知子网(从旧 IP 推 /24)?
   │     ├─ 没有 MAC → 走"首次配对"分支(用现有 IP 连一次读 MAC,写回 config)
   │     └─ 有 MAC → 正式扫描
   │
   ├─ 【扫描循环,受总超时 discovery_timeout_seconds 约束,默认 30s】
   │     ├─ Stage1: 并发 TCP 端口探测(80/554/8080,单超时 0.5s,并发~150)
   │     │          → 拿到端口开放的候选 IP 列表(通常 1-5 台)
   │     ├─ Stage2: 对每个候选 IP 做 ONVIF probe(读 HardwareId = MAC)
   │     │          → MAC == config 记录的 MAC ? 命中 : 跳过
   │     └─ 命中 → 读出该设备当前 IP → 跳出循环
   │        未命中且未超时 → 退避(5s)后重扫
   │
   ├─ 命中:更新 config
   │     ├─ vision.rtsp_url = rtsp://<新IP>:554/stream2(端口/路径用 config 现有的,只换 IP)
   │     ├─ ptz.ip = <新IP>
   │     ├─ 写 config.json(持久化,重启不丢)
   │     └─ 通知 camera_stream(下一轮读新值)+ ptz_service.notify_ip_changed()
   │
   └─ 超时未命中:状态 =「找不到设备」→ 前端开放手动填 IP(手动填 → probe_ptz 验证 → 写 config)
```

### 4.1 扫描耗时预估

| 阶段 | 动作 | 墙钟时间 |
|------|------|---------|
| Stage1 | 并发探 254 IP 端口(0.5s 超时,并发 150) | ~1-2s(空闲 IP 快速失败) |
| Stage2 | 对 1-5 台候选做 ONVIF probe(每台 0.3-0.5s) | ~1-2s |
| **合计** | 完整 /24 单次扫描 | **~3-8s** |

**关键优化**:别按设备数估时间,要按"空闲 IP 等超时"估。并发 + 短超时 + 两段式是把时间压下来的核心。

### 4.2 细节决定

- **子网推断**:从旧 IP `192.168.4.38` 推 `192.168.4.0/24`。若摄像头被分到别的网段(极少见),手动填 IP 兜底。
- **并发数**:Stage1 ~150 并发(asyncio + 短超时),Stage2 串行或小并发(候选少)。
- **端口探测范围**:TP-Link 一般 80(ONVIF)+ 554(RTSP)。任一端口开放即纳入候选。
- **MAC 来源**:ONVIF `DeviceInformation` 取 `HardwareId`(TP-Link 通常即 MAC)。某型号不给 MAC 时降级用 `SerialNumber`。config 字段叫 `device_mac`,代码内兼容回退到序列号。
- **退避**:未命中轮次间隔 5s。
- **总超时**:默认 30s(可配 `discovery_timeout_seconds`),超时未命中 → 状态置「找不到设备」,开放手动填 IP。

## 5. 与现有代码的集成点

### 5.1 config 字段(新增,保留现有 IP 字段作首次配对入口)

```json
"vision": {
  "rtsp_url": "rtsp://192.168.4.38:554/stream2",
  "rtsp_username": "admin",
  "rtsp_password_env": "RTSP_PASSWORD",
  "device_mac": "",
  "discovery_enabled": true,
  "discovery_timeout_seconds": 30,
  "discovery_subnet": ""
},
"ptz": {
  "ip": "192.168.4.38"
}
```

### 5.2 CameraStream._worker(camera_stream.py 行 565-582)

现有"连续开流失败 → 指数退避 → 用同一个 rtsp_url 重试"改为:

```
连续开流失败 N 次
  └─ discovery_enabled:
        调 discovery_service.find_camera()
        命中 → 拿到新 rtsp_url,下一轮 _resolve_rtsp_url 读到新值,重连
        未命中 → 照旧指数退避,等下一轮再试
     否则:
        维持现有纯指数退避(向后兼容)
```

**discovery 不接管重连循环**,只在循环里加一个"找新 IP"的尝试。worker 仍是唯一持有摄像头连接的地方。改动局部、可测、可关。

### 5.3 PtzService(ptz_service.py)

新增 `notify_ip_changed(new_ip)`:discovery 更新 config 后调用,作废内部缓存的连接,下次 PTZ 动作时懒重连(沿用现有懒加载模式)。

### 5.4 bootstrap.py 启动钩子

```
启动时:
if discovery_enabled and 有 rtsp_url/ip 且 device_mac 为空:
    用现有 IP 连一次 ONVIF → 读 MAC → 写回 config.device_mac
    (失败不影响启动)
```

### 5.5 复用现成资产

- `config_probes.probe_ptz(ip, port, user, pwd)` —— Stage2 验证候选设备直接复用
- `ptz_service.extract_host_from_url(url)` —— 从旧 rtsp_url 提子网
- `update_config_section` —— 回写 config

### 5.6 新增测试

- `tests/test_camera_discovery_service.py` —— 单测发现服务:子网推断、MAC 匹配、两段式扫描、超时、config 回写。ONVIFCamera 全用 `AsyncMock`。
- `tests/conftest.py` test_config 补 `vision` / `ptz` 段(当前缺失)。

## 6. 错误处理与降级

| 情况 | 行为 |
|------|------|
| 现有 IP 失效且首次 MAC 捕获失败 | 不影响启动;掉线时 fallback 到子网全扫(无 MAC 时扫到端口开 + 凭证对的即用) |
| 子网扫描超时未命中 | 状态「找不到设备」,开放手动填 IP;手动填 → probe_ptz 验证 → 写 config |
| 手动填的 IP MAC 对不上(换了摄像头) | 给提示但允许使用,并更新 MAC 记录 |
| ONVIF 不返回 HardwareId | 降级用 SerialNumber |
| discovery_disabled | 完全走现有纯指数退避路径,行为不变(向后兼容) |

## 7. 范围与非目标

**本期做**:
- 单设备 ONVIF 子网单播发现(MAC 匹配)
- 首次 MAC 自动捕获
- RTSP + PTZ 同步更新 + 重连通知
- 掉线被动触发 + 手动发现按钮
- 失败兜底:手动填 IP

**本期不做(YAGNI)**:
- 多设备发现与多路流路由(《多摄像头改造方案.md》单独议题)
- WS-Discovery 多播(已否决)
- DDNS 域名方案
- 云台动作集成(PTZ 只做 IP 重连,动作逻辑不动)

## 8. 验收标准

1. config 里 `device_mac` 为空时启动,用现有 IP 能自动读到 MAC 并写入 config.json。
2. 摄像头换 IP 后,RTSP 连续开流失败时自动触发发现,在 ~30s 内找回新 IP,RTSP 画面恢复。
3. 发现成功后 PTZ 云台控制同时恢复(同一新 IP)。
4. 发现超时未命中时,前端显示「找不到设备」并允许手动填新 IP,手动填后能验证并写入 config。
5. `discovery_enabled=false` 时行为与现状完全一致(向后兼容)。
6. 单测覆盖:子网推断、MAC 匹配、两段式扫描、超时、config 回写。

## 9. 真机验证记录(2026-07-29)

实机环境:TP-Link `TL-IPC43CL-V2`(带云台),固件 `1.0.4 Build 260207`,ONVIF 端口 80,网段 `192.168.4.0/24`。

### 9.1 关键修正:TP-Link 的 HardwareId 不是 MAC(原设计假设有误)

原设计(§4.2)假设 ONVIF `GetDeviceInformation.HardwareId` 就是 MAC。**真机读出来是硬件版本号 `2.0`,不是 MAC**;`SerialNumber` 也只返回 MAC 尾 4 字节(`e3dee054`)。若沿用原假设,首次捕获会把 `"2.0"` 存成身份证,以后永远匹配不到。

修正(commit `ccdbf55`):MAC 读取改为三级优先级:

1. **`GetNetworkInterfaces[].Info.HwAddress`** —— 真正的 MAC,最可靠。实测读到 `60-a3-e3-de-e0-54`。跳过 `Enabled=False` 的网卡。
2. **`GetDeviceInformation.HardwareId`** —— 仅当归一化后是 12 位 hex 才采用(挡掉 `"2.0"` 这类版本号)。
3. **`GetDeviceInformation.SerialNumber`** —— 兜底(非 MAC 但唯一)。

> 教训:**厂商差异只有真机能暴露**,单测 mock 不到。海康/大华一般也能从 `GetNetworkInterfaces` 拿到 MAC,所以优先级 1 通用。

### 9.2 端到端"换 IP 恢复"测试(真机,通过)

模拟摄像头换 IP:用**错误的旧 IP `.250`**(与真实 `.16` 同网段)推断子网,目标 MAC 用真实值,验证能否找回。

| 环节 | 结果 |
|------|------|
| 子网扫描 254 主机(并发 150,0.5s 超时) | **8.2s** 完成 |
| 筛出端口开放候选 | 7 个(路由器 `.1` + 其他设备 + 摄像头 `.16`) |
| ONVIF probe + MAC 匹配 | 精确命中 `192.168.4.16` ✓ |
| `find_camera` 全程耗时 | **10.5s**(远在 30s 超时内) |

结论:即使 config 里的 IP 完全错误,只要摄像头在线 + MAC 存着,**~10 秒找回**,RTSP+PTZ 同步更新后恢复。

### 9.3 实际 config.json 状态(已落盘)

| 字段 | 值 |
|------|------|
| `vision.device_mac` | `60-a3-e3-de-e0-54` |
| `vision.discovery_enabled` | `true` |
| `vision.discovery_timeout_seconds` | `30` |
| `vision.discovery_subnet` | `""`(自动推 `/24`) |

### 9.4 待办(需用户实机)

唯一未由开发侧完成的是**真实 DHCP 换 IP 触发**:在路由器后台给摄像头重新分配 IP 或重启路由器,观察 Aether 日志 `Triggering ONVIF discovery` → `find_camera: matched MAC at <新IP>`,确认画面与云台自动恢复。开发侧已用错误旧 IP 等价验证通过。
