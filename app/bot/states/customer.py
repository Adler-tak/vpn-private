"""Aiogram FSM States for customer and administrative flows."""
from aiogram.fsm.state import State, StatesGroup


class PurchaseOrderState(StatesGroup):
    selecting_product = State()
    selecting_location = State()
    entering_coupon = State()
    selecting_payment_method = State()
    awaiting_card_receipt = State()


class WalletTopupState(StatesGroup):
    entering_amount = State()
    selecting_method = State()
    awaiting_c2c_receipt = State()


class SubRenewState(StatesGroup):
    awaiting_receipt = State()


class SupportTicketState(StatesGroup):
    entering_subject = State()
    entering_message = State()


class VoucherRedeemState(StatesGroup):
    entering_code = State()


class AdminState(StatesGroup):
    entering_broadcast_message = State()
    entering_user_id_search = State()
    entering_balance_adjustment = State()
    entering_ticket_reply = State()
    
    # Plan Editing & Creation States
    editing_product_price = State()
    editing_product_name = State()
    editing_product_traffic = State()
    editing_product_duration = State()
    plan_entering_name = State()
    plan_entering_traffic = State()
    plan_entering_duration = State()
    plan_entering_price = State()

    # Location / Server States
    loc_entering_name = State()
    loc_entering_code = State()
    loc_entering_flag = State()
    loc_entering_host = State()
    editing_loc_name = State()
    editing_loc_host = State()
    waiting_for_ws_config = State()

    # Admin Management States
    entering_new_admin_id = State()
    entering_demote_admin_id = State()

    # Coupon & Direct PM & Subscription States
    coupon_entering_code = State()
    coupon_entering_percent = State()
    coupon_entering_max_uses = State()
    entering_direct_pm_message = State()
    searching_subscription = State()
    entering_panel_details = State()
    
    # Step-by-step panel addition wizard
    panel_entering_url = State()
    panel_entering_username = State()
    panel_entering_password = State()
    panel_entering_name = State()

    # Settings States
    editing_channel_username = State()
    editing_card_number = State()
    editing_card_holder = State()
    editing_card_bank = State()
    editing_crypto_wallet = State()
    editing_usdt_rate = State()
    editing_zarinpal_merchant = State()
    editing_min_wallet_amount = State()
    
    # Trial Settings States
    setting_trial_max_count = State()
    setting_trial_traffic_gb = State()
    setting_trial_duration_days = State()
