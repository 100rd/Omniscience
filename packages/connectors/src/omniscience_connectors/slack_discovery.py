"""Slack channel discovery connector."""

from __future__ import annotations

from typing import Any
import httpx
import structlog
from omniscience_connectors.discovery import DiscoveredSource

log = structlog.get_logger(__name__)

class SlackDiscoveryConnector:
    """Discovers public and private channels in a Slack workspace."""

    async def discover(self, config: dict[str, Any], secrets: dict[str, str]) -> list[DiscoveredSource]:
        token = secrets.get("slack_token")
        if not token:
            log.error("slack_discovery_missing_token")
            return []

        discovered = []
        async with httpx.AsyncClient() as client:
            url = "https://slack.com/api/conversations.list"
            headers = {"Authorization": f"Bearer {token}"}
            params = {
                "types": config.get("channel_types", "public_channel,private_channel"),
                "exclude_archived": "true",
                "limit": 1000
            }
            
            try:
                response = await client.get(url, headers=headers, params=params)
                data = response.json()
                
                if not data.get("ok"):
                    log.error("slack_discovery_api_error", error=data.get("error"))
                    return []

                for channel in data.get("channels", []):
                    discovered.append(DiscoveredSource(
                        name=f"slack-{channel['name']}",
                        type="slack",
                        uri=f"slack://{channel['id']}",
                        external_id=channel['id'],
                        metadata={
                            "topic": channel.get("topic", {}).get("value"),
                            "purpose": channel.get("purpose", {}).get("value"),
                            "num_members": channel.get("num_members")
                        }
                    ))
                
                log.info("slack_discovery_success", count=len(discovered))
            except Exception as exc:
                log.error("slack_discovery_failed", error=str(exc))
                
        return discovered
