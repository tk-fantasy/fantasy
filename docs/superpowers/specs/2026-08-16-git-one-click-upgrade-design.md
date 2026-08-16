# 设计:运维中心「git 一键升级」(Gitee)

日期:2026-08-16
状态:已实现(认证方式按用户此前明确表达的"填 Gitee token 点更新"偏好定为 HTTPS 令牌;澄清提问未获回复,记录在案)

## 背景与目标

用户以 git 私库(Gitee)+ `docker compose up -d --build` 方式部署,现有升级路径是登主机跑
`scripts/update-from-git.sh`。目标:在运维页「版本与升级」里填一次 Gitee 私有令牌,
之后网页上点一下即完成 拉取→重建→健康自检→(失败)回退,等价于主机脚本。

明确不做的:不改在线更新源通道(镜像包,面向交付);不做定时自动升级;不做分支选择(部署固定跟踪 upstream)。

## 方案取舍

| 方案 | 结论 |
|------|------|
| A. 一次性升级容器(经 docker.sock 拉起,挂宿主仓库执行现有脚本) | ✅ 采用。复用脚本全部逻辑(干净树检查/ff-only/回退/历史记录),无主机驻留 agent |
| B. 宿主机常驻 agent(systemd 服务暴露 API) | 否。多一个要安装维护的组件,交付面变大 |
| C. 容器内直接 git(把仓库整个挂进 aether) | 否。aether 镜像要装 git/compose,攻击面与镜像体积变大,仓库 rw 挂常驻容器风险更高 |

## 组件

1. **升级容器镜像** `scripts/git-updater/`(alpine + git + docker-cli + compose + curl + python3,bash),
   由 docker-compose.yml 新增 service `git-updater`(profile `ops`,默认不启动)构建,产出
   `aether-git-updater:latest`。compose service 本体只 idle 退出(镜像载体);实际升级用后端经
   Docker API 临时创建的 `aether-git-update-run` 容器(非 compose 管理)。
2. **入口包装** `run.sh`(镜像内):注入令牌(GIT_ASKPASS,username/password 都回显令牌——Gitee
   HTTPS 令牌认证);origin 若为 SSH 形式自动 `remote set-url` 改写为 HTTPS;按 `MODE` 分流:
   - `check`:fetch → 比较 HEAD 与 upstream,写结果 JSON,秒级,后端同步等
   - `apply`:执行 `scripts/update-from-git.sh`(HEALTH_URL 走 host.docker.internal 指回宿主),
     tee 日志到 `logs/git-update.log`,结束写 `logs/git-update-result.json`
3. **后端** `app/ops/git_update.py` + `app/routes/ops_routes.py`:
   - 仓库路径自动探测:inspect `aether` 容器 label `com.docker.compose.project.working_dir`
     (config `update.git_repo_path` 可覆盖;都没有则报错引导)
   - `GET/PUT /api/ops/update/git` 令牌与仓库信息(GET 只回 configured/masked,永不回明文)
   - `POST /api/ops/update/git/check` 同步(超时 120s)
   - `POST /api/ops/update/git/apply` 异步拉起容器即返回;已在跑则 409
   - `GET /api/ops/update/git/status` 容器运行态 + 结果 JSON + 日志尾 200 行
   - 结果文件经 aether 已挂载的 `./logs` 直接读到,无需容器间通信
4. **令牌存储**:config.json `update.git_token`(与 LLM 密钥同级保护;键名含 token,
   诊断包脱敏自动覆盖)。仅通过环境变量传入升级容器。
5. **前端** OperationsView「版本与升级」新增「git 一键升级」行:令牌保存、检查更新
   (显示当前/远程 commit 与落后数)、一键升级(二次确认→重启等待遮罩,Pi 上构建需数分钟,
   轮询超时从 3 分钟放宽到 15 分钟;期间后端不可达时回落到 /api/health 轮询)。
6. **审计**:check/apply/令牌保存均写 ops 审计;升级历史沿用脚本写入的记录(operator=git)。

## 错误处理

- 工作树脏 / ff-only 分叉 / 令牌失效:脚本或 fetch 失败,结果 JSON 带 error,状态接口展示日志尾。
- 健康检查不过:脚本自动 reset --hard 回上一 commit 重建(既有行为),历史记录标注"已回退"。
- 升级中重复点击:容器运行态检测 → 409。
- 构建中断导致服务长期不回:前端 15 分钟超时提示人工查看。

## 安全说明

升级容器挂 docker.sock + 宿主仓库 rw,权限等同主机用户——与现有 docker.sock 挂载(compose
内已注释说明)同级信任,仅限本机可信自用部署。令牌不写日志、不出现在 GET 响应。

## 测试

- pytest:容器创建 payload 构建(挂载/env/镜像)、仓库路径探测回退、结果文件解析、令牌
  配置读写与掩码、路由注册。Docker 依赖的链路在部署侧人工验收(文档附验收步骤)。

## 验收(部署侧)

1. `docker compose --profile ops up -d --build git-updater` 构建升级镜像(首次);
   `scripts/update-from-git.sh` 从本次起也会一并构建它。
2. 运维页填令牌 → 检查更新应显示落后提交数;推新 commit 后一键升级 → 等待 → 版本变化 +
   升级历史新增 operator=git 记录。
