"""Git source connector for Omniscience.

Supports local paths and remote repositories (GitHub/GitLab) over HTTPS or SSH.
Push-style updates are supported via GitHub/GitLab webhooks.
"""

from omniscience_connectors.git.connector import GitConfig, GitConnector
from omniscience_connectors.git.webhook import GitWebhookHandler

__all__ = [
    "GitConfig",
    "GitConnector",
    "GitWebhookHandler",
]
