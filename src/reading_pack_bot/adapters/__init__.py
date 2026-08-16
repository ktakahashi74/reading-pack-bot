"""Platform adapters."""

from .discord import DiscordAdapter
from .slack import SlackAdapter, split_message

__all__ = ["DiscordAdapter", "SlackAdapter", "split_message"]
