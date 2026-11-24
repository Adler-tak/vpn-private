"""Excel & CSV Voucher Import, Template Generation, and Export Utility."""
import csv
import io
import re
from typing import Any, Dict, List, Optional, Tuple


def calculate_duration_days(months: int) -> int:
    """Calculate exact subscription days for given months.
    12 months is explicitly mapped to 365 days (compensating for the 5-day difference).
    6 months -> 183 days.
    3 months -> 90 days.
    1 month -> 30 days.
    """
    if months <= 0:
        return 30
    if months == 12:
        return 365
    if months == 6:
        return 183
    if months == 3:
        return 90
    if months == 1:
        return 30
    return int(round(months * (365.0 / 12.0)))


def generate_voucher_template_excel() -> bytes:
    """Generate sample downloadable Excel template (.xlsx) with 3 required columns."""
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Vouchers_Template"
    ws.views.sheetView[0].rightToLeft = True

    # Header styling
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")  # Slate-800
    thin_border = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )
    center_align = Alignment(horizontal="center", vertical="center")

    headers = ["مدت زمان (ماه)", "نام کاربری", "رمز عبور"]
    ws.append(headers)

    for col_idx in range(1, 4):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border

    # Sample rows
    samples = [
        [1, "user_demo_1m", "pass_98234A"],
        [3, "user_demo_3m", "pass_10492B"],
        [6, "user_demo_6m", "pass_88291C"],
        [12, "user_demo_12m", "pass_77192D"],
    ]

    row_font = Font(name="Calibri", size=10)
    for r_idx, row_data in enumerate(samples, start=2):
        ws.append(row_data)
        for c_idx in range(1, 4):
            cell = ws.cell(row=r_idx, column=c_idx)
            cell.font = row_font
            cell.alignment = center_align
            cell.border = thin_border

    # Set column widths
    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 30

    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()


def parse_vouchers_from_bytes(file_bytes: bytes, filename: str) -> List[Dict[str, Any]]:
    """Parse uploaded Excel or CSV file into list of voucher dicts.
    Required 3 columns:
      Column 1: Duration in months
      Column 2: Username
      Column 3: Password
    """
    raw_rows: List[List[Any]] = []

    is_csv = filename.lower().endswith(".csv")
    if is_csv:
        # Decode CSV with UTF-8 or Latin1
        text = ""
        for encoding in ("utf-8-sig", "utf-8", "cp1256", "latin1"):
            try:
                text = file_bytes.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        reader = csv.reader(io.StringIO(text))
        raw_rows = [row for row in reader if any(field.strip() for field in row)]
    else:
        # Excel (.xlsx)
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            if any(cell is not None and str(cell).strip() != "" for cell in row):
                raw_rows.append([cell for cell in row])

    if not raw_rows:
        return []

    # Check if first row is header
    first_row_str = " ".join(str(c or "").lower() for c in raw_rows[0])
    has_header = any(
        kw in first_row_str
        for kw in ["ماه", "مدت", "duration", "month", "یوزر", "user", "پسورد", "pass", "رمز"]
    )

    data_rows = raw_rows[1:] if has_header else raw_rows
    vouchers: List[Dict[str, Any]] = []

    for row in data_rows:
        if len(row) < 2:
            continue

        raw_months = row[0]
        raw_username = row[1] if len(row) > 1 else ""
        raw_password = row[2] if len(row) > 2 else ""

        # Parse months
        try:
            str_months = str(raw_months).strip()
            for fa, en in zip("۰۱۲۳۴۵۶۷۸۹", "0123456789"):
                str_months = str_months.replace(fa, en)
            months = int(float(str_months)) if str_months else 1
        except (ValueError, TypeError):
            months = 1

        username = str(raw_username or "").strip()
        password = str(raw_password or "").strip()

        if not username and not password:
            continue

        days = calculate_duration_days(months)

        vouchers.append({
            "duration_months": months,
            "duration_days": days,
            "username": username or None,
            "password": password or None,
        })

    return vouchers


def export_unused_vouchers_excel(vouchers_data: List[Dict[str, Any]]) -> bytes:
    """Generate Excel (.xlsx) of unused vouchers with duration, username, password."""
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Unused_Vouchers"
    ws.views.sheetView[0].rightToLeft = True

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="0F766E", end_color="0F766E", fill_type="solid")  # Teal-700
    thin_border = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )
    center_align = Alignment(horizontal="center", vertical="center")

    headers = ["مدت زمان (ماه)", "نام کاربری", "رمز عبور", "کد ووچر", "تعداد روز اعتبار"]
    ws.append(headers)

    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border

    row_font = Font(name="Calibri", size=10)
    for r_idx, v in enumerate(vouchers_data, start=2):
        row_values = [
            v.get("duration_months") or 1,
            v.get("username") or "-",
            v.get("password") or "-",
            v.get("code") or "-",
            v.get("duration_days") or 30,
        ]
        ws.append(row_values)
        for c_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=r_idx, column=c_idx)
            cell.font = row_font
            cell.alignment = center_align
            cell.border = thin_border

    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 25
    ws.column_dimensions["C"].width = 25
    ws.column_dimensions["D"].width = 28
    ws.column_dimensions["E"].width = 18

    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()
