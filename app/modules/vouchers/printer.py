"""High-Resolution Printable HTML Page Generator for Physical Voucher Cards."""
import html
from typing import List
from app.database.models.voucher import Voucher
from app.modules.vouchers.service import generate_qr_base64


def render_printable_vouchers_html(
    vouchers: List[Voucher],
    batch_id: str,
    bot_username: str,
) -> str:
    """Generate high-resolution printable HTML with standard 8 cards per A4 page."""
    clean_bot = bot_username.lstrip("@").strip() if bot_username and bot_username.strip() else "VPNPrivateSell_Bot"
    total_cards = len(vouchers)
    cards_per_sheet = 8  # 2 columns x 4 rows
    sheets = [vouchers[i:i + cards_per_sheet] for i in range(0, total_cards, cards_per_sheet)]
    total_pages = len(sheets)

    sheets_html = []
    card_index = 0

    for sheet_idx, sheet_cards in enumerate(sheets):
        card_items_html = []
        for v in sheet_cards:
            card_index += 1
            qr_target_url = f"https://t.me/{clean_bot}?start=v_{v.code}"
            qr_base64 = generate_qr_base64(qr_target_url)

            # Determine Title and Specs
            if v.voucher_type == "plan" and v.product:
                plan_name = html.escape(v.product.name)
                plan_spec = f"{v.product.duration_days} روزه  •  حجم: {v.product.traffic_display}"
                type_badge = "کارت اشتراک اختصاصی"
            elif v.voucher_type == "plan":
                plan_name = "اشتراک اختصاصی اینترنت"
                plan_spec = "پلن پریمیوم پرسرعت"
                type_badge = "کارت اشتراک اختصاصی"
            else:
                plan_name = f"کارت هدیه: {v.amount_toman:,} تومان"
                plan_spec = "شارژ مستقیم و آنی کیف پول کاربری"
                type_badge = "کارت هدیه کیف پول"

            exp_label = f"انقضا: {v.expires_at.strftime('%Y-%m-%d')}" if v.expires_at else "اعتبار: نامحدود ♾️"
            serial_no = f"SN: {batch_id[-8:].upper()}-{card_index:04d}"

            card_html = f"""
            <div class="card">
                <div class="card-inner">
                    <!-- Header -->
                    <div class="card-header">
                        <div class="brand">
                            <svg class="logo-icon" viewBox="0 0 24 24" fill="none" stroke="#d4af37" stroke-width="2">
                                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                                <path d="M9 12l2 2 4-4"/>
                            </svg>
                            <span class="brand-text">VPN PRIVATE</span>
                        </div>
                        <span class="badge">{type_badge}</span>
                    </div>

                    <!-- Plan / Value Info -->
                    <div class="card-body">
                        <div class="plan-title">{plan_name}</div>
                        <div class="plan-specs">{plan_spec}</div>
                    </div>

                    <!-- Scratch Box -->
                    <div class="scratch-box">
                        <div class="scratch-hint">░░ محل خراشیدن اسکرچ ░░</div>
                        <div class="scratch-code">{v.code}</div>
                    </div>

                    <!-- Footer: QR & Instructions -->
                    <div class="card-footer">
                        <div class="qr-col">
                            <img src="data:image/png;base64,{qr_base64}" alt="QR" class="qr-img" />
                        </div>
                        <div class="info-col">
                            <div class="step-guide">اسکن با دوربین یا ارسال در ربات:</div>
                            <div class="bot-link">t.me/{clean_bot}</div>
                            <div class="meta-row">
                                <span class="serial">{serial_no}</span>
                                <span class="expiry">{exp_label}</span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            """
            card_items_html.append(card_html)

        is_last_sheet = (sheet_idx == total_pages - 1)
        sheet_class = "sheet" if is_last_sheet else "sheet page-break"
        sheets_html.append(f"""
        <div class="{sheet_class}">
            {''.join(card_items_html)}
        </div>
        """)

    full_html = f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>چاپ کارت‌های ووچر فیزیکی - دوره {batch_id}</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;600;700;800&family=JetBrains+Mono:wght@700&display=swap');

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            font-family: 'Vazirmatn', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1e2029;
            color: #ffffff;
            direction: rtl;
            -webkit-font-smoothing: antialiased;
        }}

        /* Screen Toolbar */
        .toolbar {{
            position: sticky;
            top: 0;
            z-index: 1000;
            background: rgba(18, 19, 26, 0.95);
            backdrop-filter: blur(10px);
            border-bottom: 2px solid #d4af37;
            padding: 12px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
        }}

        .toolbar-info h2 {{
            font-size: 16px;
            color: #d4af37;
            margin-bottom: 4px;
        }}

        .toolbar-meta {{
            font-size: 13px;
            color: #a0a5b8;
        }}

        .toolbar-meta span {{
            margin-left: 14px;
        }}

        .btn-print {{
            background: linear-gradient(135deg, #d4af37 0%, #aa8421 100%);
            color: #0b0c10;
            border: none;
            padding: 10px 24px;
            border-radius: 8px;
            font-weight: 700;
            font-size: 14px;
            cursor: pointer;
            box-shadow: 0 4px 15px rgba(212, 175, 55, 0.4);
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }}

        .btn-print:hover {{
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(212, 175, 55, 0.6);
        }}

        .page-container {{
            padding: 20px 0;
            display: flex;
            flex-direction: column;
            align-items: center;
        }}

        /* Standard A4 Sheet (210mm x 297mm) */
        .sheet {{
            width: 210mm;
            min-height: 297mm;
            height: 297mm;
            padding: 6mm 7mm;
            background: #ffffff;
            margin: 0 auto 20px auto;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
            display: grid;
            grid-template-columns: repeat(2, 95mm);
            grid-template-rows: repeat(4, 67mm);
            column-gap: 6mm;
            row-gap: 5mm;
            justify-content: center;
            align-content: center;
        }}

        /* Physical Scratch Voucher Card */
        .card {{
            width: 95mm;
            height: 67mm;
            background: linear-gradient(135deg, #181922 0%, #101117 100%);
            border: 1.8px solid #d4af37;
            border-radius: 9px;
            padding: 3mm;
            color: #ffffff;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }}

        .card::before {{
            content: "";
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            border: 1px dashed rgba(212, 175, 55, 0.25);
            border-radius: 7px;
            pointer-events: none;
            margin: 1.5mm;
        }}

        .card-inner {{
            position: relative;
            z-index: 1;
            display: flex;
            flex-direction: column;
            height: 100%;
            justify-content: space-between;
        }}

        /* Header */
        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 1.5mm;
            border-bottom: 1px solid rgba(212, 175, 55, 0.3);
        }}

        .brand {{
            display: flex;
            align-items: center;
            gap: 5px;
        }}

        .logo-icon {{
            width: 15px;
            height: 15px;
        }}

        .brand-text {{
            font-size: 11px;
            font-weight: 800;
            letter-spacing: 1px;
            color: #d4af37;
        }}

        .badge {{
            font-size: 8.5px;
            background: rgba(212, 175, 55, 0.15);
            border: 1px solid rgba(212, 175, 55, 0.6);
            color: #f3d179;
            padding: 1px 6px;
            border-radius: 10px;
            font-weight: 600;
        }}

        /* Plan / Value */
        .card-body {{
            text-align: center;
            margin: 1.5mm 0;
        }}

        .plan-title {{
            font-size: 13.5px;
            font-weight: 800;
            color: #ffffff;
            text-shadow: 0 1px 3px rgba(0,0,0,0.8);
            line-height: 1.2;
        }}

        .plan-specs {{
            font-size: 9.5px;
            color: #e2b94c;
            margin-top: 1px;
            font-weight: 600;
        }}

        /* Scratch Area */
        .scratch-box {{
            background: repeating-linear-gradient(45deg, #2a2b37, #2a2b37 6px, #1d1e27 6px, #1d1e27 12px);
            border: 1.4px dashed #d4af37;
            border-radius: 6px;
            padding: 1.8mm 2mm;
            text-align: center;
            margin: 1mm 0;
        }}

        .scratch-hint {{
            font-size: 8px;
            color: #d4af37;
            font-weight: 700;
            letter-spacing: 0.5px;
            margin-bottom: 1px;
        }}

        .scratch-code {{
            font-family: 'JetBrains Mono', 'Consolas', monospace;
            font-size: 12.5px;
            font-weight: 700;
            letter-spacing: 1.8px;
            color: #ffffff;
            background: rgba(10, 11, 15, 0.85);
            padding: 1px 8px;
            border-radius: 4px;
            display: inline-block;
            direction: ltr;
        }}

        /* Footer */
        .card-footer {{
            display: flex;
            align-items: center;
            gap: 2.5mm;
            padding-top: 1.5mm;
            border-top: 1px solid rgba(212, 175, 55, 0.25);
        }}

        .qr-col {{
            flex-shrink: 0;
        }}

        .qr-img {{
            width: 14mm;
            height: 14mm;
            display: block;
            background: #ffffff;
            padding: 1mm;
            border-radius: 4px;
        }}

        .info-col {{
            flex: 1;
            min-width: 0;
        }}

        .step-guide {{
            font-size: 7.5px;
            color: #a7abbd;
            white-space: nowrap;
        }}

        .bot-link {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 9px;
            color: #d4af37;
            font-weight: 700;
            direction: ltr;
            text-align: right;
            margin: 0.5mm 0;
        }}

        .meta-row {{
            display: flex;
            justify-content: space-between;
            font-size: 7px;
            color: #83889b;
        }}

        .serial {{
            font-family: 'JetBrains Mono', monospace;
            direction: ltr;
        }}

        /* Print Specific CSS */
        @media print {{
            .no-print, .toolbar {{
                display: none !important;
            }}

            html, body {{
                background: #ffffff !important;
                margin: 0 !important;
                padding: 0 !important;
            }}

            .page-container {{
                padding: 0 !important;
            }}

            .sheet {{
                margin: 0 !important;
                padding: 6mm 7mm !important;
                box-shadow: none !important;
                border: none !important;
            }}

            .page-break {{
                page-break-after: always !important;
                break-after: page !important;
            }}

            .card {{
                -webkit-print-color-adjust: exact !important;
                print-color-adjust: exact !important;
            }}
        }}

        @page {{
            size: A4 portrait;
            margin: 0;
        }}
    </style>
</head>
<body>
    <div class="toolbar no-print">
        <div class="toolbar-info">
            <h2>🖨 پیش‌نمایش و چاپ کارت‌های فیزیکی اسکرچ ووچر</h2>
            <div class="toolbar-meta">
                <span>شناسه بچ: <b>{batch_id}</b></span>
                <span>تعداد کارت‌ها: <b>{total_cards} عدد</b></span>
                <span>تعداد صفحات: <b>{total_pages} صفحه A4</b> (۸ کارت در هر برگه)</span>
                <span style="background: rgba(212,175,55,0.15); border: 1px solid rgba(212,175,55,0.4); padding: 2px 8px; border-radius: 6px; color: #ffd700;">ربات تلگرام: <b>@{clean_bot}</b></span>
                <button type="button" onclick="changePrintBot()" style="background: #252836; color: #d4af37; border: 1px solid #d4af37; padding: 3px 8px; border-radius: 6px; font-size: 11px; cursor: pointer; font-weight: bold;">✏️ تغییر ربات چاپ</button>
            </div>
        </div>
        <button class="btn-print" onclick="window.print()">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="6 9 6 2 18 2 18 9"></polyline>
                <path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"></path>
                <rect x="6" y="14" width="12" height="8"></rect>
            </svg>
            چاپ کارت‌ها (Print A4)
        </button>
    </div>

    <div class="page-container">
        {''.join(sheets_html)}
    </div>

    <script>
    function changePrintBot() {{
        const cur = '{clean_bot}';
        const nextBot = prompt('نام کاربری ربات تلگرام جهت چاپ روی کارت‌ها و کیوآرکد (بدون @):', cur);
        if (nextBot && nextBot.trim() && nextBot.trim().replace(/^@/, '') !== cur) {{
            const u = new URL(window.location.href);
            u.searchParams.set('bot_username', nextBot.trim().replace(/^@/, ''));
            window.location.href = u.toString();
        }}
    }}
    </script>
</body>
</html>
"""
    return full_html
