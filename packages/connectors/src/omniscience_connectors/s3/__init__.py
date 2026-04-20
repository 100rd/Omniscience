"""S3 source connector for Omniscience.

Supports Amazon S3 buckets with explicit credentials, named profiles,
or the default credential chain (instance role, env vars, etc.).
Push-style updates are supported via S3 Event Notifications (direct or SNS-wrapped).
"""

from omniscience_connectors.s3.connector import S3Config, S3Connector
from omniscience_connectors.s3.webhook import S3WebhookHandler

__all__ = [
    "S3Config",
    "S3Connector",
    "S3WebhookHandler",
]
