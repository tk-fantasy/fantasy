"""Health check service for startup validation and runtime degradation."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class HealthChecker:
    """Tracks availability of external services (HA, LLM, etc.)."""

    ha_available: bool = False
    llm_available: bool = False

    async def check_ha(self, ha_client: Any) -> bool:
        """Check if Home Assistant is reachable."""
        try:
            states = await asyncio.wait_for(ha_client.get_states(), timeout=5.0)
            self.ha_available = len(states) > 0
            if self.ha_available:
                logger.info("HA health check: OK (%d entities)", len(states))
            else:
                logger.warning("HA health check: no entities found")
        except asyncio.TimeoutError:
            self.ha_available = False
            logger.warning("HA health check: timeout (5s)")
        except Exception as e:
            self.ha_available = False
            logger.warning("HA health check: failed - %s", e)
        return self.ha_available

    async def check_llm(self, llm_chat_client: Any) -> bool:
        """Check if LLM service is reachable."""
        if not llm_chat_client.enabled:
            self.llm_available = False
            logger.warning("LLM health check: disabled in config")
            return False
        try:
            # Simple test: send minimal request
            result = await asyncio.wait_for(
                llm_chat_client.chat([{"role": "user", "content": "hi"}], timeout=10.0),
                timeout=15.0,
            )
            self.llm_available = bool(result)
            if self.llm_available:
                logger.info("LLM health check: OK")
            else:
                logger.warning("LLM health check: empty response")
        except asyncio.TimeoutError:
            self.llm_available = False
            logger.warning("LLM health check: timeout (15s)")
        except Exception as e:
            self.llm_available = False
            logger.warning("LLM health check: failed - %s", e)
        return self.llm_available

    async def check_all(self, ha_client: Any, llm_chat_client: Any) -> dict[str, bool]:
        """Run all health checks and return status dict."""
        await asyncio.gather(
            self.check_ha(ha_client),
            self.check_llm(llm_chat_client),
        )
        return {
            "ha": self.ha_available,
            "llm": self.llm_available,
        }

    def get_status(self) -> dict[str, Any]:
        """Get current health status for API response."""
        return {
            "ha_available": self.ha_available,
            "llm_available": self.llm_available,
        }
