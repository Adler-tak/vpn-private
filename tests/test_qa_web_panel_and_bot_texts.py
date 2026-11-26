import os
import re
import json
import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_HTML_PATH = os.path.join(BASE_DIR, "app", "static", "admin.html")
ADMIN_PY_PATH = os.path.join(BASE_DIR, "app", "api", "routes", "admin.py")
BOT_TEXTS_PY_PATH = os.path.join(BASE_DIR, "app", "modules", "bot_texts", "service.py")
BOT_DIR = os.path.join(BASE_DIR, "app", "bot")

CANONICAL_KEYS = [
    "start_welcome",
    "purchase_guide",
    "services_guide",
    "wallet_guide",
    "card_payment_guide",
    "referral_guide",
    "support_guide",
    "rules_terms",
    "free_trial_guide",
    "order_approved_delivery"
]

@pytest.fixture(scope="module")
def admin_html_content():
    with open(ADMIN_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()

@pytest.fixture(scope="module")
def admin_py_content():
    with open(ADMIN_PY_PATH, "r", encoding="utf-8") as f:
        return f.read()

@pytest.fixture(scope="module")
def bot_texts_service():
    import sys
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    from app.modules.bot_texts import service
    return service


# =====================================================================
# 1. Bot Texts Service & 10 Canonical Keys
# =====================================================================
def test_all_10_canonical_keys_in_default_bot_texts(bot_texts_service):
    defaults = bot_texts_service.DEFAULT_BOT_TEXTS
    for key in CANONICAL_KEYS:
        assert key in defaults, f"Key '{key}' missing from DEFAULT_BOT_TEXTS"
        meta = defaults[key]
        assert "title" in meta and meta["title"], f"Key '{key}' missing title"
        assert "description" in meta and meta["description"], f"Key '{key}' missing description"
        assert "default" in meta and meta["default"], f"Key '{key}' missing default text"


def test_bot_texts_placeholder_interpolation(bot_texts_service):
    # Test dynamic placeholder replacement for channel, support, and bot usernames
    welcome_text = bot_texts_service.get_bot_text("start_welcome")
    assert "@{channel_username}" not in welcome_text
    assert "@{support_username}" not in welcome_text

    # Test delivery template contains expected formatting variables
    delivery_tmpl = bot_texts_service.DEFAULT_BOT_TEXTS["order_approved_delivery"]["default"]
    for var in ["{client_email}", "{location}", "{flag}", "{protocol}", "{subscription_url}"]:
        assert var in delivery_tmpl, f"Variable {var} missing from order_approved_delivery default"


def test_update_bot_texts_and_get_all(bot_texts_service, tmp_path, monkeypatch):
    test_file = tmp_path / "bot_texts_test.json"
    monkeypatch.setattr(bot_texts_service, "PRIMARY_BOT_TEXTS_FILE", test_file)
    monkeypatch.setattr(bot_texts_service, "BOT_TEXTS_FILE", test_file)
    monkeypatch.setattr(bot_texts_service, "DATA_BOT_TEXTS_FILE", test_file)

    # Test update
    bot_texts_service.update_bot_texts({"start_welcome": "تست شروع اختصاصی"})
    retrieved = bot_texts_service.get_bot_text("start_welcome")
    assert retrieved == "تست شروع اختصاصی"

    # Test get_all_bot_texts
    all_texts = bot_texts_service.get_all_bot_texts()
    assert len(all_texts) >= 10
    keys_in_all = {item["key"] for item in all_texts}
    for k in CANONICAL_KEYS:
        assert k in keys_in_all


# =====================================================================
# 2. Admin HTML Bot Texts UI Audit
# =====================================================================
def test_admin_html_all_10_textareas_exist_with_matching_ids(admin_html_content):
    for key in CANONICAL_KEYS:
        expected_id = f"bot-text-{key}"
        pattern = rf'<textarea[^>]*id=["\']{expected_id}["\'][^>]*>'
        match = re.search(pattern, admin_html_content)
        assert match is not None, f"Textarea with ID '{expected_id}' not found in admin.html"


def test_admin_html_textareas_have_labels_hints_and_rtl(admin_html_content):
    # Verify RTL styling in CSS
    assert ".bot-textarea" in admin_html_content
    assert "text-align: right;" in admin_html_content
    assert "unicode-bidi: plaintext;" in admin_html_content

    for key in CANONICAL_KEYS:
        expected_id = f"bot-text-{key}"
        idx = admin_html_content.find(f'id="{expected_id}"')
        assert idx != -1

        # Check surrounding container (up to 800 chars before)
        before = admin_html_content[max(0, idx - 800):idx]
        assert "<label" in before, f"Label missing for {expected_id}"
        assert f">{key}</span>" in before or key in before, f"Key badge missing for {expected_id}"
        assert "<p class=\"text-[11px]" in before or "text-slate-400" in before, f"Hint missing for {expected_id}"


def test_bot_text_keys_array_in_js(admin_html_content):
    match = re.search(r'BOT_TEXT_KEYS\s*=\s*\[([\s\S]*?)\]', admin_html_content)
    assert match is not None, "BOT_TEXT_KEYS array not found in JavaScript"

    keys_found = [k.strip().strip("'\"") for k in match.group(1).split(",") if k.strip().strip("'\"")]
    assert len(keys_found) == 10
    for key in CANONICAL_KEYS:
        assert key in keys_found, f"Key '{key}' missing from JS BOT_TEXT_KEYS array"


def test_load_bot_texts_loads_all_fields(admin_html_content):
    # Extract loadBotTexts function
    m = re.search(r'async function loadBotTexts\s*\(\)\s*\{([\s\S]*?)\n    \}', admin_html_content)
    assert m is not None, "loadBotTexts function not found in admin.html"
    code = m.group(1)

    assert "BOT_TEXT_KEYS.forEach" in code
    assert "bot-text-${key}" in code
    assert "cfg-client-email-suffix" in code
    assert "cfg-config-title-prefix" in code
    assert "cfg-bot-username" in code
    assert "cfg-bot-channel-username" in code
    assert "cfg-bot-support-username" in code
    assert "cfg-bot-channel-lock-enabled" in code


def test_save_bot_texts_gathers_and_sends_all_fields(admin_html_content):
    # Extract saveBotTexts function
    m = re.search(r'async function saveBotTexts\s*\(\)\s*\{([\s\S]*?)\n    \}', admin_html_content)
    assert m is not None, "saveBotTexts function not found in admin.html"
    code = m.group(1)

    assert "BOT_TEXT_KEYS.forEach" in code
    assert "bot-text-${key}" in code
    assert "cfg-client-email-suffix" in code
    assert "cfg-config-title-prefix" in code
    assert "cfg-bot-username" in code
    assert "cfg-bot-channel-username" in code
    assert "cfg-bot-support-username" in code
    assert "cfg-bot-channel-lock-enabled" in code
    assert "method: 'PUT'" in code or 'method: "PUT"' in code
    assert "/bot/texts" in code


# =====================================================================
# 3. Admin Login & Security UI
# =====================================================================
def test_login_form_line_495_placeholder_clean(admin_html_content):
    lines = admin_html_content.splitlines()
    line_495 = lines[494] if len(lines) >= 495 else ""
    assert 'placeholder="نام کاربری مدیریت"' in line_495
    assert 'value=' not in line_495, "Login username input must not have a prefilled value attribute"


def test_no_domcontentloaded_autofill(admin_html_content):
    dcl_matches = re.findall(r'document\.addEventListener\([\'"]DOMContentLoaded[\'"][\s\S]*?\}\);', admin_html_content)
    for block in dcl_matches:
        assert "SecureAdmin" not in block, "SecureAdmin must not be hardcoded in DOMContentLoaded"
        if "login-username" in block:
            assert "value =" not in block and "value=" not in block, "login-username must not be autofilled in DOMContentLoaded"


def test_update_admin_display_names_does_not_overwrite_login_username(admin_html_content):
    m = re.search(r'function updateAdminDisplayNames\s*\([^)]*\)\s*\{([\s\S]*?)\n    \}', admin_html_content)
    assert m is not None, "updateAdminDisplayNames not found in admin.html"
    code = m.group(1)

    # Verify login-username is NOT assigned a value
    assert "document.getElementById('login-username').value" not in code
    assert 'document.getElementById("login-username").value' not in code


def test_admin_api_login_checks_db_and_env(admin_py_content):
    # Verify login endpoint handles .env, SettingsService DB, and Bcrypt
    assert '@router.post("/login")' in admin_py_content
    assert 'settings.ADMIN_WEB_PASSWORD' in admin_py_content
    assert 'settings.ADMIN_WEB_PASSWORD_HASH' in admin_py_content
    assert 'SettingsService.get_value("ADMIN_WEB_PASSWORD")' in admin_py_content
    assert 'bcrypt.checkpw' in admin_py_content
    assert 'security_store.record_login_failure' in admin_py_content
    assert 'security_store.check_ip_blocked' in admin_py_content


# =====================================================================
# 4. Suffix and Remark Prefix UI
# =====================================================================
def test_suffix_and_remark_prefix_inputs_exist(admin_html_content):
    sfx_match = re.search(r'<input[^>]*id=["\']cfg-client-email-suffix["\'][^>]*>', admin_html_content)
    pfx_match = re.search(r'<input[^>]*id=["\']cfg-config-title-prefix["\'][^>]*>', admin_html_content)

    assert sfx_match is not None, "cfg-client-email-suffix input element not found in admin.html"
    assert pfx_match is not None, "cfg-config-title-prefix input element not found in admin.html"


# =====================================================================
# 5. Audit Logs View
# =====================================================================
def test_audit_logs_tab_view_and_elements(admin_html_content):
    assert 'id="tab-audit-logs-view"' in admin_html_content
    assert 'id="audit-logs-tbody"' in admin_html_content
    assert 'id="audit-action-filter"' in admin_html_content
    assert 'id="audit-search-input"' in admin_html_content
    assert 'id="audit-limit-select"' in admin_html_content
    assert 'id="btn-audit-prev"' in admin_html_content
    assert 'id="btn-audit-next"' in admin_html_content
    assert 'id="audit-current-page"' in admin_html_content
    assert 'id="btn-refresh-audit-logs"' in admin_html_content


def test_load_audit_logs_js_function(admin_html_content):
    m = re.search(r'async function loadAuditLogs\s*\([\s\S]*?\)\s*\{([\s\S]*?)\n    \}', admin_html_content)
    assert m is not None, "loadAuditLogs function not found in admin.html"
    code = m.group(1)

    assert "/audit-logs" in code
    assert "limit=" in code
    assert "offset=" in code
    assert "action=" in code
    assert "search=" in code
    assert "audit-logs-tbody" in code


def test_admin_api_list_audit_logs_endpoint(admin_py_content):
    assert '@router.get("/audit-logs")' in admin_py_content
    assert 'select(AuditLog)' in admin_py_content
    assert 'NEW_USERS' in admin_py_content
    assert 'USER_STARTS' in admin_py_content
    assert 'VOUCHERS' in admin_py_content
    assert 'ORDERS' in admin_py_content
    assert 'RENEWALS' in admin_py_content
    assert 'WALLET' in admin_py_content
    assert 'TRIALS' in admin_py_content
    assert 'ADMIN_ACTIONS' in admin_py_content


# =====================================================================
# 6. Bot Handlers Usage of Canonical Keys
# =====================================================================
def test_all_10_canonical_keys_used_in_bot_handlers():
    calls = {}
    for root, dirs, files in os.walk(BOT_DIR):
        for file in files:
            if file.endswith(".py"):
                fpath = os.path.join(root, file)
                relpath = os.path.relpath(fpath, BASE_DIR)
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                matches = re.findall(r'get_bot_text\(\s*["\']([^"\']+)["\']', content)
                for m in matches:
                    calls.setdefault(m, []).append(relpath)

    for key in CANONICAL_KEYS:
        assert key in calls, f"Canonical key '{key}' is NOT used in any bot handler!"
        assert len(calls[key]) >= 1, f"Key '{key}' has 0 invocations"
