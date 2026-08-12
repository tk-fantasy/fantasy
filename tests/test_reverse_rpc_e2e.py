"""反向 RPC 全链路 e2e + 死锁专项（Phase 3）。

用真实子进程跑 ``reverse`` 测试插件：插件 handle 内反向调宿主 ``ha.call_service`` /
``host.broadcast``。旧串行 runtime（read→handle→write）在此会死锁——handle await 宿主
响应时无人读 stdin；新并发 runtime（后台 reader + writer + 反向 future map）正常完成。

宿主侧用 in-process IntegrationLayer + mock ha_client。即便 runtime 仍死锁，测试也不会
永久挂起：``proc.call`` 的 rpc_timeout 会超时抛 RuntimeError。
"""
import asyncio
from unittest.mock import AsyncMock

from app.integration.integration_layer import IntegrationLayer

PLUGINS_DIR = "tests/integrations"


def test_reverse_rpc_roundtrip_no_deadlock():
    """handle 内反向调 ha.call_service，全链路返回，不死锁。"""
    ha_client = AsyncMock()
    ha_client.call_service.return_value = {"ok": True, "status": 200}
    layer = IntegrationLayer(
        plugin_dir=PLUGINS_DIR,
        api_version="1", rpc_timeout=15.0, max_restarts=0,
        host_deps={"ha_client": ha_client, "ha_service": None, "llm_chat_client": None},
    )

    async def go():
        try:
            await layer.start()
            proc = layer._supervisor.get_process("reverse")
            assert proc is not None and proc.is_alive, "reverse 插件未启动"

            result = await proc.call("test.call_host", {
                "domain": "light", "service": "turn_on", "entity_id": "light.living",
            })
            assert result["reversed"] is True
            assert result["host_result"]["status"] == 200

            # 宿主 ha_client 被插件经反向 RPC 调用一次，参数透传正确
            ha_client.call_service.assert_awaited_once()
            args = ha_client.call_service.call_args.args
            assert args[0] == "light"          # domain
            assert args[1] == "turn_on"        # service
            assert args[2] == "light.living"   # entity_id
        finally:
            await layer.stop()

    asyncio.new_event_loop().run_until_complete(go())


def test_reverse_rpc_broadcast_path():
    """handle 内反向调 host.broadcast，全链路返回（reverse 无 output_sink，broadcast 为 no-op 但路径走通）。"""
    layer = IntegrationLayer(
        plugin_dir=PLUGINS_DIR,
        api_version="1", rpc_timeout=15.0, max_restarts=0,
        host_deps={"ha_client": AsyncMock(), "ha_service": None, "llm_chat_client": None},
    )

    async def go():
        try:
            await layer.start()
            proc = layer._supervisor.get_process("reverse")
            assert proc is not None and proc.is_alive
            result = await proc.call("test.broadcast", {"text": "hi", "msg_id": "m1"})
            assert result["broadcasted"] is True
        finally:
            await layer.stop()

    asyncio.new_event_loop().run_until_complete(go())
