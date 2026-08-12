"""验证 lifespan shutdown 重置进程级全局状态（防僵尸对象）。

背景：``main.dispatcher`` 在 lifespan 内被赋值，shutdown 只调
``close_all_agent_clients()`` 回收 httpx 客户端，从不置回 None → 进程内重启
（uvicorn --reload / 测试复用进程）时仍指向已关闭的旧对象。
``_reset_global_state()`` 在 shutdown 末尾解除这些引用；此处直接单测该函数，
无需跑完整 lifespan（lifespan 会连 HA / 起 RAG，单测环境不可行）。
"""
import app.main as main


def test_reset_global_state_clears_runtime_globals():
    """_reset_global_state 把 dispatcher / _services 载体清回初始态。"""
    # 模拟 lifespan 期间注入的运行时对象
    main.dispatcher = object()
    main.langgraph_agent = object()
    main._services["langgraph_agent"] = object()
    main._services["langchain_tools"] = [object()]

    main._reset_global_state()

    assert main.dispatcher is None
    assert main.langgraph_agent is None
    assert main._services["langgraph_agent"] is None
    assert "langchain_tools" not in main._services


def test_reset_global_state_is_idempotent():
    """连续调用两次不报错（shutdown 可能被重复触发）。"""
    main._reset_global_state()
    main._reset_global_state()
    assert main.dispatcher is None
