"""System analytics, business telemetry, and administrative dashboards."""
from datetime import datetime, timedelta, timezone
from typing import Dict, Any
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.user import User
from app.database.models.subscription import Subscription
from app.database.models.order import Order
from app.database.models.payment import Payment
from app.database.models.panel import Panel, Server
from app.core.constants import OrderStatus, SubscriptionStatus, PaymentStatus


class AnalyticsService:
    """Computes real-time operational and financial metrics."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_admin_dashboard_stats(self) -> Dict[str, Any]:
        """Aggregate high-level overview for Admin Telegram panel."""
        now = datetime.now(timezone.utc)
        start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        start_of_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

        # Users
        total_users = (await self.session.execute(select(func.count(User.id)))).scalar() or 0

        # Subscriptions
        active_subs = (
            await self.session.execute(
                select(func.count(Subscription.id)).where(
                    Subscription.status == SubscriptionStatus.ACTIVE
                )
            )
        ).scalar() or 0

        expired_subs = (
            await self.session.execute(
                select(func.count(Subscription.id)).where(
                    Subscription.status == SubscriptionStatus.EXPIRED
                )
            )
        ).scalar() or 0

        # Revenue
        daily_revenue_res = await self.session.execute(
            select(func.coalesce(func.sum(Payment.amount_irr), 0)).where(
                and_(
                    Payment.status == PaymentStatus.PAID,
                    Payment.paid_at >= start_of_day,
                )
            )
        )
        daily_revenue_irr = daily_revenue_res.scalar() or 0

        monthly_revenue_res = await self.session.execute(
            select(func.coalesce(func.sum(Payment.amount_irr), 0)).where(
                and_(
                    Payment.status == PaymentStatus.PAID,
                    Payment.paid_at >= start_of_month,
                )
            )
        )
        monthly_revenue_irr = monthly_revenue_res.scalar() or 0

        # Pending approvals
        pending_payments = (
            await self.session.execute(
                select(func.count(Payment.id)).where(
                    Payment.status == PaymentStatus.PENDING
                )
            )
        ).scalar() or 0

        # Open support tickets
        from app.database.models.support import SupportTicket
        from app.core.constants import TicketStatus
        open_tickets = (
            await self.session.execute(
                select(func.count(SupportTicket.id)).where(
                    SupportTicket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS])
                )
            )
        ).scalar() or 0

        # Failed provisioning
        failed_orders = (
            await self.session.execute(
                select(func.count(Order.id)).where(
                    Order.status == OrderStatus.PROVISIONING_FAILED
                )
            )
        ).scalar() or 0

        # Panel status
        total_panels = (await self.session.execute(select(func.count(Panel.id)))).scalar() or 0
        healthy_panels = (
            await self.session.execute(
                select(func.count(Panel.id)).where(Panel.is_healthy.is_(True))
            )
        ).scalar() or 0

        return {
            "total_users": total_users,
            "active_subscriptions": active_subs,
            "expired_subscriptions": expired_subs,
            "daily_revenue_toman": daily_revenue_irr // 10,
            "monthly_revenue_toman": monthly_revenue_irr // 10,
            "pending_payments": pending_payments,
            "open_tickets": open_tickets,
            "failed_orders": failed_orders,
            "total_panels": total_panels,
            "healthy_panels": healthy_panels,
        }
