"""Persian localization strings and message templates for VPN Private."""

WELCOME_MSG = """
🔒 <b>به پلتفرم هوشمند VPN Private خوش آمدید!</b> 🌐💚

با VPN Private، به اینترنت پرسرعت، پایدار و امن با بیش از <b>۲۵ لوکیشن متنوع</b> متصل شوید!

✨ <b>امکانات ویژه:</b>
✅ بیش از ۲۵ لوکیشن متنوع (آلمان، فنلاند، هلند، ترکیه، آمریکا و...)
✅ امکان انتخاب لوکیشن دلخواه از طریق اپلیکیشن <b>Happ</b>
✅ پلن‌های متنوع حجمی و نامحدود متناسب با هر نوع نیاز
✅ پروتکل‌های ضدفیلتر: <b>VLESS (Reality), Trojan, VMess</b>
✅ پشتیبانی دائمی و تایید آنی سفارشات

📢 <b>کانال رسمی:</b> <a href="https://t.me/ADVPN_Private">@ADVPN_Private</a>
🎧 <b>پشتیبانی تلگرام:</b> <a href="https://t.me/VPNPrivateSupport">@VPNPrivateSupport</a>

جهت مشاهده و خرید اشتراک، از منوی زیر گزینه <b>🛒 خرید سرویس</b> را انتخاب فرمایید:
"""

MAIN_MENU_BTNS = {
    "buy": "🛒 خرید سرویس",
    "my_services": "📦 سرویس‌های من",
    "renew": "🔄 تمدید سرویس",
    "usage": "📊 وضعیت مصرف",
    "wallet": "💳 کیف پول",
    "voucher": "🎁 ثبت کارت ووچر",
    "lucky_wheel": "🎡 گردونه شانس روزانه",
    "gift_card": "🎁 کارت هدیه تلگرامی",
    "trial": "🎁 اکانت تست رایگان",
    "discount": "🎟 کد تخفیف",
    "referral": "👥 دعوت از دوستان",
    "support": "🎧 پشتیبانی",
    "tutorial": "📚 آموزش اتصال",
    "rules": "📜 قوانین و شرایط",
    "reseller": "💼 پنل نمایندگی",
    "admin": "⚙️ پنل مدیریت",
}

RULES_TEXT = """
📜 <b>قوانین و شرایط استفاده از VPN Private:</b>

۱. استفاده از سرویس‌ها صرفاً جهت دورزدن تحریم‌ها، وب‌گردی آزاد و استفاده قانونی می‌باشد.
۲. هرگونه فعالیت مجرمانه، فیشینگ، اسپم و حملات سایبری اکیداً ممنوع بوده و منجر به مسدودسازی حساب بدون بازگشت وجه خواهد شد.
۳. پشتیبانی ۲۴ ساعته از طریق سیستم تیکتینگ اختصاصی در دسترس است.
۴. مهلت تست و تضمین اتصال پایدار به مدت ۴۸ ساعت پس از فعال‌سازی می‌باشد.
"""

TUTORIALS_TEXT = """
📚 <b>راهنمای جامع اتصال به سرورها:</b>

📱 <b>اندروید:</b>
• برنامه‌های پیشنهادی: <code>v2rayNG</code>, <code>NekoBox</code>, <code>Happ</code>
• لینک اشتراک دریافتی را کپی کرده و در برنامه با گزینه <b>+</b> ➔ <b>Import config from clipboard</b> وارد کنید.

🍏 <b>آیفون (iOS):</b>
• برنامه‌های پیشنهادی: <code>Streisand</code>, <code>V2Box</code>, <code>Foxray</code>, <code>Shadowrocket</code>
• لینک اشتراک را اضافه کرده و دکمه اتصال را بزنید.

💻 <b>ویندوز و مک:</b>
• نرم‌افزارهای پیشنهادی: <code>NekoRay</code>, <code>v2rayN</code>, <code>Hiddify Next</code>
"""

def format_toman(amount_irr: int) -> str:
    if amount_irr is None:
        return "۰ تومان"
    try:
        toman = int(amount_irr) // 10
        return f"{toman:,} تومان"
    except (ValueError, TypeError):
        return "۰ تومان"
