"""Slack source connector."""

from omniscience_connectors.slack.connector import SlackConfig, SlackConnector
from omniscience_connectors.slack.entities import (
    EXTRACTOR_VERSION,
    MENTION_KIND,
    EdgeData,
    EntityData,
    extract_mentions,
    extract_slack_graph,
    slack_thread_name,
)

__all__ = [
    "EXTRACTOR_VERSION",
    "MENTION_KIND",
    "EdgeData",
    "EntityData",
    "SlackConfig",
    "SlackConnector",
    "extract_mentions",
    "extract_slack_graph",
    "slack_thread_name",
]
