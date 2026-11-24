# VPN Private — استراتژی پشتیبان‌گیری و بازیابی فاجعه (Disaster Recovery)

### ۱. سیاست پشتیبان‌گیری (Backup Policy)
- **پایگاه‌داده PostgreSQL:** فایل‌های Dump به همراه فشرده‌سازی gzip به صورت ساعتی یا روزانه ذخیره می‌شوند.
- **دوره نگهداری (Retention Period):** ۳۰ روز. فایل‌های قدیمی‌تر به طور خودکار پاکسازی می‌شوند.
- **محل ذخیره‌سازی:** مسیر محلی `/var/backups/vpnprivate` و قابلیت ارسال خودکار به فضای ابری امن خارج از سرور (Off-site S3 / Remote Storage).

---

### ۲. زمان‌بندی پشتیبان‌گیری خودکار (Cron Job)
جهت اجرای روزانه پشتیبان‌گیری در ساعت ۳:۰۰ بامداد، دستور زیر را در سرور اجرا فرمایید:
```bash
crontab -e
```
و خط زیر را اضافه نمایید:
```cron
0 3 * * * /opt/vpn-private/scripts/backup.sh >> /var/log/vpnprivate_backup.log 2>&1
```

---

### ۳. فرآیند بازیابی در شرایط اضطراری (Disaster Recovery Step-by-Step)
در صورت بروز نقص سخت‌افزاری سرور یا خرابی اطلاعات:

1. سرور جدید را آماده کرده و سورس کد را Clone کنید:
   ```bash
   git clone <repo_url> /opt/vpn-private
   cd /opt/vpn-private
   cp .env.example .env
   ```
2. کانتینرهای پایگاه‌داده را اجرا نمایید:
   ```bash
   docker compose up -d vpnprivate-postgres vpnprivate-redis
   ```
3. اسکریپت بازیابی را با تعیین فایل پشتیبان اجرا نمایید:
   ```bash
   chmod +x scripts/restore.sh
   ./scripts/restore.sh /path/to/vpn_private_20261031_030000.sql.gz
   ```
4. کلیه سرویس‌ها را بالا بیاورید:
   ```bash
   docker compose up -d
   docker compose ps
   ```
5. وضعیت سلامت سیستم را بررسی نمایید:
   ```bash
   curl http://localhost/health
   ```
   تمامی اطلاعات کاربران، کیف‌پول‌ها، کدهای پیگیری، و سوابق اشتراک‌ها بدون کوچکترین ریزش اطلاعاتی بازگردانی خواهند شد.

