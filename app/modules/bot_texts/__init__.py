"""Bot texts module for dynamic localization and admin text management."""
from app.modules.bot_texts.service import get_bot_text, update_bot_texts, get_all_bot_texts

__all__ = ["get_bot_text", "update_bot_texts", "get_all_bot_texts"]
