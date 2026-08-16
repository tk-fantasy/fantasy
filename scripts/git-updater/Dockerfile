# git 一键升级容器（运维页「git 一键升级」的实际执行者，一次性运行）。
#
# 镜像由 compose 的 git-updater service（profile: ops）构建，产出 aether-git-updater:latest。
# 实际升级不跑 compose service 本体，而是后端经 Docker API 临时创建的容器
# （见 app/ops/git_update.py），挂载宿主仓库与 docker.sock 执行 scripts/update-from-git.sh。
#
# 为什么单独做镜像而不在 aether 镜像里装 git/compose：升级工具链（git、compose、bash）
# 只在升级的几分钟里需要，常驻 aether 容器没必要背这个体积与攻击面。
FROM alpine:3.20

# docker-cli + docker-compose-plugin：alpine community 源（edge 跟进慢时用官方静态包）
# git/python3/curl/bash/jq：update-from-git.sh 与 run.sh 的全部依赖
RUN apk add --no-cache bash git python3 curl jq docker-cli docker-cli-compose

WORKDIR /repo
COPY run.sh /usr/local/bin/run.sh
RUN chmod +x /usr/local/bin/run.sh

ENTRYPOINT ["/usr/local/bin/run.sh"]
