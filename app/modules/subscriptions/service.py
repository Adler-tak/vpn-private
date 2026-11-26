"""Subscription Provisioning, Renewal, and Lifecycle Service."""
from datetime import datetime, timedelta, timezone
import json
import logging
import secrets
import uuid
from typing import Any, Dict, Optional, Tuple
from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.order import Order
from app.database.models.subscription import Subscription
from app.database.models.product import Product
from app.database.models.panel import Panel, Inbound, Server
from app.database.models.user import User
from app.modules.servers.allocation import ServerAllocationEngine
from app.integrations.xui.client import ThreeXUIClient
from app.integrations.xui.adapter import ThreeXUIPanelAdapter
from app.integrations.xui.schemas import ClientSpec
from app.core.constants import OrderStatus, SubscriptionStatus
from app.core.security import generate_secure_uuid, generate_sub_id
from app.core.exceptions import ProvisioningError, VPNPrivateError, OrderStateError
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)


class SubscriptionService:
    """Manages VPN subscription provisioning, panel synchronization, and renewals."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.allocator = ServerAllocationEngine(session)

    def _get_panel_adapter(self, panel: Panel):
        from app.integrations.factory import get_panel_adapter
        return get_panel_adapter(panel, timeout=15.0, verify_ssl=False)

    async def provision_subscription_for_order(
        self,
        order_id: uuid.UUID,
        location_code: Optional[str] = None,
        duration_days_override: Optional[int] = None,
    ) -> Subscription:
        """Provision a new VPN client on 3x-ui panel for an approved order."""
        order = await self.session.get(Order, order_id)
        if not order:
            raise ProvisioningError(f"سفارش با شناسه {order_id} یافت نشد.")

        # Check order status integrity
        if order.status == OrderStatus.ACTIVE:
            raise OrderStateError(f"سفارش در وضعیت {order.status} است و امکان فعال‌سازی مجدد ندارد.")
        if order.status in (OrderStatus.CANCELLED, OrderStatus.REFUNDED):
            raise OrderStateError(f"سفارش در وضعیت {order.status} لغو یا مرجوع شده است.")

        # Check if subscription already exists for this order
        existing_sub = await self.session.execute(
            select(Subscription).where(Subscription.order_id == order.id)
        )
        if existing_sub.scalars().first():
            raise OrderStateError("برای این سفارش قبلاً اشتراک فعال صادر شده است.")

        product = await self.session.get(Product, order.product_id)
        user = await self.session.get(User, order.user_id)
        if not product or not user:
            raise ProvisioningError("اطلاعات محصول یا کاربر سفارش نامعتبر است.")

        # Update order status to PROVISIONING
        order.status = OrderStatus.PROVISIONING
        await self.session.flush()

        # Honor specific location: from argument, or the server assigned to order, or product allowed_locations
        loc = location_code
        if not loc and order.server_id:
            ordered_server = await self.session.get(Server, order.server_id)
            if ordered_server and ordered_server.location_code:
                loc = ordered_server.location_code
        if not loc:
            loc = product.allowed_locations

        protocol = product.protocol

        try:
            now = datetime.now(timezone.utc)
            sub_days = duration_days_override if duration_days_override and duration_days_override > 0 else product.duration_days
            expiry_dt = now + timedelta(days=sub_days)
            expiry_ms = int(expiry_dt.timestamp() * 1000)

            client_uuid = generate_secure_uuid()
            sub_id = generate_sub_id(16)
            try:
                tg_id = int(user.telegram_id) if user.telegram_id else 0
            except (ValueError, TypeError):
                tg_id = 0
            clean_tg = "".join(c for c in str(user.telegram_id or "0") if c.isalnum())
            email_tag = f"u{clean_tg}_{secrets.token_hex(3)}"
            email_suffix = await SettingsService.get_effective_email_suffix(self.session)
            client_email = f"{email_tag}{email_suffix}"

            spec = ClientSpec(
                id=client_uuid,
                email=client_email,
                flow="xtls-rprx-vision" if protocol.lower() == "vless" else "",
                limitIp=2,
                totalGB=product.traffic_bytes,
                expiryTime=expiry_ms,
                enable=True,
                tgId=tg_id,
                subId=sub_id,
            )

            # --- Case 1: Multi-Location Plan (all or custom) -> Provision on ALL allowed inbounds ---
            loc_mode = getattr(product, "location_mode", None)
            if loc_mode == "user_choice":
                is_multi = False
            elif loc_mode in ("all", "custom"):
                is_multi = True
            else:
                is_multi = (product.traffic_bytes == 0) or (loc and (loc.upper() == "ALL" or "," in loc))

            if is_multi:
                from app.modules.servers.allocation import inbound_matches_location

                inb_query = (
                    select(Inbound, Panel)
                    .join(Panel, Inbound.panel_id == Panel.id)
                    .where(
                        and_(
                            Panel.is_active.is_(True),
                            Panel.is_healthy.is_(True),
                            Inbound.is_active.is_(True),
                            Inbound.protocol == protocol.lower(),
                            Inbound.client_count < Inbound.max_clients,
                        )
                    )
                    .order_by(Inbound.client_count.asc())
                )
                candidate_inbounds = (await self.session.execute(inb_query)).all()

                target_allowed = (product.allowed_locations or "ALL").strip()
                matched_pairs = []
                if target_allowed.upper() != "ALL":
                    raw_codes = [c.strip().upper() for c in target_allowed.split(",") if c.strip()]
                    srv_res = await self.session.execute(
                        select(Server).where(
                            Server.is_active.is_(True),
                            Server.inbound_id.isnot(None),
                            func.upper(Server.location_code).in_(raw_codes),
                        )
                    )
                    allowed_inb_ids = {s.inbound_id for s in srv_res.scalars().all() if s.inbound_id}
                    if allowed_inb_ids:
                        matched_pairs = [
                            (ib, p) for ib, p in candidate_inbounds
                            if ib.id in allowed_inb_ids
                        ]

                if not matched_pairs:
                    matched_pairs = [
                        (ib, p) for ib, p in candidate_inbounds
                        if inbound_matches_location(ib.remark, ib.tag, target_allowed, ib.remote_inbound_id)
                    ]

                # Fallback to all candidate inbounds if specific location filter didn't match any
                if not matched_pairs:
                    matched_pairs = candidate_inbounds

                if not matched_pairs:
                    # If still no inbounds found, allocate via standard engine as last resort
                    server, inbound, panel = await self.allocator.allocate_server_and_inbound(
                        location_code="ALL",
                        protocol=protocol,
                        product=product,
                    )
                    matched_pairs = [(inbound, panel)]

                # Group by panel and add client across inbounds
                panels_map = {}
                for ib, p in matched_pairs:
                    panels_map.setdefault(p, []).append(ib)

                from app.integrations.vpn_ui.adapter import VpnUIPanelAdapter
                from app.integrations.vpn_ui.schemas import WireGuardClientSpec, OpenVPNClientSpec

                for panel_obj, inbs in panels_map.items():
                    adapter = self._get_panel_adapter(panel_obj)
                    remote_ids = [ib.remote_inbound_id for ib in inbs]
                    try:
                        if isinstance(adapter, VpnUIPanelAdapter) and protocol.lower() == "wireguard":
                            wg_spec = WireGuardClientSpec(
                                id=client_uuid,
                                email=client_email,
                                totalGB=product.traffic_bytes,
                                expiryTime=expiry_ms,
                                enable=True,
                                tgId=tg_id,
                                subId=sub_id,
                            )
                            for ib in inbs:
                                await adapter.add_wireguard_client(ib.remote_inbound_id, wg_spec)
                        elif isinstance(adapter, VpnUIPanelAdapter) and protocol.lower() in ("amneziawg", "awg"):
                            wg_spec = WireGuardClientSpec(
                                id=client_uuid,
                                email=client_email,
                                totalGB=product.traffic_bytes,
                                expiryTime=expiry_ms,
                                enable=True,
                                tgId=tg_id,
                                subId=sub_id,
                            )
                            for ib in inbs:
                                await adapter.add_amneziawg_client(ib.remote_inbound_id, wg_spec)
                        elif isinstance(adapter, VpnUIPanelAdapter) and protocol.lower() == "openvpn":
                            ovpn_spec = OpenVPNClientSpec(
                                id=client_uuid,
                                username=client_email.split("@")[0],
                                email=client_email,
                                totalGB=product.traffic_bytes,
                                expiryTime=expiry_ms,
                                enable=True,
                                subId=sub_id,
                            )
                            for ib in inbs:
                                await adapter.add_openvpn_client(ib.remote_inbound_id, ovpn_spec)
                        else:
                            await adapter.add_clients_to_inbounds(remote_ids, spec)
                        logger.info(
                            f"Added unlimited client {client_email} to inbounds {remote_ids} on panel {panel_obj.name}"
                        )
                    except Exception as e_batch:
                        logger.warning(f"Batch add client failed: {e_batch}, trying individually...")
                        for ib in inbs:
                            try:
                                await adapter.add_client(ib.remote_inbound_id, spec)
                            except Exception as e_indiv:
                                logger.error(f"Failed to add client to inbound {ib.remote_inbound_id}: {e_indiv}")

                primary_inbound, primary_panel = matched_pairs[0]
                srv_res = await self.session.execute(
                    select(Server).where(Server.panel_id == primary_panel.id, Server.is_active.is_(True))
                )
                primary_server = srv_res.scalars().first()
                if not primary_server:
                    primary_server = (
                        await self.session.execute(select(Server).where(Server.is_active.is_(True)))
                    ).scalars().first()
                if not primary_server:
                    srv_any = await self.session.execute(
                        select(Server).where(Server.panel_id == primary_panel.id)
                    )
                    primary_server = srv_any.scalars().first()
                    if primary_server:
                        primary_server.is_active = True
                    else:
                        import urllib.parse
                        parsed = urllib.parse.urlparse(primary_panel.base_url)
                        primary_server = Server(
                            id=uuid.uuid4(),
                            name=f"سرور {primary_panel.name}",
                            location_code="ALL",
                            location_name=primary_panel.name,
                            flag_emoji="🌐",
                            priority=10,
                            panel_id=primary_panel.id,
                            subscription_host=parsed.hostname or primary_panel.base_url,
                            subscription_port=parsed.port or 443,
                            is_active=True,
                            capacity_limit=1000,
                            current_clients=0,
                        )
                        self.session.add(primary_server)
                        await self.session.flush()

                subscription = Subscription(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    order_id=order.id,
                    server_id=primary_server.id,
                    panel_id=primary_panel.id,
                    inbound_id=primary_inbound.id,
                    client_uuid=client_uuid,
                    client_email=client_email,
                    protocol=protocol,
                    subscription_token=sub_id,
                    traffic_limit_bytes=product.traffic_bytes,
                    traffic_used_bytes=0,
                    expires_at=expiry_dt,
                    status=SubscriptionStatus.ACTIVE,
                )
                self.session.add(subscription)

                order.status = OrderStatus.ACTIVE
                order.server_id = primary_server.id

                for ib, _ in matched_pairs:
                    ib.client_count += 1
                if primary_server:
                    primary_server.current_clients += 1
                await self.session.flush()

                logger.info(
                    f"Successfully provisioned unlimited multi-location subscription {subscription.id} across {len(matched_pairs)} inbounds"
                )
                return subscription

            # --- Case 2: Volume Traffic Plan (traffic_bytes > 0) -> Provision on 1 selected location ---
            else:
                server, inbound, panel = await self.allocator.allocate_server_and_inbound(
                    location_code=loc,
                    protocol=protocol,
                    product=product,
                    preferred_server_id=str(order.server_id) if order.server_id else None,
                )

                adapter = self._get_panel_adapter(panel)
                from app.integrations.vpn_ui.adapter import VpnUIPanelAdapter
                from app.integrations.vpn_ui.schemas import WireGuardClientSpec, OpenVPNClientSpec
                if isinstance(adapter, VpnUIPanelAdapter) and protocol.lower() == "wireguard":
                    wg_spec = WireGuardClientSpec(
                        id=client_uuid,
                        email=client_email,
                        totalGB=product.traffic_bytes,
                        expiryTime=expiry_ms,
                        enable=True,
                        tgId=int(user.telegram_id) if user.telegram_id else 0,
                        subId=sub_id,
                    )
                    add_res = await adapter.add_wireguard_client(inbound.remote_inbound_id, wg_spec)
                elif isinstance(adapter, VpnUIPanelAdapter) and protocol.lower() in ("amneziawg", "awg"):
                    wg_spec = WireGuardClientSpec(
                        id=client_uuid,
                        email=client_email,
                        totalGB=product.traffic_bytes,
                        expiryTime=expiry_ms,
                        enable=True,
                        tgId=int(user.telegram_id) if user.telegram_id else 0,
                        subId=sub_id,
                    )
                    add_res = await adapter.add_amneziawg_client(inbound.remote_inbound_id, wg_spec)
                elif isinstance(adapter, VpnUIPanelAdapter) and protocol.lower() == "openvpn":
                    ovpn_spec = OpenVPNClientSpec(
                        id=client_uuid,
                        username=client_email.split("@")[0],
                        email=client_email,
                        totalGB=product.traffic_bytes,
                        expiryTime=expiry_ms,
                        enable=True,
                        subId=sub_id,
                    )
                    add_res = await adapter.add_openvpn_client(inbound.remote_inbound_id, ovpn_spec)
                else:
                    add_res = await adapter.add_client(inbound.remote_inbound_id, spec)
                logger.info(f"Panel response for adding client: {add_res}")

                subscription = Subscription(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    order_id=order.id,
                    server_id=server.id,
                    panel_id=panel.id,
                    inbound_id=inbound.id,
                    client_uuid=client_uuid,
                    client_email=client_email,
                    protocol=protocol,
                    subscription_token=sub_id,
                    traffic_limit_bytes=product.traffic_bytes,
                    traffic_used_bytes=0,
                    expires_at=expiry_dt,
                    status=SubscriptionStatus.ACTIVE,
                )
                self.session.add(subscription)

                order.status = OrderStatus.ACTIVE
                order.server_id = server.id
                await self.allocator.increment_capacity(server.id, inbound.id)
                await self.session.flush()

                logger.info(f"Successfully provisioned subscription {subscription.id} on panel {panel.name}")
                return subscription

        except Exception as exc:
            logger.error(f"Provisioning failed for order {order_id}: {exc}", exc_info=True)
            try:
                order.status = OrderStatus.PROVISIONING_FAILED
                order.provisioning_error = str(exc)
                await self.session.flush()
            except Exception:
                try:
                    await self.session.rollback()
                    failed_order = await self.session.get(Order, order_id)
                    if failed_order:
                        failed_order.status = OrderStatus.PROVISIONING_FAILED
                        failed_order.provisioning_error = str(exc)
                        await self.session.flush()
                except Exception as e_rollback:
                    logger.error(f"Failed to record provisioning error for order {order_id}: {e_rollback}")
            raise ProvisioningError(f"خطا در ایجاد اکانت در پنل: {exc}") from exc

    async def provision_trial_subscription(self, user: User) -> Subscription:
        """Provision a free trial VPN client on an active panel with configurable inbound, limits, and duration."""
        from app.core.config import settings

        inbound = None
        server = None
        panel = None

        trial_inbound_id = getattr(settings, "TRIAL_INBOUND_ID", None)
        if trial_inbound_id:
            try:
                target_inb = await self.session.get(Inbound, uuid.UUID(trial_inbound_id))
                if target_inb and target_inb.is_active:
                    inbound = target_inb
                    panel = await self.session.get(Panel, inbound.panel_id)
                    srv_res = await self.session.execute(
                        select(Server).where(Server.panel_id == inbound.panel_id, Server.is_active.is_(True))
                    )
                    server = srv_res.scalars().first()
            except Exception as e:
                logger.warning(f"Could not load custom TRIAL_INBOUND_ID {trial_inbound_id}: {e}")

        # Fallback to automatic selection if no dedicated inbound configured or found
        if not inbound or not panel or not server or not panel.is_active or not panel.is_healthy:
            server_res = await self.session.execute(
                select(Server, Inbound, Panel)
                .join(Panel, Server.panel_id == Panel.id)
                .join(Inbound, Inbound.panel_id == Panel.id)
                .where(
                    Server.is_active.is_(True),
                    Panel.is_active.is_(True),
                    Panel.is_healthy.is_(True),
                    Inbound.is_active.is_(True),
                )
                .order_by(Server.priority.desc())
            )
            row = server_res.first()
            if not row:
                raise ProvisioningError("هیچ سرور فعالی برای ارائه اکانت تست یافت نشد.")
            server, inbound, panel = row

        try:
            tg_id = int(user.telegram_id) if user.telegram_id else 0
        except (ValueError, TypeError):
            tg_id = 0
        clean_tg = "".join(c for c in str(user.telegram_id or "0") if c.isalnum())
        client_uuid = generate_secure_uuid()
        sub_id = generate_sub_id(16)
        email_suffix = await SettingsService.get_effective_email_suffix(self.session)
        client_email = f"trial_{clean_tg}_{secrets.token_hex(2)}{email_suffix}"

        now = datetime.now(timezone.utc)
        traffic_bytes = settings.TRIAL_TRAFFIC_MB * 1024 * 1024
        duration_days = getattr(settings, "TRIAL_DURATION_DAYS", 1)
        duration_hours = getattr(settings, "TRIAL_DURATION_HOURS", duration_days * 24)
        expiry_dt = now + timedelta(hours=duration_hours)
        expiry_ms = int(expiry_dt.timestamp() * 1000)

        spec = ClientSpec(
            id=client_uuid,
            email=client_email,
            flow="xtls-rprx-vision" if inbound.protocol.lower() == "vless" else "",
            limitIp=1,
            totalGB=traffic_bytes,
            expiryTime=expiry_ms,
            enable=True,
            tgId=tg_id,
            subId=sub_id,
        )

        try:
            adapter = self._get_panel_adapter(panel)
            await adapter.add_client(inbound.remote_inbound_id, spec)
        except Exception as exc:
            logger.error(f"Failed to add trial client to remote 3x-ui ({exc})")
            raise VPNPrivateError(f"خطا در ایجاد اکانت تست روی پنل سرور: {exc}") from exc

        subscription = Subscription(
            id=uuid.uuid4(),
            user_id=user.id,
            order_id=None,
            server_id=server.id,
            panel_id=panel.id,
            inbound_id=inbound.id,
            client_uuid=client_uuid,
            client_email=client_email,
            protocol=inbound.protocol.lower(),
            subscription_token=sub_id,
            traffic_limit_bytes=traffic_bytes,
            traffic_used_bytes=0,
            expires_at=expiry_dt,
            status=SubscriptionStatus.ACTIVE,
        )
        self.session.add(subscription)
        await self.session.flush()
        return subscription

    async def renew_subscription(
        self,
        subscription_id: uuid.UUID,
        product_id: uuid.UUID,
    ) -> Subscription:
        """Renew subscription duration and reset/extend traffic quota."""
        sub = await self.session.get(Subscription, subscription_id)
        if not sub:
            raise VPNPrivateError("سرویس مورد نظر یافت نشد.")

        product = await self.session.get(Product, product_id)
        if not product:
            raise VPNPrivateError("طرح تمدید نامعتبر است.")

        if not product.duration_days or product.duration_days <= 0:
            raise VPNPrivateError("مدت زمان طرح تمدید نامعتبر است.")

        panel = await self.session.get(Panel, sub.panel_id)
        inbound = await self.session.get(Inbound, sub.inbound_id)
        if not panel or not inbound:
            raise VPNPrivateError("سرور مرتبط با سرویس در دسترس نیست.")
        if not panel.is_active or not panel.is_healthy:
            raise VPNPrivateError("پنل سرور غیرفعال یا خارج از دسترس است.")
        if not inbound.is_active:
            raise VPNPrivateError("اینباند مرتبط با سرویس غیرفعال است.")

        now = datetime.now(timezone.utc)
        sub_expires = sub.expires_at
        if sub_expires is not None and sub_expires.tzinfo is None:
            sub_expires = sub_expires.replace(tzinfo=timezone.utc)
        base_time = sub_expires if sub_expires and sub_expires > now else now
        new_expiry = base_time + timedelta(days=product.duration_days)
        new_expiry_ms = int(new_expiry.timestamp() * 1000)

        adapter = self._get_panel_adapter(panel)

        # Unify traffic extension: add package volume to current quota
        add_bytes = product.traffic_bytes or int(product.traffic_gb * (1024**3))
        base_quota = max(sub.traffic_limit_bytes or 0, sub.traffic_used_bytes or 0)
        new_limit_bytes = base_quota + add_bytes

        try:
            # Update client on panel with extended traffic & expiry
            spec = ClientSpec(
                id=sub.client_uuid,
                email=sub.client_email,
                flow="xtls-rprx-vision" if sub.protocol.lower() == "vless" else "",
                limitIp=inbound.max_clients or 2,
                totalGB=new_limit_bytes,
                expiryTime=new_expiry_ms,
                enable=True,
                subId=sub.subscription_token,
            )
            await adapter.update_client(inbound.remote_inbound_id, sub.client_uuid, spec)
        except Exception as exc:
            logger.error(f"Panel update failed during renewal of subscription {sub.id}: {exc}")
            raise VPNPrivateError(f"خطا در به‌روزرسانی پنل هنگام تمدید: {exc}") from exc

        # Update local subscription
        sub.expires_at = new_expiry
        sub.traffic_limit_bytes = new_limit_bytes
        sub.status = SubscriptionStatus.ACTIVE
        sub.alert_traffic_sent = False
        sub.alert_time_sent = False
        await self.session.flush()

        logger.info(f"Renewed subscription {sub.id}. New expiry: {new_expiry}")
        return sub

    async def get_connection_details(self, subscription_id: uuid.UUID) -> Dict[str, Any]:
        """Fetch links (VLESS/VMess/WireGuard/AmneziaWG/OpenVPN and Subscription URL)."""
        sub = await self.session.get(Subscription, subscription_id)
        if not sub:
            raise VPNPrivateError("سرویس یافت نشد.")

        server = await self.session.get(Server, sub.server_id)
        panel = await self.session.get(Panel, sub.panel_id)
        inbound = await self.session.get(Inbound, sub.inbound_id)

        adapter = self._get_panel_adapter(panel) if panel else None
        
        server_host = server.subscription_host if server else "sub.vpnprivate.com"
        sub_port = server.subscription_port if server else 443
        use_ssl = (sub_port in (443, 8443)) or (panel and "https" in (panel.base_url or ""))

        sub_url = ""
        if adapter:
            sub_url = await adapter.get_subscription_url(
                server_host=server_host,
                sub_port=sub_port,
                sub_id=sub.subscription_token,
                use_ssl=use_ssl,
            )
        else:
            proto_prefix = "https" if use_ssl else "http"
            port_part = f":{sub_port}" if sub_port not in (80, 443) else ""
            sub_url = f"{proto_prefix}://{server_host}{port_part}/sub/{sub.subscription_token}"

        inbound_stream = {}
        if inbound and inbound.stream_settings:
            try:
                inbound_stream = json.loads(inbound.stream_settings)
            except Exception:
                pass

        loc_code = server.location_code if server else "ALL"
        config_prefix = await SettingsService.get_effective_config_title_prefix(self.session)
        inbound_dict = {
            "port": inbound.port if inbound else 443,
            "remark": f"{config_prefix}-{loc_code}",
            "stream_settings": inbound_stream,
            "settings": inbound_stream,
            "protocol": inbound.protocol if inbound else sub.protocol,
        }
        client_dict = {
            "id": sub.client_uuid,
            "email": sub.client_email,
            "username": (sub.client_email or "user").split("@")[0],
            "remark": f"{config_prefix}-{loc_code}",
            "flow": "xtls-rprx-vision" if sub.protocol == "vless" else "",
        }

        proto_lower = (sub.protocol or "vless").strip().lower()

        # Ensure adapter supports WireGuard/AWG/OpenVPN URL and config generation
        from app.integrations.vpn_ui.adapter import VpnUIPanelAdapter
        active_adapter = adapter
        if active_adapter is None:
            if proto_lower in ("wireguard", "amneziawg", "openvpn"):
                active_adapter = VpnUIPanelAdapter(client=None)
            else:
                active_adapter = ThreeXUIPanelAdapter(client=None)
        elif proto_lower in ("wireguard", "amneziawg", "openvpn") and not isinstance(active_adapter, VpnUIPanelAdapter):
            client = getattr(adapter, "client", None)
            active_adapter = VpnUIPanelAdapter(client=client)

        direct_link = ""
        if active_adapter:
            direct_link = active_adapter.generate_client_url(
                protocol=sub.protocol,
                inbound_data=inbound_dict,
                client_data=client_dict,
                server_host=server_host,
            )

        raw_config = None
        if proto_lower in ("wireguard", "amneziawg", "openvpn") and active_adapter:
            if proto_lower == "wireguard":
                raw_config = active_adapter.generate_wireguard_conf(
                    inbound_data=inbound_dict,
                    client_data=client_dict,
                    server_host=server_host,
                )
            elif proto_lower in ("amneziawg", "awg"):
                raw_config = active_adapter.generate_awg_conf(
                    inbound_data=inbound_dict,
                    client_data=client_dict,
                    server_host=server_host,
                )
            elif proto_lower == "openvpn":
                raw_config = active_adapter.generate_openvpn_conf(
                    inbound_data=inbound_dict,
                    client_data=client_dict,
                    server_host=server_host,
                )

        is_unlimited = (sub.traffic_limit_bytes == 0)
        loc_name = "تمام لوکیشن‌های مجاز پلن (چندسروره)" if is_unlimited else (server.location_name if server else "آلمان")
        flag = "🌐" if is_unlimited else (server.flag_emoji if server else "🇩🇪")

        res = {
            "subscription_url": sub_url,
            "direct_link": direct_link,
            "location": loc_name,
            "flag": flag,
            "protocol": sub.protocol.upper(),
            "remark": f"{config_prefix}-{loc_code}",
        }
        if raw_config is not None:
            res["raw_config"] = raw_config

        return res
