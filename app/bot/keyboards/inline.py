"""Inline Keyboards for purchase workflow, subscription management, and admin actions."""
from typing import List, Optional
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from app.database.models.product import Product
from app.database.models.panel import Server
from app.database.models.subscription import Subscription
from app.core.constants import PaymentMethod
from app.core.config import settings


def _trim_callback_data(prefix: str, data: str, max_bytes: int = 64) -> str:
    """Ensure prefix + data does not exceed Telegram's strict 64-byte callback_data limit."""
    prefix_bytes = len(prefix.encode("utf-8"))
    allowed_data_bytes = max_bytes - prefix_bytes
    if allowed_data_bytes <= 0:
        return prefix.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    data_bytes = data.encode("utf-8")
    if len(data_bytes) <= allowed_data_bytes:
        return f"{prefix}{data}"
    trimmed = data_bytes[:allowed_data_bytes].decode("utf-8", errors="ignore")
    return f"{prefix}{trimmed}"


def get_products_keyboard(products: List[Product]) -> InlineKeyboardMarkup:
    """List available VPN subscription packages."""
    inline_keyboard = []
    for prod in products:
        label = f"📦 {prod.name} — {prod.price_toman:,} تومان"
        inline_keyboard.append([
            InlineKeyboardButton(text=label, callback_data=_trim_callback_data("buy_prod:", str(prod.id)))
        ])
    inline_keyboard.append([InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_action")])
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def get_locations_keyboard(servers: List[Server]) -> InlineKeyboardMarkup:
    """Show distinct locations available."""
    inline_keyboard = []
    seen = set()
    row = []
    for s in servers:
        if s.location_code not in seen:
            seen.add(s.location_code)
            btn = InlineKeyboardButton(
                text=f"{s.flag_emoji} {s.location_name}",
                callback_data=_trim_callback_data("sel_loc:", str(s.location_code)),
            )
            row.append(btn)
            if len(row) == 2:
                inline_keyboard.append(row)
                row = []
    if row:
        inline_keyboard.append(row)

    # Any location option
    inline_keyboard.append([
        InlineKeyboardButton(text="⚡️ بهترین لوکیشن (خودکار)", callback_data="sel_loc:ALL")
    ])
    inline_keyboard.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="back_to_products")])
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def get_payment_methods_keyboard(
    order_id: str,
    can_wallet: bool = True,
    active_providers: dict = None,
) -> InlineKeyboardMarkup:
    """Select payment gateway or wallet dynamically based on enabled settings and DB gateways."""
    from app.core.config import settings
    buttons = []
    if can_wallet:
        buttons.append([
            InlineKeyboardButton(
                text="💰 پرداخت مستقیم از موجودی کیف پول",
                callback_data=_trim_callback_data("pay_method:", f"{order_id}:{PaymentMethod.WALLET}"),
            )
        ])

    provs = active_providers or {}

    # 1. Card to Card
    c2c_active = provs.get("card_to_card", getattr(settings, "CARD_TO_CARD_ENABLED", True))
    if c2c_active:
        buttons.append([
            InlineKeyboardButton(
                text="💳 کارت به کارت (کارت بانکی)",
                callback_data=_trim_callback_data("pay_method:", f"{order_id}:{PaymentMethod.CARD_TO_CARD}"),
            )
        ])

    # 2. SEP (Saman Shaparak)
    sep_active = provs.get("sep", getattr(settings, "SEP_ENABLED", False))
    if sep_active:
        buttons.append([
            InlineKeyboardButton(
                text="🌐 درگاه پرداخت سامان شاپرک (SEP)",
                callback_data=_trim_callback_data("pay_method:", f"{order_id}:{PaymentMethod.SEP}"),
            )
        ])

    # 3. IDPay
    idpay_active = provs.get("idpay", getattr(settings, "IDPAY_ENABLED", False))
    if idpay_active:
        buttons.append([
            InlineKeyboardButton(
                text="🌐 درگاه پرداخت آنلاین آیدی پی (IDPay)",
                callback_data=_trim_callback_data("pay_method:", f"{order_id}:{PaymentMethod.IDPAY}"),
            )
        ])

    # 4. ZarinPal
    zarinpal_active = provs.get("zarinpal", getattr(settings, "ZARINPAL_ENABLED", False))
    if zarinpal_active:
        buttons.append([
            InlineKeyboardButton(
                text="🌐 درگاه پرداخت آنلاین زرین‌پال",
                callback_data=_trim_callback_data("pay_method:", f"{order_id}:{PaymentMethod.ZARINPAL}"),
            )
        ])

    # 5. Crypto
    crypto_active = provs.get("crypto", getattr(settings, "CRYPTO_ENABLED", True))
    if crypto_active:
        buttons.append([
            InlineKeyboardButton(
                text="🪙 پرداخت با تتر / رمزارز (USDT)",
                callback_data=_trim_callback_data("pay_method:", f"{order_id}:{PaymentMethod.CRYPTO}"),
            )
        ])

    # 6. Perfect Money
    pm_active = provs.get("perfect_money", getattr(settings, "PERFECT_MONEY_ENABLED", False))
    if pm_active:
        buttons.append([
            InlineKeyboardButton(
                text="💵 پرداخت با پرفکت مانی (Perfect Money)",
                callback_data=_trim_callback_data("pay_method:", f"{order_id}:{PaymentMethod.PERFECT_MONEY}"),
            )
        ])

    # 7. Stripe International (Visa / Mastercard / Apple Pay / Google Pay)
    stripe_active = provs.get("stripe", getattr(settings, "STRIPE_ENABLED", False))
    if stripe_active:
        buttons.append([
            InlineKeyboardButton(
                text="💳 پرداخت بین‌المللی ارزی (Stripe - Visa/Mastercard)",
                callback_data=_trim_callback_data("pay_method:", f"{order_id}:{PaymentMethod.STRIPE}"),
            )
        ])

    buttons.append([InlineKeyboardButton(text="❌ لغو سفارش", callback_data="cancel_action")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_subscription_actions_keyboard(
    sub_id: str,
    protocol: Optional[str] = None,
    auto_renew: bool = False,
) -> InlineKeyboardMarkup:
    """Actions on an existing active subscription. Strictly ensures all callback_data <= 64 bytes."""
    sub_id_str = str(sub_id)
    rows = [
        [
            InlineKeyboardButton(text="📱 دریافت بارکد QR", callback_data=_trim_callback_data("sub_qr:", sub_id_str)),
            InlineKeyboardButton(text="🔄 تمدید یا ارتقای پلن", callback_data=_trim_callback_data("sub_renew:", sub_id_str)),
        ]
    ]

    proto_lower = (protocol or "").strip().lower()
    if proto_lower in ("wireguard", "amneziawg", "openvpn", "wg", "awg", "ovpn"):
        rows.append([
            InlineKeyboardButton(
                text="📥 دریافت فایل کانفیگ (.conf)",
                callback_data=_trim_callback_data("sub_conf:", sub_id_str),
            )
        ])

    renew_icon = "فعال ✅" if auto_renew else "غیرفعال ❌"
    rows.append([
        InlineKeyboardButton(
            text=f"🔄 تمدید خودکار کیف‌پول: {renew_icon}",
            callback_data=_trim_callback_data("sub_autorenew:", sub_id_str),
        ),
    ])

    rows.append([
        InlineKeyboardButton(text="📊 بررسی ترافیک زنده", callback_data=_trim_callback_data("sub_sync:", sub_id_str)),
        InlineKeyboardButton(text="🆘 اتصال اضطراری (۲۴س)", callback_data=_trim_callback_data("sub_grace:", sub_id_str)),
    ])
    rows.append([
        InlineKeyboardButton(text="🗑 حذف این سرویس", callback_data=_trim_callback_data("sub_delete_ask:", sub_id_str)),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_admin_payment_approval_keyboard(payment_id: str) -> InlineKeyboardMarkup:
    """Admin review keyboard for pending Card-to-Card receipts."""
    p_id_str = str(payment_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تایید و فعال‌سازی سرویس",
                    callback_data=_trim_callback_data("admin_pay_approve:", p_id_str),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ رد پرداخت",
                    callback_data=_trim_callback_data("admin_pay_reject:", p_id_str),
                ),
            ],
        ]
    )


def get_wallet_inline_keyboard() -> InlineKeyboardMarkup:
    """Wallet menu options including topup, voucher card registration, and buy."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ افزایش موجودی / شارژ کیف پول", callback_data="wallet_topup_start")
            ],
            [
                InlineKeyboardButton(text="🎁 ثبت کارت ووچر (کارت هدیه)", callback_data="voucher_redeem_start")
            ],
            [
                InlineKeyboardButton(text="🛒 خرید سرویس جدید", callback_data="open_buy_menu")
            ],
        ]
    )


def get_voucher_cancel_keyboard() -> InlineKeyboardMarkup:
    """Cancel button when entering voucher code."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔙 انصراف و بازگشت", callback_data="voucher_cancel")
            ]
        ]
    )


def get_voucher_success_keyboard(voucher_type: str = "plan", sub_id: str = "") -> InlineKeyboardMarkup:
    """Action buttons after successfully redeeming a voucher."""
    if voucher_type == "plan" and sub_id:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📦 مشاهده در سرویس‌های من", callback_data="my_services_menu"),
                    InlineKeyboardButton(text="📱 دریافت مجدد بارکد QR", callback_data=_trim_callback_data("sub_qr:", str(sub_id))),
                ]
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🛒 خرید سرویس با اعتبار کیف پول", callback_data="open_buy_menu"),
            ],
            [
                InlineKeyboardButton(text="💳 مشاهده موجودی کیف پول", callback_data="wallet_view"),
            ]
        ]
    )
