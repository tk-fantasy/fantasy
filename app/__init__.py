"""Aether application package."""
import os

# 本项目所有服务（HA/MQTT 本地 + 智谱/通义/和风天气国内）均不需要代理。
# 在最早期注入 NO_PROXY，防止 Clash 等系统代理拦截 localhost 和国内 API。
_no_proxy = os.environ.get("NO_PROXY", "")
for _host in ("localhost", "127.0.0.1", "0.0.0.0"):
    if _host not in _no_proxy:
        _no_proxy = f"{_no_proxy},{_host}" if _no_proxy else _host
os.environ["NO_PROXY"] = _no_proxy
os.environ.setdefault("no_proxy", _no_proxy)
