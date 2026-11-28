import pytest
from app.modules.bot_texts.service import DEFAULT_BOT_TEXTS, get_bot_text, update_bot_texts
from app.bot.handlers.referral import DEFAULT_REFERRAL_TEMPLATE


def test_referral_guide_default_config():
    """Verify referral_guide entry in DEFAULT_BOT_TEXTS."""
    entry = DEFAULT_BOT_TEXTS.get("referral_guide")
    assert entry is not None
    assert "referral_link" in entry["default"]
    assert "commission_percent" in entry["default"]
    assert "invited_count" in entry["default"]
    assert "commission_toman" in entry["default"]


def test_referral_template_formatting():
    """Verify dynamic substitution of referral template variables."""
    template = DEFAULT_REFERRAL_TEMPLATE
    formatted = (
        template
        .replace("{referral_link}", "https://t.me/TestBot?start=REF123")
        .replace("{commission_percent}", "15")
        .replace("{invited_count}", "7")
        .replace("{commission_toman}", "50,000")
        .replace("{bot_username}", "TestBot")
    )

    assert "https://t.me/TestBot?start=REF123" in formatted
    assert "15٪" in formatted
    assert "7 نفر" in formatted
    assert "50,000 تومان" in formatted
    assert "{referral_link}" not in formatted


def test_referral_custom_bot_text():
    """Verify custom bot text can be saved and loaded for referral_guide."""
    orig = get_bot_text("referral_guide")
    try:
        custom_msg = "دعوت اختصاصی: {referral_link} با پورسانت {commission_percent}%"
        update_bot_texts({"referral_guide": custom_msg})

        loaded = get_bot_text("referral_guide")
        assert loaded == custom_msg

        # Test format on loaded
        formatted = loaded.replace("{referral_link}", "https://t.me/MyBot?start=ABC").replace("{commission_percent}", "20")
        assert formatted == "دعوت اختصاصی: https://t.me/MyBot?start=ABC با پورسانت 20%"
    finally:
        update_bot_texts({"referral_guide": DEFAULT_BOT_TEXTS["referral_guide"]["default"]})
