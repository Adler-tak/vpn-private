"""Payment webhook and callback routes for Zarinpal, SEP (Saman), and IDPay."""
import logging
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request, Form
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.session import get_db
from app.database.models.payment import Payment, PaymentGateway
from app.database.models.user import User
from app.modules.orders.service import OrderService
from app.modules.subscriptions.service import SubscriptionService
from app.integrations.payments.zarinpal import ZarinpalGateway
from app.integrations.payments.sep import SEPGateway
from app.integrations.payments.idpay import IDPayGateway
from app.integrations.payments.perfect_money import PerfectMoneyGateway
from app.core.constants import PaymentStatus, PaymentMethod
from app.bot.main import create_bot

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_bot_return_link() -> str:
    try:
        from app.services.settings_service import get_effective_bot_username_sync
        u = get_effective_bot_username_sync()
        if u:
            return f"https://t.me/{u}"
    except Exception:
        pass
    return "https://t.me/"


async def _handle_payment_success_notification(db: AsyncSession, payment: Payment, subscription, ref_id: str, provider_name: str):
    """Notify user via Telegram Bot about approved subscription or wallet credit."""
    try:
        user = await db.get(User, payment.user_id)
        if not user or not user.telegram_id:
            return

        bot = create_bot()
        if subscription:
            sub_service = SubscriptionService(db)
            conn = await sub_service.get_connection_details(subscription.id)
            msg = (
                f"🎉 <b>پرداخت آنلاین شما از طریق درگاه {provider_name} با موفقیت تایید شد!</b>\n\n"
                f"شماره پیگیری بانکی: <code>{ref_id}</code>\n"
                f"🏷 شناسه سرویس: <code>{subscription.client_email}</code>\n"
                f"🌍 لوکیشن: {conn['flag']} <b>{conn['location']}</b>\n\n"
                f"🔗 <b>لینک سابسکرایب هوشمند:</b>\n<code>{conn['subscription_url']}</code>\n\n"
                f"📱 <b>نرم‌افزار پیشنهادی جهت اتصال (Happ Proxy):</b>\n"
                f"🍏 <a href=\"https://apps.apple.com/app/happ-proxy-utility/id6504287215\">دانلود نسخه iOS (آیفون)</a>\n"
                f"🤖 <a href=\"https://play.google.com/store/apps/details?id=com.happproxy\">دانلود نسخه اندروید</a>\n\n"
                f"💡 <i>لینک سابسکرایب بالا را کپی کرده یا درون اپلیکیشن وارد نمایید.</i>"
            )
        else:
            msg = (
                f"🎉 <b>شارژ آنلاین کیف پول شما با موفقیت انجام شد!</b>\n\n"
                f"💰 مبلغ افزوده شده: <b>{payment.amount_toman:,} تومان</b>\n"
                f"🔖 شماره تراکنش بانکی: <code>{ref_id}</code>\n"
                f"🏦 درگاه: <b>{provider_name}</b>\n\n"
                f"اکنون می‌توانید از موجودی کیف پول خود جهت خرید و تمدید اشتراک استفاده فرمایید."
            )
        await bot.send_message(chat_id=user.telegram_id, text=msg, parse_mode="HTML")
        await bot.session.close()
    except Exception as e:
        logger.error(f"Failed to send confirmation telegram message: {e}")


# =========================================================================
# 1. ZARINPAL CALLBACK
# =========================================================================
@router.get("/zarinpal/callback", response_class=HTMLResponse)
async def zarinpal_callback(
    Authority: str = Query(...),
    Status: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Handle Zarinpal payment redirection callback."""
    payment_res = await db.execute(
        select(Payment).where(Payment.tracking_code == Authority)
    )
    payment = payment_res.scalar_one_or_none()

    if not payment:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:red;'>خطا: شناسه پرداخت یافت نشد.</h2>"
            "</body></html>",
            status_code=404,
        )

    if payment.status == PaymentStatus.PAID:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:green;'>این پرداخت قبلاً با موفقیت تایید و سرویس فعال شده است.</h2>"
            "<p>لطفاً به ربات تلگرام بازگردید.</p>"
            "</body></html>"
        )

    if Status.upper() != "OK":
        payment.status = PaymentStatus.REJECTED
        await db.commit()
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:red;'>پرداخت توسط کاربر لغو شد یا ناموفق بود.</h2>"
            f"<p><a href='{_get_bot_return_link()}'>بازگشت به ربات</a></p>"
            "</body></html>"
        )

    # Resolve gateway config if available
    merchant_id = None
    sandbox = None
    if payment.gateway_id:
        gw = await db.get(PaymentGateway, payment.gateway_id)
        if gw:
            cfg = gw.config_dict
            merchant_id = cfg.get("merchant_id")
            sandbox = cfg.get("sandbox", False)

    gateway = ZarinpalGateway(merchant_id=merchant_id, sandbox=sandbox)
    verify_res = await gateway.verify_payment(
        authority=Authority,
        amount_irr=payment.amount_irr,
    )

    if not verify_res.success:
        payment.status = PaymentStatus.REJECTED
        await db.commit()
        return HTMLResponse(
            f"<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            f"<h2 style='color:red;'>تایید پرداخت زرین‌پال با خطا مواجه شد:</h2>"
            f"<p>{verify_res.error_message}</p>"
            f"</body></html>"
        )

    order_service = OrderService(db)
    subscription = await order_service.approve_and_provision_order(
        payment_id=payment.id,
        reference_id=verify_res.reference_id,
        admin_notes="Zarinpal Online Verification",
    )
    await db.commit()

    await _handle_payment_success_notification(
        db, payment, subscription, verify_res.reference_id, "زرین‌پال"
    )

    return HTMLResponse(
        "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
        "<h2 style='color:green;'>✅ پرداخت با موفقیت انجام شد و سرویس شما فعال گردید!</h2>"
        f"<p>شماره تراکنش: <b>{verify_res.reference_id}</b></p>"
        "<p>اطلاعات کانفیگ به ربات تلگرام شما ارسال شد. لطفاً به تلگرام بازگردید.</p>"
        "</body></html>"
    )


# =========================================================================
# 2. SEP (SAMAN ELECTRONIC PAYMENT - پرداخت سامان) ROUTES
# =========================================================================
@router.get("/sep/pay/{payment_id}", response_class=HTMLResponse)
async def sep_pay_redirect(payment_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Auto-submitting HTML form page posting Token to SEP Shaparak."""
    payment = await db.get(Payment, payment_id)
    if not payment or not payment.tracking_code:
        return HTMLResponse("<h2>خطا: اطلاعات پرداخت معتبر نیست.</h2>", status_code=404)

    token = ""
    notes = (payment.admin_notes or "").strip()
    if notes.startswith("TOKEN:"):
        token = notes.split(":", 1)[1]
    elif payment.reference_id:
        token = payment.reference_id

    if not token:
        token = payment.tracking_code

    html_content = f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
    <meta charset="UTF-8">
    <title>انتقال به درگاه پرداخت اینترنتی سامان (شاپرک)</title>
    <style>
        body {{ font-family: Tahoma, sans-serif; text-align: center; margin-top: 80px; background: #0f172a; color: #f8fafc; }}
        .card {{ background: #1e293b; max-width: 450px; margin: 0 auto; padding: 30px; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
        .spinner {{ width: 48px; height: 48px; border: 4px solid #3b82f6; border-bottom-color: transparent; border-radius: 50%; display: inline-block; animation: rotation 1s linear infinite; margin-bottom: 20px; }}
        @keyframes rotation {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
        h3 {{ margin: 10px 0; font-size: 18px; }}
        p {{ color: #94a3b8; font-size: 13px; }}
    </style>
</head>
<body onload="document.getElementById('sepForm').submit();">
    <div class="card">
        <span class="spinner"></span>
        <h3>در حال اتصال امن به درگاه سامان شاپرک...</h3>
        <p>لطفاً چند لحظه شکیبا باشید.</p>
        <form id="sepForm" method="post" action="https://sep.shaparak.ir/OnlinePG/OnlinePG">
            <input type="hidden" name="Token" value="{token}">
            <input type="hidden" name="GetMethod" value="true">
            <noscript><button type="submit" style="padding:10px 20px; background:#2563eb; color:white; border:none; border-radius:8px; cursor:pointer;">انتقال به درگاه سامان</button></noscript>
        </form>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html_content)


@router.api_route("/sep/callback", methods=["GET", "POST"], response_class=HTMLResponse)
async def sep_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle SEP Shaparak return callback."""
    params = {}
    if request.method == "POST":
        try:
            form = await request.form()
            params = dict(form)
        except Exception:
            params = {}
    if not params:
        params = dict(request.query_params)

    res_num = str(params.get("ResNum") or "").strip()
    ref_num = str(params.get("RefNum") or "").strip()
    state = str(params.get("State") or "").upper()
    status = str(params.get("Status") or "")
    terminal_id = str(params.get("TerminalId") or "").strip()

    if not res_num:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:red;'>خطا: شناسه پرداخت سامان یافت نشد.</h2>"
            "</body></html>",
            status_code=400,
        )

    payment_res = await db.execute(
        select(Payment).where(Payment.tracking_code == res_num)
    )
    payment = payment_res.scalar_one_or_none()

    if not payment:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            f"<h2 style='color:red;'>پرداخت با شناسه {res_num} یافت نشد.</h2>"
            "</body></html>",
            status_code=404,
        )

    if payment.status == PaymentStatus.PAID:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:green;'>این پرداخت قبلاً با موفقیت تایید و سرویس فعال شده است.</h2>"
            "<p>لطفاً به ربات تلگرام بازگردید.</p>"
            "</body></html>"
        )

    is_customer_ok = state == "OK" or status in ("2", "0")
    if not is_customer_ok or not ref_num:
        payment.status = PaymentStatus.REJECTED
        await db.commit()
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:red;'>پرداخت در درگاه سامان لغو شد یا ناموفق بود.</h2>"
            f"<p><a href='{_get_bot_return_link()}'>بازگشت به ربات</a></p>"
            "</body></html>"
        )

    term_id = terminal_id
    merchant_id = ""
    if payment.gateway_id:
        gw = await db.get(PaymentGateway, payment.gateway_id)
        if gw:
            cfg = gw.config_dict
            term_id = cfg.get("terminal_id") or term_id
            merchant_id = cfg.get("merchant_id", "")

    gateway = SEPGateway(terminal_id=term_id, merchant_id=merchant_id)
    verify_res = await gateway.verify_payment(
        ref_num=ref_num,
        amount_irr=payment.amount_irr,
        terminal_id=term_id,
    )

    if not verify_res.success:
        payment.status = PaymentStatus.REJECTED
        await db.commit()
        return HTMLResponse(
            f"<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            f"<h2 style='color:red;'>تایید پرداخت درگاه سامان با خطا مواجه شد:</h2>"
            f"<p>{verify_res.error_message}</p>"
            f"</body></html>"
        )

    order_service = OrderService(db)
    subscription = await order_service.approve_and_provision_order(
        payment_id=payment.id,
        reference_id=verify_res.reference_id or ref_num,
        admin_notes="SEP Saman Online Verification",
    )
    await db.commit()

    await _handle_payment_success_notification(
        db, payment, subscription, verify_res.reference_id or ref_num, "سامان (SEP)"
    )

    return HTMLResponse(
        "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
        "<h2 style='color:green;'>✅ پرداخت با موفقیت از طریق درگاه سامان انجام شد و سرویس شما فعال گردید!</h2>"
        f"<p>شماره مرجع بانکی (RefNum): <b>{verify_res.reference_id or ref_num}</b></p>"
        "<p>اطلاعات اتصال به ربات تلگرام شما ارسال شد. لطفاً به تلگرام بازگردید.</p>"
        "</body></html>"
    )


# =========================================================================
# 3. IDPAY (درگاه آیدی پی) ROUTES
# =========================================================================
@router.api_route("/idpay/callback", methods=["GET", "POST"], response_class=HTMLResponse)
async def idpay_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle IDPay return callback."""
    params = {}
    if request.method == "POST":
        try:
            form = await request.form()
            params = dict(form)
        except Exception:
            params = {}
    if not params:
        params = dict(request.query_params)

    order_id = str(params.get("order_id") or "").strip()
    idpay_id = str(params.get("id") or "").strip()
    track_id = str(params.get("track_id") or "").strip()
    status = str(params.get("status") or "").strip()

    if not order_id and not idpay_id:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:red;'>خطا: اطلاعات بازگشتی آیدی پی نامعتبر است.</h2>"
            "</body></html>",
            status_code=400,
        )

    query = select(Payment).where(
        (Payment.tracking_code == order_id) | (Payment.tracking_code == idpay_id)
    )
    payment_res = await db.execute(query)
    payment = payment_res.scalar_one_or_none()

    if not payment:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            f"<h2 style='color:red;'>پرداخت سفارش {order_id} یافت نشد.</h2>"
            "</body></html>",
            status_code=404,
        )

    if payment.status == PaymentStatus.PAID:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:green;'>این پرداخت قبلاً با موفقیت تایید و سرویس فعال شده است.</h2>"
            "<p>لطفاً به ربات تلگرام بازگردید.</p>"
            "</body></html>"
        )

    if status not in ("10", "100", "101"):
        payment.status = PaymentStatus.REJECTED
        await db.commit()
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            f"<h2 style='color:red;'>پرداخت در درگاه آیدی پی لغو شد یا ناموفق بود (کد: {status}).</h2>"
            f"<p><a href='{_get_bot_return_link()}'>بازگشت به ربات</a></p>"
            "</body></html>"
        )

    api_key = None
    sandbox = None
    if payment.gateway_id:
        gw = await db.get(PaymentGateway, payment.gateway_id)
        if gw:
            cfg = gw.config_dict
            api_key = cfg.get("api_key")
            sandbox = cfg.get("sandbox", False)

    gateway = IDPayGateway(api_key=api_key, sandbox=sandbox)
    verify_res = await gateway.verify_payment(
        authority=idpay_id,
        amount_irr=payment.amount_irr,
        order_id=order_id,
    )

    if not verify_res.success:
        payment.status = PaymentStatus.REJECTED
        await db.commit()
        return HTMLResponse(
            f"<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            f"<h2 style='color:red;'>تایید پرداخت آیدی‌پی با خطا مواجه شد:</h2>"
            f"<p>{verify_res.error_message}</p>"
            f"</body></html>"
        )

    order_service = OrderService(db)
    final_ref = verify_res.reference_id or track_id
    subscription = await order_service.approve_and_provision_order(
        payment_id=payment.id,
        reference_id=final_ref,
        admin_notes="IDPay Online Verification",
    )
    await db.commit()

    await _handle_payment_success_notification(
        db, payment, subscription, final_ref, "آیدی پی (IDPay)"
    )

    return HTMLResponse(
        "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
        "<h2 style='color:green;'>✅ پرداخت با موفقیت از طریق آیدی پی انجام شد و سرویس شما فعال گردید!</h2>"
        f"<p>شماره پیگیری (Track ID): <b>{final_ref}</b></p>"
        "<p>اطلاعات اتصال به ربات تلگرام شما ارسال شد. لطفاً به تلگرام بازگردید.</p>"
        "</body></html>"
    )


# =========================================================================
# 4. PERFECT MONEY ROUTES (CALLBACK & IPN STATUS)
# =========================================================================
@router.api_route("/perfectmoney/callback", methods=["GET", "POST"], response_class=HTMLResponse)
async def perfect_money_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle customer redirection back from Perfect Money SCI."""
    params = {}
    if request.method == "POST":
        try:
            form = await request.form()
            params = dict(form)
        except Exception:
            params = {}
    if not params:
        params = dict(request.query_params)

    payment_id_param = str(params.get("PAYMENT_ID") or params.get("ORDER_NUM") or "").strip()
    batch_num = str(params.get("PAYMENT_BATCH_NUM") or params.get("batch") or "").strip()

    if not payment_id_param:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:green;'>با تشکر از پرداخت شما با پرفکت مانی.</h2>"
            "<p>به محض تایید شبکه، سرویس شما در ربات تلگرام تحویل داده خواهد شد.</p>"
            "</body></html>"
        )

    payment_res = await db.execute(
        select(Payment).where(
            (Payment.tracking_code == payment_id_param)
            | (Payment.tracking_code == f"PM-{payment_id_param}")
            | (Payment.id == uuid.UUID(payment_id_param) if len(payment_id_param) == 36 else False)
        )
    )
    payment = payment_res.scalar_one_or_none()

    if not payment:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:orange;'>تراکنش در حال پردازش است. لطفاً وضعیت را در ربات تلگرام بررسی فرمایید.</h2>"
            "</body></html>"
        )

    if payment.status == PaymentStatus.PAID:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:green;'>✅ پرداخت با موفقیت تایید و سرویس شما فعال گردید!</h2>"
            f"<p>شماره تراکنش (Batch): <b>{payment.reference_id or batch_num}</b></p>"
            "<p>اطلاعات اشتراک به تلگرام شما ارسال شد.</p>"
            "</body></html>"
        )

    # If status webhook hasn't processed yet, attempt verification
    account_id = None
    passphrase = None
    if payment.gateway_id:
        gw = await db.get(PaymentGateway, payment.gateway_id)
        if gw:
            cfg = gw.config_dict
            account_id = cfg.get("account_id")
            passphrase = cfg.get("passphrase")

    gateway = PerfectMoneyGateway(account_id=account_id, passphrase=passphrase)
    verify_res = await gateway.verify_payment(
        authority=payment.tracking_code,
        amount_irr=payment.amount_irr,
        metadata=params,
    )

    if verify_res.success:
        order_service = OrderService(db)
        ref_id = verify_res.reference_id or batch_num or f"PM-BAT-{payment.tracking_code}"
        subscription = await order_service.approve_and_provision_order(
            payment_id=payment.id,
            reference_id=ref_id,
            admin_notes="Perfect Money SCI Callback Verification",
        )
        await db.commit()
        await _handle_payment_success_notification(
            db, payment, subscription, ref_id, "پرفکت مانی (Perfect Money)"
        )
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:green;'>✅ پرداخت با موفقیت تایید و سرویس شما فعال گردید!</h2>"
            f"<p>شماره تراکنش (Batch): <b>{ref_id}</b></p>"
            "<p>اطلاعات اشتراک به ربات تلگرام ارسال شد.</p>"
            "</body></html>"
        )

    return HTMLResponse(
        "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
        "<h2 style='color:orange;'>تراکنش پرفکت مانی دریافت شد و در انتظار تایید نهایی است.</h2>"
        "<p>لطفاً به ربات تلگرام بازگردید.</p>"
        "</body></html>"
    )


@router.post("/perfectmoney/status")
async def perfect_money_status_ipn(request: Request, db: AsyncSession = Depends(get_db)):
    """Server-to-server IPN status webhook from Perfect Money."""
    params = {}
    try:
        form = await request.form()
        params = dict(form)
    except Exception:
        params = {}
    if not params:
        params = dict(request.query_params)

    payment_id_param = str(params.get("PAYMENT_ID") or "").strip()
    batch_num = str(params.get("PAYMENT_BATCH_NUM") or "").strip()

    if not payment_id_param or not batch_num:
        return {"status": "error", "message": "Missing PAYMENT_ID or PAYMENT_BATCH_NUM"}

    payment_res = await db.execute(
        select(Payment).where(
            (Payment.tracking_code == payment_id_param)
            | (Payment.tracking_code == f"PM-{payment_id_param}")
        )
    )
    payment = payment_res.scalar_one_or_none()
    if not payment:
        return {"status": "error", "message": f"Payment {payment_id_param} not found"}

    if payment.status == PaymentStatus.PAID:
        return {"status": "ok", "message": "Already processed"}

    account_id = None
    passphrase = None
    if payment.gateway_id:
        gw = await db.get(PaymentGateway, payment.gateway_id)
        if gw:
            cfg = gw.config_dict
            account_id = cfg.get("account_id")
            passphrase = cfg.get("passphrase")

    gateway = PerfectMoneyGateway(account_id=account_id, passphrase=passphrase)
    if not gateway.validate_callback(params):
        return {"status": "error", "message": "V2_HASH validation failed"}

    order_service = OrderService(db)
    subscription = await order_service.approve_and_provision_order(
        payment_id=payment.id,
        reference_id=batch_num,
        admin_notes="Perfect Money IPN Webhook Verification",
    )
    await db.commit()

    await _handle_payment_success_notification(
        db, payment, subscription, batch_num, "پرفکت مانی (Perfect Money)"
    )
    return {"status": "ok", "batch": batch_num}


# =========================================================================
# 5. STRIPE INTERNATIONAL ROUTES (CALLBACK & WEBHOOK)
# =========================================================================
@router.api_route("/stripe/callback", methods=["GET", "POST"], response_class=HTMLResponse)
async def stripe_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle customer redirection back from Stripe Checkout."""
    from app.integrations.payments.stripe_gw import StripeGateway

    params = dict(request.query_params)
    session_id = str(params.get("session_id") or "").strip()
    tracking_code = str(params.get("tracking_code") or "").strip()
    canceled = str(params.get("canceled") or "").strip()

    if canceled == "1":
        if tracking_code:
            p_res = await db.execute(select(Payment).where(Payment.tracking_code == tracking_code))
            p_obj = p_res.scalar_one_or_none()
            if p_obj and p_obj.status == PaymentStatus.PENDING:
                p_obj.status = PaymentStatus.REJECTED
                await db.commit()
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:red;'>پرداخت در درگاه استرایپ (Stripe) لغو شد.</h2>"
            f"<p><a href='{_get_bot_return_link()}'>بازگشت به ربات تلگرام</a></p>"
            "</body></html>"
        )

    if not session_id and not tracking_code:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:red;'>خطا: شناسه تراکنش استرایپ یافت نشد.</h2>"
            "</body></html>",
            status_code=400,
        )

    payment_res = await db.execute(
        select(Payment).where(
            (Payment.tracking_code == tracking_code)
            | (Payment.tracking_code == session_id)
            | (Payment.reference_id == session_id)
        )
    )
    payment = payment_res.scalar_one_or_none()
    if not payment:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:red;'>پرداخت مورد نظر در سیستم یافت نشد.</h2>"
            "</body></html>",
            status_code=404,
        )

    if payment.status == PaymentStatus.PAID:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:green;'>✅ پرداخت شما قبلاً تایید و سرویس فعال شده است.</h2>"
            "<p>لطفاً به ربات تلگرام بازگردید.</p>"
            "</body></html>"
        )

    secret_key = None
    currency = "USD"
    rate = 70000
    if payment.gateway_id:
        gw = await db.get(PaymentGateway, payment.gateway_id)
        if gw:
            cfg = gw.config_dict
            secret_key = cfg.get("secret_key")
            currency = cfg.get("currency", "USD")
            rate = int(cfg.get("exchange_rate", 70000) or 70000)

    gateway = StripeGateway(secret_key=secret_key, currency=currency, exchange_rate_toman=rate)
    auth_to_verify = session_id or payment.reference_id or payment.tracking_code
    verify_res = await gateway.verify_payment(authority=auth_to_verify, amount_irr=payment.amount_irr)

    if not verify_res.success:
        return HTMLResponse(
            "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
            "<h2 style='color:red;'>تایید پرداخت درگاه استرایپ ناموفق بود:</h2>"
            f"<p>{verify_res.error_message}</p>"
            "</body></html>"
        )

    order_service = OrderService(db)
    final_ref = verify_res.reference_id or auth_to_verify
    subscription = await order_service.approve_and_provision_order(
        payment_id=payment.id,
        reference_id=final_ref,
        admin_notes="Stripe Checkout Online Verification",
    )
    await db.commit()

    await _handle_payment_success_notification(
        db, payment, subscription, final_ref, "درگاه بین‌المللی استرایپ (Stripe)"
    )

    return HTMLResponse(
        "<html><body style='text-align:center;font-family:tahoma;direction:rtl;margin-top:50px;'>"
        "<h2 style='color:green;'>✅ پرداخت ارزی از طریق استرایپ (Stripe) با موفقیت انجام شد و سرویس شما فعال گردید!</h2>"
        f"<p>شناسه تراکنش: <b>{final_ref}</b></p>"
        "<p>اطلاعات اتصال به ربات تلگرام شما ارسال شد. لطفاً به تلگرام بازگردید.</p>"
        "</body></html>"
    )

