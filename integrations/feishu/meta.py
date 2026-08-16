"""飞书集成元信息（供插件管理页显示）。"""

NAME = "飞书机器人"
VERSION = "1.0.1"
DESCRIPTION = "飞书聊天机器人（WebSocket 长连接，私聊/群聊 @机器人）"
CAPABILITIES = ["host_integration"]  # 宿主侧集成（非子进程）

# 管理页弹窗配置表单声明。secret 字段读取时脱敏回显、保存留空=保持原值；
# 未在界面配置时回退 .env（FEISHU_APP_ID 等），老部署无需任何迁移。
CONFIG_SCHEMA = {
    "app_id": {
        "type": "string",
        "required": True,
        "label": "App ID",
        "placeholder": "cli_ 开头，飞书开放平台 → 凭证与基础信息",
    },
    "app_secret": {
        "type": "secret",
        "required": True,
        "label": "App Secret",
        "placeholder": "与 App ID 同页",
    },
    "verification_token": {
        "type": "secret",
        "required": False,
        "label": "Verification Token",
        "placeholder": "事件与回调 → 加密策略（可选）",
    },
    "encrypt_key": {
        "type": "secret",
        "required": False,
        "label": "Encrypt Key",
        "placeholder": "未启用事件加密可留空",
    },
}
