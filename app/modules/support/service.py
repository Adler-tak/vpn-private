"""Customer support ticketing service."""
import logging
import uuid
from typing import List, Optional
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.support import SupportTicket, SupportMessage
from app.database.models.user import User
from app.core.constants import TicketPriority, TicketStatus
from app.core.exceptions import VPNPrivateError

logger = logging.getLogger(__name__)


class SupportService:
    """Manages ticketing lifecycle between customers and administrators."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_ticket(
        self,
        user_id: uuid.UUID,
        subject: str,
        category: str,
        initial_message: str,
        priority: str = TicketPriority.MEDIUM,
        attachment_file_id: Optional[str] = None,
    ) -> SupportTicket:
        """Open a new support ticket."""
        ticket = SupportTicket(
            id=uuid.uuid4(),
            user_id=user_id,
            subject=subject,
            category=category,
            priority=priority,
            status=TicketStatus.OPEN,
        )
        self.session.add(ticket)
        await self.session.flush()

        user = await self.session.get(User, user_id)
        msg = SupportMessage(
            id=uuid.uuid4(),
            ticket_id=ticket.id,
            sender_type="CUSTOMER",
            sender_id=user.telegram_id if user else 0,
            text=initial_message,
            attachment_file_id=attachment_file_id,
        )
        self.session.add(msg)
        await self.session.flush()
        return ticket

    async def add_reply(
        self,
        ticket_id: uuid.UUID,
        sender_type: str,
        sender_id: int,
        text: str,
        attachment_file_id: Optional[str] = None,
    ) -> SupportMessage:
        """Add reply to existing ticket and update ticket status."""
        ticket = await self.session.get(SupportTicket, ticket_id)
        if not ticket:
            raise VPNPrivateError("تیکت پشتیبانی یافت نشد.")

        if sender_type == "ADMIN":
            ticket.status = TicketStatus.WAITING_FOR_CUSTOMER
            ticket.assigned_admin_id = sender_id
        else:
            ticket.status = TicketStatus.IN_PROGRESS

        msg = SupportMessage(
            id=uuid.uuid4(),
            ticket_id=ticket.id,
            sender_type=sender_type,
            sender_id=sender_id,
            text=text,
            attachment_file_id=attachment_file_id,
        )
        self.session.add(msg)
        await self.session.flush()
        return msg

    async def get_user_tickets(self, user_id: uuid.UUID) -> List[SupportTicket]:
        """Fetch all tickets submitted by a specific user."""
        res = await self.session.execute(
            select(SupportTicket)
            .where(SupportTicket.user_id == user_id)
            .order_by(desc(SupportTicket.created_at))
        )
        return list(res.scalars().all())

    async def get_open_tickets(self) -> List[SupportTicket]:
        """Fetch unresolved tickets for administrators."""
        res = await self.session.execute(
            select(SupportTicket)
            .where(SupportTicket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS]))
            .order_by(desc(SupportTicket.created_at))
        )
        return list(res.scalars().all())
