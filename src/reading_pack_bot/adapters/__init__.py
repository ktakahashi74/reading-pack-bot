"""Platform adapters."""

from .slack import SlackAdapter, split_message

__all__ = ["SlackAdapter", "split_message"]
