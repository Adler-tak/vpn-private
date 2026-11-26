"""Support ticket handlers for customer inquiries."""
import html
import uuid
import re
import logging
from typing import Optional, Union, Dict, Any, List
from aiogram import Router, F, Bot
from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, cast, String
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.user import User
from app.database.models.support import SupportTicket
from app.modules.support.service import SupportService
from app.bot.locales.fa import MAIN_MENU_BTNS
from app.modules.bot_texts.service import get_bot_text
from app.bot.states.customer import SupportTicketState, AdminState
from app.core.constants import TicketStatus
from app.core.config import settings
from app.bot.handlers.admin import is_admin

logger = logging.getLogger(__name__)
router = Router()


class AdminReplyFilter(BaseFilter):
    """Filter that passes only if the replying user is a registered Admin/Superadmin."""
    async def __call__(self, message: Message, db_user: Optional[User] = None) -> bool:
        if db_user:
            return is_admin(db_user)
        return bool(message.from_user and message.from_user.id in settings.ADMIN_TELEGRAM_IDS)


@router.message(F.text == MAIN_MENU_BTNS["support"])
async def open_support_center(message: Message, db_user: User, db_session: AsyncSession):
    support_service = SupportService(db_session)
    tickets = await support_service.get_user_tickets(db_user.id)

    support_username = getattr(settings, "SUPPORT_USERNAME", "VPNPrivateSupport").lstrip("@")
    kb_buttons = [
        [InlineKeyboardButton(text="✍️ ارسال تیکت جدید به پشتیبانی", callback_data="new_support_ticket")]
    ]
    if support_username:
        kb_buttons.append([
            InlineKeyboardButton(
                text=f"💬 ارتباط مستقیم با پشتیبان تلگرام (@{support_username})",
                url=f"https://t.me/{support_username}"
            )
        ])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    support_intro = get_bot_text(
        "support_guide",
        f"🎧 <b>مرکز پشتیبانی VPN Private:</b>\n\nتیم پشتیبانی ما به صورت ۲۴ ساعته آماده پاسخگویی به سوالات و رفع مشکلات شما می‌باشد.\n\n👤 <b>آیدی پشتیبانی تلگرام:</b> @{support_username}",
    )
    text = f"{support_intro}\n\n"
    if tickets:
        text += "📋 <b>تیکت‌های اخیر شما:</b>\n"
        for t in tickets[:5]:
            status_fa = "🟢 باز" if t.status == "OPEN" else "🟡 در حال بررسی" if t.status == "IN_PROGRESS" else "⚪️ بسته شده"
            safe_subj = html.escape(t.subject or "")
            text += f"• تیکت #{t.id.hex[:6]}: <b>{safe_subj}</b> ({status_fa})\n"
    else:
        text += "شما هنوز تیکتی ارسال نکرده‌اید."

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "new_support_ticket")
async def start_ticket_creation(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SupportTicketState.entering_subject)
    await callback.message.edit_text(
        "📝 <b>لطفاً عنوان تیکت خود را وارد فرمایید:</b>\n(مثال: مشکل در اتصال، سوال در مورد فیش بانکی و...)",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(SupportTicketState.entering_subject)
async def handle_ticket_subject(message: Message, state: FSMContext):
    subject = message.text.strip()
    await state.update_data(subject=subject)
    await state.set_state(SupportTicketState.entering_message)
    await message.answer(
        "✍️ <b>لطفاً شرح کامل پیام خود را بنویسید:</b>\n(در صورت نیاز می‌توانید تصویر نیز ضمیمه فرمایید)",
        parse_mode="HTML",
    )


@router.message(SupportTicketState.entering_message)
async def handle_ticket_body(
    message: Message,
    state: FSMContext,
    bot: Bot,
    db_user: User,
    db_session: AsyncSession,
):
    data = await state.get_data()
    subject = data.get("subject", "بدون عنوان")
    text = message.text or message.caption or "بدون متن"
    photo_id = message.photo[-1].file_id if message.photo else None

    support_service = SupportService(db_session)
    ticket = await support_service.create_ticket(
        user_id=db_user.id,
        subject=subject,
        category="general",
        initial_message=text,
        attachment_file_id=photo_id,
    )
    await state.clear()
    await message.answer(
        f"✅ <b>تیکت شماره #{ticket.id.hex[:6]} با موفقیت ثبت شد.</b>\nپشتیبانان ما به زودی پاسخ شما را ارسال خواهند کرد.",
        parse_mode="HTML",
    )

    # Notify administrators about the new ticket with action buttons
    import html
    from app.core.config import settings
    from app.core.constants import UserRole

    u_name = html.escape(db_user.first_name or "کاربر")
    u_username = f"@{html.escape(db_user.username)}" if db_user.username else "ندارد"
    safe_subject = html.escape(subject)
    safe_text = html.escape(text[:600])

    admin_alert = (
        f"🚨 <b>#تیکت_پشتیبانی_جدید</b>\n\n"
        f"🎫 <b>شناسه تیکت:</b> <code>#{ticket.id.hex[:6]}</code>\n"
        f"👤 <b>کاربر:</b> {u_name} ({u_username})\n"
        f"🆔 <b>شناسه کاربر:</b> <code>{db_user.telegram_id}</code>\n"
        f"📌 <b>موضوع:</b> <b>{safe_subject}</b>\n\n"
        f"💬 <b>متن تیکت:</b>\n{safe_text}\n\n"
        f"💡 <i>جهت پاسخ، می‌توانید مستقیماً روی همین پیام Reply کنید یا از دکمه زیر استفاده نمایید.</i>"
    )
    ticket_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✉️ پاسخ به این تیکت", callback_data=f"adm_rpl_tk:{ticket.id.hex}:{db_user.telegram_id}"),
            InlineKeyboardButton(text="🔒 بستن تیکت", callback_data=f"adm_cls_tk:{ticket.id.hex}"),
        ],
        [
            InlineKeyboardButton(text="📂 مشاهده در پنل مدیریت", callback_data=f"adm_tk_dtl:{ticket.id.hex}"),
        ]
    ])

    from app.bot.notifications import notify_all_admins
    await notify_all_admins(
        bot=bot,
        db_session=db_session,
        text=admin_alert,
        photo_file_id=photo_id,
        reply_markup=ticket_kb,
    )
    logger.info(f"Dispatched ticket alert #{ticket.id.hex[:6]} to all admins.")


@router.callback_query(F.data.startswith("adm_rpl_tk:") | F.data.startswith("admin_reply_ticket:"))
async def prompt_ticket_reply(callback: CallbackQuery, state: FSMContext, db_user: User):
    """Admin clicks button to reply to a support ticket."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    parts = callback.data.split(":")
    ticket_id = parts[1]
    target_tg_id = int(parts[2])

    await state.set_state(AdminState.entering_ticket_reply)
    await state.update_data(ticket_id=ticket_id, target_tg_id=target_tg_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_cancel_ticket_reply")]
    ])
    await callback.message.reply(
        f"✍️ <b>پاسخ به تیکت کاربر <code>{target_tg_id}</code>:</b>\n\n"
        "لطفاً متن پاسخ یا تصویر/ویس خود را ارسال نمایید تا فوراً به دست کاربر برسد:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_cancel_ticket_reply")
async def cancel_ticket_reply(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("ارسال پاسخ لغو شد.")
    try:
        await callback.message.delete()
    except Exception:
        pass


@router.message(AdminState.entering_ticket_reply)
async def process_admin_ticket_reply_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
    db_user: User,
    db_session: AsyncSession,
):
    """Handle admin sending the reply message via FSM prompt."""
    if not is_admin(db_user):
        await state.clear()
        return

    # Check for slash command cancellation
    if (message.text or "").startswith("/"):
        await state.clear()
        return

    data = await state.get_data()
    target_tg_id = data.get("target_tg_id")
    ticket_id_str = data.get("ticket_id")

    if not target_tg_id:
        await state.clear()
        return await message.answer("⚠️ نشست پاسخ به تیکت منقضی شده است.")

    reply_content = message.text or message.caption or "پاسخ جدید"
    photo_id = message.photo[-1].file_id if message.photo else (message.document.file_id if message.document else None)

    # Save to database
    if ticket_id_str:
        try:
            support_svc = SupportService(db_session)
            await support_svc.add_reply(
                ticket_id=uuid.UUID(ticket_id_str),
                sender_type="ADMIN",
                sender_id=db_user.telegram_id,
                text=reply_content,
                attachment_file_id=photo_id,
            )
            await db_session.commit()
        except Exception as e_db:
            logger.warning(f"Failed to record admin reply in DB: {e_db}")

    safe_reply_content = html.escape(reply_content)
    ticket_label = f" به تیکت #{tid_match.group(1).lower()[:6]}" if tid_match else ""
    customer_msg = (
        f"🎧 <b>پاسخ پشتیبانی VPN Private{ticket_label}:</b>\n\n"
        f"{safe_reply_content}\n\n"
        f"💬 <i>در صورت نیاز می‌توانید مستقیماً به همین پیام پاسخ دهید.</i>"
    )

    try:
        if message.photo:
            await bot.send_photo(chat_id=target_tg_id, photo=message.photo[-1].file_id, caption=customer_msg, parse_mode="HTML")
        elif message.voice:
            await bot.send_voice(chat_id=target_tg_id, voice=message.voice.file_id, caption=customer_msg, parse_mode="HTML")
        elif message.document:
            await bot.send_document(chat_id=target_tg_id, document=message.document.file_id, caption=customer_msg, parse_mode="HTML")
        elif message.video:
            await bot.send_video(chat_id=target_tg_id, video=message.video.file_id, caption=customer_msg, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=target_tg_id, text=customer_msg, parse_mode="HTML")

        await state.clear()
        await message.reply(
            f"✅ <b>پاسخ شما با موفقیت برای کاربر <code>{target_tg_id}</code> ارسال شد!</b>",
            parse_mode="HTML",
        )
    except Exception as exc:
        await message.reply(f"❌ خطا در ارسال پاسخ به کاربر: {exc}")


@router.callback_query(F.data.startswith("adm_cls_tk:") | F.data.startswith("admin_close_ticket:"))
async def handle_admin_close_ticket(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    ticket_id_str = callback.data.split(":")[1]
    tid_uuid = uuid.UUID(hex=ticket_id_str) if len(ticket_id_str) == 32 else uuid.UUID(ticket_id_str)
    ticket = await db_session.get(SupportTicket, tid_uuid)
    if ticket:
        ticket.status = TicketStatus.CLOSED
        await db_session.commit()
        await callback.answer("🔒 تیکت با موفقیت بسته شد.", show_alert=True)
        try:
            if callback.message.text and "جزئیات تیکت" in callback.message.text:
                from app.bot.handlers.admin import admin_ticket_detail
                callback.data = f"adm_tk_dtl:{ticket.id.hex}"
                await admin_ticket_detail(callback, db_user, db_session)
            else:
                await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    else:
        await callback.answer("تیکت یافت نشد.", show_alert=True)


@router.message(F.reply_to_message, AdminReplyFilter())
async def handle_admin_ticket_reply(
    message: Message,
    bot: Bot,
    db_user: User,
    db_session: AsyncSession,
):
    """Handle admin replying to any ticket or customer notification message via native Telegram Reply."""
    from app.bot.handlers.admin import is_admin
    import re
    if not is_admin(db_user):
        return

    reply = message.reply_to_message
    orig_text = reply.text or reply.caption or ""

    # Check if the replied message is a system notification containing a customer ID
    match = re.search(r"شناسه\s*(?:کاربر|عددی|تلگرام)?\s*[:\s*`<code>]*(\d{5,15})", orig_text)
    if not match:
        match = re.search(r"(?:user[_\s]*id|tg[_\s]*id)\s*[:\s*`<code>]*(\d{5,15})", orig_text, re.IGNORECASE)
    if not match:
        match = re.search(r"\b(\d{7,15})\b", orig_text)

    if not match:
        # If admin replied to a ticket or alert message but ID couldn't be parsed
        if "#تیکت" in orig_text or "تیکت" in orig_text or "سفارش" in orig_text or "فیش" in orig_text:
            return await message.reply("⚠️ شناسه کاربر در این پیام شناسایی نشد. لطفاً از دکمه «✉️ پاسخ به این تیکت» استفاده فرمایید.")
        return

    target_tg_id = int(match.group(1))
    reply_content = message.text or message.caption or "پاسخ پشتیبانی"
    photo_id = message.photo[-1].file_id if message.photo else (message.document.file_id if message.document else None)

    # If this reply belongs to a ticket, record it in database
    tid_match = re.search(r"شناسه تیکت:\s*#?([0-9a-fA-F]{6,36})", orig_text)
    if tid_match:
        try:
            tid_hex = tid_match.group(1).lower()
            tickets_res = await db_session.execute(
                select(SupportTicket).order_by(SupportTicket.created_at.desc())
            )
            for t in tickets_res.scalars().all():
                if t.id.hex.startswith(tid_hex):
                    support_svc = SupportService(db_session)
                    await support_svc.add_reply(
                        ticket_id=t.id,
                        sender_type="ADMIN",
                        sender_id=db_user.telegram_id,
                        text=reply_content,
                        attachment_file_id=photo_id,
                    )
                    await db_session.commit()
                    break
        except Exception as e_log:
            logger.warning(f"Could not link reply to ticket: {e_log}")

    safe_reply_content = html.escape(reply_content)
    ticket_label = f" به تیکت #{tid_match.group(1).lower()[:6]}" if tid_match else ""
    customer_msg = (
        f"🎧 <b>پاسخ پشتیبانی VPN Private{ticket_label}:</b>\n\n"
        f"{safe_reply_content}\n\n"
        f"💬 <i>در صورت نیاز می‌توانید مستقیماً به همین پیام پاسخ دهید.</i>"
    )
    try:
        if message.photo:
            await bot.send_photo(
                chat_id=target_tg_id,
                photo=message.photo[-1].file_id,
                caption=customer_msg,
                parse_mode="HTML",
            )
        elif message.voice:
            await bot.send_voice(
                chat_id=target_tg_id,
                voice=message.voice.file_id,
                caption=customer_msg,
                parse_mode="HTML",
            )
        elif message.document:
            await bot.send_document(
                chat_id=target_tg_id,
                document=message.document.file_id,
                caption=customer_msg,
                parse_mode="HTML",
            )
        elif message.video:
            await bot.send_video(
                chat_id=target_tg_id,
                video=message.video.file_id,
                caption=customer_msg,
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id=target_tg_id,
                text=customer_msg,
                parse_mode="HTML",
            )

        await message.reply(
            f"✅ <b>پاسخ شما با موفقیت برای کاربر <code>{target_tg_id}</code> ارسال شد!</b>",
            parse_mode="HTML",
        )
    except Exception as exc:
        await message.reply(f"❌ خطا در ارسال پاسخ به کاربر: {exc}")


@router.message(F.reply_to_message)
async def handle_customer_ticket_reply(
    message: Message,
    bot: Bot,
    db_user: User,
    db_session: AsyncSession,
):
    """Handle customer replying back to a support message."""
    from app.bot.handlers.admin import is_admin
    import html

    # If sender is admin, let handle_admin_ticket_reply handle it
    if is_admin(db_user):
        return

    # Find ticket: check if user is replying to a message with a specific ticket number
    support_svc = SupportService(db_session)
    tickets = await support_svc.get_user_tickets(db_user.id)

    reply_orig_text = ""
    if message.reply_to_message:
        reply_orig_text = message.reply_to_message.text or message.reply_to_message.caption or ""

    target_ticket = None
    tid_match = re.search(r"(?:تیکت|ticket)\s*#?([0-9a-fA-F]{6,36})", reply_orig_text, re.IGNORECASE)
    if tid_match:
        matched_hex = tid_match.group(1).lower()
        for t in tickets:
            if t.id.hex.startswith(matched_hex):
                target_ticket = t
                break

    if not target_ticket:
        active_tickets = [t for t in tickets if t.status in [TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.WAITING_FOR_CUSTOMER]]
        target_ticket = active_tickets[0] if active_tickets else (tickets[0] if tickets else None)

    reply_content = message.text or message.caption or "پاسخ متنی/تصویری کاربر"
    photo_id = message.photo[-1].file_id if message.photo else (message.document.file_id if message.document else None)

    if not target_ticket:
        # Create a new ticket if none open
        target_ticket = await support_svc.create_ticket(
            user_id=db_user.id,
            subject="پاسخ به پیام پشتیبانی",
            category="general",
            initial_message=reply_content,
            attachment_file_id=photo_id,
        )
    else:
        await support_svc.add_reply(
            ticket_id=target_ticket.id,
            sender_type="CUSTOMER",
            sender_id=db_user.telegram_id,
            text=reply_content,
            attachment_file_id=photo_id,
        )
        target_ticket.status = TicketStatus.OPEN
        await db_session.commit()

    await message.reply(
        f"✅ <b>پیام شما دریافت شد و به تیکت شماره #{target_ticket.id.hex[:6]} افزوده گردید.</b>\nکارشناسان پشتیبانی به‌زودی پاسخ شما را ارسال خواهند کرد.",
        parse_mode="HTML",
    )

    # Immediately alert all admins about customer's new response
    u_name = html.escape(db_user.first_name or "کاربر")
    u_username = f"@{html.escape(db_user.username)}" if db_user.username else "ندارد"
    safe_text = html.escape(reply_content[:600])

    admin_alert = (
        f"📩 <b>#پیام_جدید_کاربر_در_تیکت</b>\n\n"
        f"🎫 <b>شناسه تیکت:</b> <code>#{target_ticket.id.hex[:6]}</code>\n"
        f"👤 <b>کاربر:</b> {u_name} ({u_username})\n"
        f"🆔 <b>شناسه عددی کاربر:</b> <code>{db_user.telegram_id}</code>\n"
        f"📌 <b>موضوع تیکت:</b> <b>{html.escape(target_ticket.subject)}</b>\n\n"
        f"💬 <b>متن پیام جدید:</b>\n{safe_text}\n\n"
        f"💡 <i>جهت پاسخ می‌توانید مستقیماً روی این پیام Reply کنید یا از دکمه زیر استفاده نمایید.</i>"
    )
    ticket_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✉️ پاسخ به این تیکت", callback_data=f"adm_rpl_tk:{target_ticket.id.hex}:{db_user.telegram_id}"),
            InlineKeyboardButton(text="🔒 بستن تیکت", callback_data=f"adm_cls_tk:{target_ticket.id.hex}"),
        ],
        [
            InlineKeyboardButton(text="🔎 مشاهده در پنل مدیریت", callback_data=f"adm_tk_dtl:{target_ticket.id.hex}"),
        ]
    ])

    from app.bot.notifications import notify_all_admins
    await notify_all_admins(
        bot=bot,
        db_session=db_session,
        text=admin_alert,
        photo_file_id=photo_id,
        reply_markup=ticket_kb,
    )
    logger.info(f"Broadcasted customer follow-up message on ticket #{target_ticket.id.hex[:6]} to all admins.")

