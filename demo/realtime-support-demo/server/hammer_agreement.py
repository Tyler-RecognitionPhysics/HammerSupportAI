"""Hammer product agreement copy and pricing for Zapier emails (Drive, Facebook AIA, …)."""

from __future__ import annotations

import base64
import html
import os
import re
from datetime import datetime, timedelta, timezone

_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
_LOGO_FILE = os.path.join(_SERVER_DIR, "static", "email", "hammer-ai-logo.png")
# Static file from web/public → dist/email/ on Vercel. API mirror: GET /api/email/hammer-ai-logo.png
_DEFAULT_LOGO_PATH = "/email/hammer-ai-logo.png"
_HAMMER_LOGO_RED = "#CC0000"
_LOCAL_API_BASE = "http://127.0.0.1:8780"
_LOCAL_WEB_BASE = "http://127.0.0.1:5173"

def _normalize_host(website: str) -> str:
    s = website.strip()
    for prefix in ("https://", "http://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :]
    return s.rstrip("/") or website.strip()


def _compact_product_text(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())

# USD monthly tiers (voice demo pricing sheet — main.ts PRICING block)
HAMMER_DRIVE_USD_BANDS: tuple[tuple[int, int, int], ...] = (
    (10, 30, 299),
    (31, 60, 399),
    (61, 80, 599),
    (81, 10_000, 999),
)

# Canada (CAD) — 10–30 tier per Hammer Drive price sheet
HAMMER_DRIVE_CAD_BANDS: tuple[tuple[int, int, int], ...] = (
    (10, 30, 299),
    (31, 60, 399),
    (61, 80, 599),
    (81, 10_000, 1299),
)

# Facebook AIA — flat $299/mo Hammer fee (never lot-tiered) + separate $15/day Meta minimum
FACEBOOK_AIA_HAMMER_MONTHLY_USD = 299
FACEBOOK_AIA_META_DAILY_MIN_USD = 15

# MarketPoster seat tiers (USD) — additional seats +$50/mo above tier base
MARKETPOSTER_ADDITIONAL_USER_MONTHLY_USD = 50
MARKETPOSTER_TIER_PRICES: tuple[tuple[int, int], ...] = ((1, 199), (3, 299), (5, 599))

# Hammer Connect standalone (no MarketPoster)
HAMMER_CONNECT_MONTHLY_USD = 99


def dealership_display_name(website: str) -> str:
    """Prefer human dealership name; fall back to a readable host label."""
    s = website.strip()
    if not s:
        return "your dealership"
    lowered = s.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        s = _normalize_host(s)
    # "Victory Motorsports" or "Acme Ford of Dallas"
    if " " in s and not re.fullmatch(r"[\w.-]+\.[a-z]{2,}", s, flags=re.I):
        return s
    host = _normalize_host(s).split("/")[0]
    if "." in host:
        stem = host.split(".")[0]
        return stem.replace("-", " ").replace("_", " ").title()
    return s


def _parse_lot_count(
    lot_size: str | None,
    selected_plan: str | None,
    *,
    min_lot: int = 10,
) -> int | None:
    for text in (lot_size, selected_plan):
        if not text:
            continue
        for match in re.finditer(r"\b(\d{1,3})\b", text):
            value = int(match.group(1))
            if min_lot <= value <= 500:
                return value
    if selected_plan:
        band = re.search(r"(\d{1,3})\s*[-–]\s*(\d{1,3})", selected_plan)
        if band:
            lo, hi = int(band.group(1)), int(band.group(2))
            return (lo + hi) // 2
        plus = re.search(r"(\d{2,3})\s*\+", selected_plan)
        if plus:
            return int(plus.group(1))
    return None


def _monthly_for_lot(lot_count: int, cad: bool) -> tuple[int, str]:
    bands = HAMMER_DRIVE_CAD_BANDS if cad else HAMMER_DRIVE_USD_BANDS
    currency = "CAD" if cad else "USD"
    for lo, hi, price in bands:
        if lo <= lot_count <= hi:
            band_label = f"{lo}–{hi} cars" if hi < 10_000 else f"{lo}+ cars"
            return price, band_label
    return bands[-1][2], f"{bands[-1][0]}+ cars"


def _parse_price_from_plan(selected_plan: str) -> int | None:
    match = re.search(r"\$?\s*(\d{3,4})\s*(?:/?\s*mo)?", selected_plan, flags=re.I)
    if match:
        return int(match.group(1))
    return None


def is_hammer_connect_signup(selected_plan: str | None) -> bool:
    if not selected_plan:
        return False
    lowered = selected_plan.lower()
    compact = _compact_product_text(selected_plan)
    if "marketposter" in compact:
        return False
    return "hammerconnect" in compact or "connect" in lowered


def is_marketposter_signup(selected_plan: str | None) -> bool:
    if not selected_plan:
        return False
    compact = _compact_product_text(selected_plan)
    if is_hammer_connect_signup(selected_plan):
        return False
    return "marketposter" in compact


def is_facebook_aia_signup(selected_plan: str | None) -> bool:
    if not selected_plan:
        return False
    lowered = selected_plan.lower()
    compact = _compact_product_text(selected_plan)
    if "hammerdrive" in compact or is_marketposter_signup(selected_plan):
        return False
    return "facebookaia" in compact or (
        "aia" in lowered and ("facebook" in lowered or "meta" in lowered)
    )


def is_hammer_drive_signup(selected_plan: str | None) -> bool:
    if not selected_plan:
        return False
    if (
        is_facebook_aia_signup(selected_plan)
        or is_marketposter_signup(selected_plan)
        or is_hammer_connect_signup(selected_plan)
    ):
        return False
    lowered = selected_plan.lower()
    compact = _compact_product_text(selected_plan)
    return "hammerdrive" in compact or re.search(r"\bdrive\b", lowered, re.I) is not None


def _parse_seat_count(
    selected_plan: str | None,
    seat_count: str | None = None,
    lot_size: str | None = None,
) -> int | None:
    for text in (seat_count, selected_plan, lot_size):
        if not text:
            continue
        match = re.search(r"(\d+)\s*(?:users?|seats?)", text, flags=re.I)
        if match:
            value = int(match.group(1))
            if 1 <= value <= 50:
                return value
    return None


def marketposter_monthly_for_users(user_count: int) -> int:
    """Map seat count to monthly total (tier pricing + $50 per user above tier band)."""
    count = max(1, user_count)
    if count == 1:
        return 199
    if count == 2:
        return 199 + MARKETPOSTER_ADDITIONAL_USER_MONTHLY_USD
    if count <= 3:
        return 299
    if count == 4:
        return 299 + MARKETPOSTER_ADDITIONAL_USER_MONTHLY_USD
    if count <= 5:
        return 599
    return 599 + (count - 5) * MARKETPOSTER_ADDITIONAL_USER_MONTHLY_USD


def is_cad_signup(
    selected_plan: str | None,
    website: str | None = None,
    currency: str | None = None,
) -> bool:
    if currency and currency.strip().upper() == "CAD":
        return True
    text = f"{selected_plan or ''} {website or ''}".lower()
    return "cad" in text or "canada" in text or ".ca/" in text or text.rstrip("/").endswith(".ca")


def resolve_hammer_drive_pricing(
    selected_plan: str | None,
    lot_size: str | None,
    website: str | None = None,
    currency: str | None = None,
    seat_count: str | None = None,
) -> dict[str, str] | None:
    if not is_hammer_drive_signup(selected_plan):
        return None

    cad = is_cad_signup(selected_plan, website, currency)
    min_lot = 10
    lot_count = _parse_lot_count(lot_size, selected_plan, min_lot=min_lot)
    if lot_count is None:
        plan_price = _parse_price_from_plan(selected_plan or "")
        if plan_price is None:
            return None
        monthly = plan_price
        band_label = ""
    else:
        monthly, band_label = _monthly_for_lot(lot_count, cad)
        plan_price = _parse_price_from_plan(selected_plan or "")
        if plan_price is not None and plan_price != monthly:
            monthly = plan_price

    currency = "CAD" if cad else "USD"

    return {
        "productLine": "hammer_drive",
        "agreementTemplate": "hammer_drive",
        "serviceDescription": "HammerAI + Webchat",
        "lotBand": band_label,
        "lotCount": str(lot_count) if lot_count is not None else "",
        "subscriptionMonthlyAmount": str(monthly),
        "subscriptionMonthlyDisplay": f"${monthly} {currency} /month",
        "billingSummary": (
            f"Month-to-month at ${monthly} {currency}/month. No trial. No signup or activation fee."
        ),
        "firstMonthBillingDisplay": f"${monthly} {currency} today",
        "nextPaymentDate": format_next_payment_date(),
        "currency": currency,
    }


def resolve_facebook_aia_pricing(
    selected_plan: str | None,
    lot_size: str | None = None,
    website: str | None = None,
    currency: str | None = None,
    seat_count: str | None = None,
) -> dict[str, str] | None:
    if not is_facebook_aia_signup(selected_plan):
        return None

    # lot_size is ignored for monthly Hammer fee — always FACEBOOK_AIA_HAMMER_MONTHLY_USD
    _ = lot_size, seat_count

    cad = is_cad_signup(selected_plan, website, currency)
    currency_label = "CAD" if cad else "USD"
    monthly = FACEBOOK_AIA_HAMMER_MONTHLY_USD
    plan_price = _parse_price_from_plan(selected_plan or "")
    if plan_price is not None:
        monthly = plan_price
    daily_min = FACEBOOK_AIA_META_DAILY_MIN_USD

    return {
        "productLine": "facebook_aia",
        "agreementTemplate": "facebook_aia",
        "serviceDescription": "Facebook Advertising + AI",
        "subscriptionMonthlyAmount": str(monthly),
        "subscriptionMonthlyDisplay": f"${monthly} {currency_label}/month",
        "metaAdSpendDailyAmount": str(daily_min),
        "metaAdSpendDailyDisplay": f"${daily_min}/day",
        "billingSummary": (
            f"${monthly} {currency_label}/month Hammer subscription plus ${daily_min}/day Meta ad spend "
            "(ad spend billed separately; non-refundable)."
        ),
        "firstMonthBillingDisplay": f"${monthly} {currency_label} today",
        "nextPaymentDate": format_next_payment_date(),
        "currency": currency_label,
    }


def resolve_marketposter_pricing(
    selected_plan: str | None,
    lot_size: str | None = None,
    website: str | None = None,
    currency: str | None = None,
    seat_count: str | None = None,
) -> dict[str, str] | None:
    if not is_marketposter_signup(selected_plan):
        return None

    currency_label = "CAD" if is_cad_signup(selected_plan, website, currency) else "USD"
    users = _parse_seat_count(selected_plan, seat_count) or 3
    monthly = marketposter_monthly_for_users(users)
    plan_price = _parse_price_from_plan(selected_plan or "")
    if plan_price is not None:
        monthly = plan_price
    user_label = "User" if users == 1 else "Users"

    return {
        "productLine": "marketposter",
        "agreementTemplate": "marketposter",
        "agreementBrandName": "MarketPoster",
        "agreementThankYouBrand": "Hammer/MarketPoster",
        "serviceDescription": "Facebook Market Place Posting + Hammer Connect",
        "seatCount": str(users),
        "subscriptionMonthlyAmount": str(monthly),
        "subscriptionMonthlyDisplay": f"${monthly}/month + {users} {user_label}",
        "additionalUserMonthlyDisplay": f"${MARKETPOSTER_ADDITIONAL_USER_MONTHLY_USD} monthly",
        "billingSummary": (
            f"${monthly}/month for {users} {user_label.lower()} on MarketPoster "
            f"(+${MARKETPOSTER_ADDITIONAL_USER_MONTHLY_USD}/mo per additional user)."
        ),
        "firstMonthBillingDisplay": f"${monthly} today",
        "nextPaymentDate": format_next_payment_date(),
        "currency": currency_label,
    }


def resolve_hammer_connect_pricing(
    selected_plan: str | None,
    lot_size: str | None = None,
    website: str | None = None,
    currency: str | None = None,
    seat_count: str | None = None,
) -> dict[str, str] | None:
    if not is_hammer_connect_signup(selected_plan):
        return None

    currency_label = "CAD" if is_cad_signup(selected_plan, website, currency) else "USD"
    monthly = HAMMER_CONNECT_MONTHLY_USD

    return {
        "productLine": "hammer_connect",
        "agreementTemplate": "hammer_connect",
        "agreementBrandName": "Hammer Connect",
        "agreementThankYouBrand": "Hammer/Hammer Connect",
        "serviceDescription": "Hammer Connect",
        "subscriptionMonthlyAmount": str(monthly),
        "subscriptionMonthlyDisplay": f"${monthly}/month",
        "billingSummary": (
            f"${monthly}/month Hammer Connect standalone (Marketplace messaging; no MarketPoster)."
        ),
        "firstMonthBillingDisplay": f"${monthly} today",
        "nextPaymentDate": format_next_payment_date(),
        "currency": currency_label,
    }


def resolve_agreement_pricing(
    selected_plan: str | None,
    lot_size: str | None,
    website: str | None = None,
    currency: str | None = None,
    seat_count: str | None = None,
) -> dict[str, str] | None:
    """Resolve pricing + product metadata for agreement emails (first matching product wins)."""
    for resolver in (
        resolve_facebook_aia_pricing,
        resolve_marketposter_pricing,
        resolve_hammer_connect_pricing,
        resolve_hammer_drive_pricing,
    ):
        result = resolver(
            selected_plan,
            lot_size,
            website=website,
            currency=currency,
            seat_count=seat_count,
        )
        if result:
            return result
    return None


def format_next_payment_date() -> str:
    next_pay = datetime.now(timezone.utc).date() + timedelta(days=30)
    return f"{next_pay.month}/{next_pay.day}/{next_pay.year % 100:02d}"


def _is_public_https_url(url: str) -> bool:
    lowered = url.lower()
    return lowered.startswith("https://") and "127.0.0.1" not in lowered and "localhost" not in lowered


def _is_local_base(url: str) -> bool:
    lowered = url.lower()
    return "127.0.0.1" in lowered or "localhost" in lowered


def _public_site_base() -> str:
    """API origin for logo URLs — local dev defaults to uvicorn on :8780."""
    for key in ("REALTIME_SALES_PUBLIC_BASE_URL", "REALTIME_SALES_SITE_URL"):
        value = os.environ.get(key, "").strip().rstrip("/")
        if value:
            return value
    vercel = os.environ.get("VERCEL_URL", "").strip()
    if vercel:
        return vercel if vercel.startswith("http") else f"https://{vercel}"
    return _LOCAL_API_BASE


def agreement_logo_url() -> str:
    override = os.environ.get("HAMMER_AGREEMENT_LOGO_URL", "").strip()
    if override:
        return override
    return f"{_public_site_base().rstrip('/')}{_DEFAULT_LOGO_PATH}"


def agreement_logo_url_for_email() -> str:
    """Logo URL included in webhook metadata (local :8780 when developing)."""
    override = os.environ.get("HAMMER_AGREEMENT_LOGO_URL", "").strip()
    if override:
        return override
    return agreement_logo_url()


def _is_email_loadable_image_url(url: str) -> bool:
    """Gmail needs public HTTPS; local http works for browser/Zapier editor preview only."""
    if _is_public_https_url(url):
        return True
    return _use_remote_logo_image() and _is_local_base(url) and url.lower().startswith("http://")


def agreement_logo_src() -> str:
    """Image src for HTML email — inline base64 by default so Gmail/Zapier always show the banner."""
    embed = os.environ.get("HAMMER_AGREEMENT_LOGO_EMBED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    if embed and os.path.isfile(_LOGO_FILE):
        with open(_LOGO_FILE, "rb") as logo_file:
            encoded = base64.standard_b64encode(logo_file.read()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    return agreement_logo_url()


def _agreement_pricing_fields(pricing: dict[str, str]) -> dict[str, str]:
    monthly = pricing["subscriptionMonthlyAmount"]
    currency = pricing.get("currency", "USD")
    fields = {
        "template": pricing.get("agreementTemplate", "hammer_drive"),
        "monthly": monthly,
        "currency": currency,
        "sub": pricing["subscriptionMonthlyDisplay"],
        "charge_today": pricing.get("firstMonthBillingDisplay") or f"${monthly} {currency} today",
        "next_pay": pricing.get("nextPaymentDate") or format_next_payment_date(),
        "service": pricing.get("serviceDescription", "HammerAI + Webchat"),
    }
    if pricing.get("metaAdSpendDailyDisplay"):
        fields["ad_spend"] = pricing["metaAdSpendDailyDisplay"]
    if pricing.get("additionalUserMonthlyDisplay"):
        fields["additional_users"] = pricing["additionalUserMonthlyDisplay"]
    template = fields["template"]
    if template in ("marketposter", "hammer_connect"):
        fields["brand"] = pricing.get("agreementBrandName", "MarketPoster")
        fields["thank_you"] = pricing.get("agreementThankYouBrand", "Hammer/MarketPoster")
    return fields


def _cancellation_policy() -> str:
    return (
        "CANCELLATION POLICY: To cancel your account, please submit a cancel request by going to "
        "hammertime.com/help and clicking unsubscribe. You may cancel with 1 day notice before the next "
        "billing date following your upfront term. After this point you must give 30 days notice to "
        "cancel before the end of the relevant subscription term. If you have any questions or concerns, "
        "please contact our Customer Success team at (512) 883-1336. We are happy to help!"
    )


def _cancellation_policy_html() -> str:
    return (
        "<strong>CANCELLATION POLICY:</strong> To cancel your account, please submit a cancel request "
        "by going to <a href=\"https://hammertime.com/help\">hammertime.com/help</a> and clicking "
        "<em>unsubscribe</em>. You may cancel with <strong>1 day notice</strong> before the next "
        "billing date following your upfront term. After this point you must give 30 days notice to "
        "cancel before the end of the relevant subscription term. If you have any questions or concerns, "
        "please contact our Customer Success team at (512) 883-1336. We are happy to help!"
    )


def _facebook_aia_cancellation_policy() -> str:
    return _cancellation_policy()


def _marketposter_cancellation_policy() -> str:
    return _cancellation_policy()


def _hammer_drive_cancellation_policy() -> str:
    return _cancellation_policy()


def _marketposter_marketplace_disclaimer() -> str:
    return (
        "Hammer offers posting tools with safeguards, including a 10-vehicle limit per session, "
        "intended to help align with Facebook Marketplace policies. Hammer assumes no liability for "
        "account suspensions, restrictions, or bans resulting from Marketplace activity. The user is "
        "solely responsible for complying with all Facebook Terms of Service and platform guidelines."
    )


def _data_access_authorization_drive() -> str:
    return (
        "DATA ACCESS AUTHORIZATION: By approving, you authorize HAMMER to access your dealership's "
        "inventory feeds, integrate with your lead sources and/or CRM/LMS, and implement our web chat "
        "on your website for service optimization."
    )


def _additional_users_block_plain() -> list[str]:
    return ["", "Additional Users $50 monthly", ""]


def _additional_users_block_html(gap: str) -> str:
    return '<p style="margin:0 0 1.2em;">Additional Users $50 monthly</p>'


def _market_style_agreement_lines(dealership: str, fields: dict[str, str]) -> list[str]:
    """MarketPoster / Hammer Connect agreement — blank lines match the approved email reference."""
    brand = fields["brand"]
    thank_you = fields["thank_you"]
    additional_users = (
        ["", "Additional Users $50 monthly"] if fields.get("template") == "marketposter" else []
    )
    data_access_block = (
        _data_access_authorization_drive()
        + "For more information, you can visit the links below or contact Hammer Support at (512) 883-1336."
        " We can't wait to take your dealership to the next level!\U0001f680"
    )
    return [
        f"Hello {dealership},",
        "",
        'Please reply to this email with "I approve" as your agreement to the terms below:',
        "",
        f"Welcome to {brand}! Your subscription is month-to-month.",
        "",
        f"Your service description: {fields['service']}",
        f"Subscription: {fields['sub']}",
        f"Next Payment: {fields['next_pay']}",
        "",
        f"You will be charged {fields['charge_today']} for your first month of {brand}.",
        *additional_users,
        "",
        _marketposter_cancellation_policy(),
        "",
        _marketposter_marketplace_disclaimer(),
        "",
        data_access_block,
        "",
        f"Thank you for choosing {thank_you}!",
        "",
        "Cheers,",
        "",
        "Hannah",
        "",
        "",
        "Hannah",
        "hannah@hammer-corp.com",
    ]


def _marketposter_cancellation_policy_html() -> str:
    return _cancellation_policy_html()


def _marketposter_data_access_html() -> str:
    return (
        "<strong>DATA ACCESS AUTHORIZATION:</strong> By approving, you authorize HAMMER to access "
        "your dealership&rsquo;s inventory feeds, integrate with your lead sources and/or CRM/LMS, "
        "and implement our web chat on your website for service optimization."
    )


def _market_style_agreement_html_body(
    dealership: str,
    fields: dict[str, str],
) -> str:
    """HTML for MarketPoster / Hammer Connect — spacing and bolding match the approved reference."""
    gap = _html_email_spacer()
    dealer = html.escape(dealership, quote=True)
    service = html.escape(fields["service"], quote=True)
    sub = html.escape(fields["sub"], quote=True)
    next_pay = html.escape(fields["next_pay"], quote=True)
    charge_amount = html.escape(f"${fields['monthly']}", quote=True)
    brand = html.escape(fields["brand"], quote=True)
    thank_you = html.escape(fields["thank_you"], quote=True)
    disclaimer = html.escape(_marketposter_marketplace_disclaimer(), quote=True)
    additional_users_html = (
        _additional_users_block_html(gap) if fields.get("template") == "marketposter" else ""
    )
    data_access_html = (
        "<strong>DATA ACCESS AUTHORIZATION:</strong> By approving, you authorize HAMMER to access "
        "your dealership&rsquo;s inventory feeds, integrate with your lead sources and/or CRM/LMS, "
        "and implement our web chat on your website for service optimization."
        "For more information, you can visit the links below or contact Hammer Support at (512) 883-1336. "
        "We can&rsquo;t wait to take your dealership to the next level!&#x1F680;"
    )
    return f"""<p style="margin:0 0 1.2em;">Hello {dealer},</p>
<p style="margin:0 0 1.2em;"><em>Please reply to this email with &ldquo;<strong>I approve</strong>&rdquo; as your agreement to the terms below:</em></p>
<p style="margin:0 0 1.2em;">Welcome to <strong>{brand}</strong>! Your subscription is month-to-month.</p>
<p style="margin:0 0 1.2em;"><strong>Your service description:</strong> {service}<br>
<strong>Subscription:</strong> {sub}<br>
<strong>Next Payment:</strong> <strong>{next_pay}</strong></p>
<p style="margin:0 0 1.2em;">You will be charged <strong>{charge_amount}</strong> today for your first month of {brand}.</p>
{additional_users_html}
<p style="margin:0 0 1.2em;">{_marketposter_cancellation_policy_html()}</p>
<p style="margin:0 0 1.2em;">{disclaimer}</p>
<p style="margin:0 0 1.2em;">{data_access_html}</p>
<p style="margin:0 0 1.2em;"><strong>Thank you for choosing {thank_you}!</strong></p>
<p style="margin:0 0 1.2em;">Cheers,</p>
<p style="margin:0 0 2.4em;">Hannah</p>
<p style="margin:0;">Hannah<br>hannah@hammer-corp.com</p>"""


def _agreement_email_lines(dealership: str, fields: dict[str, str]) -> list[str]:
    """Line breaks match the agreement email reference (single blank between every section)."""
    if fields.get("template") in ("marketposter", "hammer_connect"):
        return _market_style_agreement_lines(dealership, fields)
    data_access_block = (
        _data_access_authorization_drive()
        + "For more information, you can visit the links below or contact Hammer Support at (512) 883-1336."
        " We can't wait to take your dealership to the next level!\U0001f680"
    )
    if fields.get("template") == "facebook_aia":
        return [
            f"Hello {dealership},",
            "",
            'Please reply to this email with "I approve" as your agreement to the terms below:',
            "",
            "Welcome to HAMMER! Your subscription is month-to-month.",
            "",
            f"Your service description: {fields['service']}",
            f"Subscription: {fields['sub']}",
            f"Ad Spend: {fields['ad_spend']}",
            f"Next Payment: {fields['next_pay']}",
            "",
            "Ad spend for Ads is non-refundable",
            "",
            f"You will be charged {fields['charge_today']} for your first month of Hammer.",
            "",
            _facebook_aia_cancellation_policy(),
            "",
            data_access_block,
            "",
            "Thank you for choosing Hammer!",
            "",
            "Cheers,",
            "",
            "Hannah",
            "",
            "",
            "Hannah",
            "hannah@hammer-corp.com",
        ]
    return [
        f"Hello {dealership},",
        "",
        'Please reply to this email with "I approve" as your agreement to the terms below:',
        "",
        "Welcome to HAMMER! Your subscription is month-to-month.",
        "",
        f"Your service description: {fields['service']}",
        f"Subscription: {fields['sub']}",
        f"Next Payment: {fields['next_pay']}",
        "",
        f"You will be charged {fields['charge_today']} for your first 30 days of Hammer. "
        "This charge is non-refundable.",
        "",
        _hammer_drive_cancellation_policy(),
        "",
        data_access_block,
        "",
        "Thank you for choosing Hammer!",
        "",
        "Cheers,",
        "",
        "Hannah",
        "",
        "",
        "Hannah",
        "hannah@hammer-corp.com",
    ]


def build_agreement_email(dealership: str, pricing: dict[str, str]) -> str:
    fields = _agreement_pricing_fields(pricing)
    return "\n".join(_agreement_email_lines(dealership, fields))


def build_hammer_drive_agreement_email(dealership: str, pricing: dict[str, str]) -> str:
    return build_agreement_email(dealership, pricing)


def _use_remote_logo_image() -> bool:
    """Remote PNG only when explicitly configured (Gmail needs a working HTTPS URL)."""
    if os.environ.get("HAMMER_EMAIL_LOGO_USE_IMAGE", "").strip().lower() in ("1", "true", "yes"):
        return True
    return bool(os.environ.get("HAMMER_AGREEMENT_LOGO_URL", "").strip())


def _hammer_email_logo_header_html(*, logo_src: str | None = None) -> str:
    """Email-safe logo: HTML banner (always renders in Gmail) + PNG when a working HTTPS URL is set."""
    src = logo_src or agreement_logo_url_for_email()
    text_banner = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 24px;max-width:560px;">
  <tr>
    <td align="left" style="background-color:{_HAMMER_LOGO_RED};padding:18px 24px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td style="font-family:Arial Black,Arial,Helvetica,sans-serif;font-size:40px;font-weight:900;color:#FFFFFF;line-height:1;letter-spacing:-0.5px;">HAMMER</td>
          <td style="font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:700;color:#FFFFFF;vertical-align:bottom;padding:0 0 6px 4px;">AI</td>
        </tr>
      </table>
    </td>
  </tr>
</table>"""
    if not _use_remote_logo_image() or not _is_email_loadable_image_url(src):
        return text_banner
    safe_src = html.escape(src, quote=True)
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 24px;max-width:560px;">
  <tr>
    <td style="padding:0;line-height:0;font-size:0;background-color:{_HAMMER_LOGO_RED};">
      <img src="{safe_src}" alt="HAMMER AI" width="560" style="display:block;width:100%;max-width:560px;height:auto;border:0;" />
    </td>
  </tr>
</table>"""


def _html_email_spacer() -> str:
    # Returns empty string — spacing is handled by margin-bottom on <p> elements.
    # Previously returned a <p>&nbsp;</p> spacer which rendered as " " blank lines
    # in copy-paste and some plain-text email views.
    return ""


def build_agreement_email_html(
    dealership: str,
    pricing: dict[str, str],
    *,
    logo_src: str | None = None,
) -> str:
    """HTML body for Gmail / Zapier — reference spacing + bold/italic styling."""
    fields = _agreement_pricing_fields(pricing)
    logo_header = _hammer_email_logo_header_html(logo_src=logo_src)
    dealer = html.escape(dealership, quote=True)
    service = html.escape(fields["service"], quote=True)
    sub = html.escape(fields["sub"], quote=True)
    next_pay = html.escape(fields["next_pay"], quote=True)
    charge_today = html.escape(fields["charge_today"], quote=True)
    gap = _html_email_spacer()

    # Shared DATA ACCESS paragraph: authorization sentence runs directly into "For more information"
    # with no space — matching the exact reference layout provided.
    _data_html = (
        "<strong>DATA ACCESS AUTHORIZATION:</strong> By approving, you authorize HAMMER to access "
        "your dealership&rsquo;s inventory feeds, integrate with your lead sources and/or CRM/LMS, "
        "and implement our web chat on your website for service optimization."
        "For more information, you can visit the links below or contact Hammer Support at (512) 883-1336. "
        "We can&rsquo;t wait to take your dealership to the next level!&#x1F680;"
    )

    if fields.get("template") in ("marketposter", "hammer_connect"):
        body_html = _market_style_agreement_html_body(dealership, fields)
    elif fields.get("template") == "facebook_aia":
        ad_spend = html.escape(fields["ad_spend"], quote=True)
        service_block = f"""<strong>Your service description:</strong> {service}<br>
<strong>Subscription:</strong> {sub}<br>
<strong>Ad Spend:</strong> <strong>{ad_spend}</strong><br>
<strong>Next Payment:</strong> <strong>{next_pay}</strong>"""
        body_html = f"""<p style="margin:0 0 1.2em;">Hello {dealer},</p>
<p style="margin:0 0 1.2em;"><em>Please reply to this email with &ldquo;<strong>I approve</strong>&rdquo; as your agreement to the terms below:</em></p>
<p style="margin:0 0 1.2em;">Welcome to <strong>HAMMER</strong>! Your subscription is month-to-month.</p>
<p style="margin:0 0 1.2em;">{service_block}</p>
<p style="margin:0 0 1.2em;"><strong>Ad spend for Ads is non-refundable</strong></p>
<p style="margin:0 0 1.2em;">You will be charged <strong>{charge_today}</strong> for your first month of Hammer.</p>
<p style="margin:0 0 1.2em;">{_cancellation_policy_html()}</p>
<p style="margin:0 0 1.2em;">{_data_html}</p>
<p style="margin:0 0 1.2em;"><strong>Thank you for choosing Hammer!</strong></p>
<p style="margin:0 0 1.2em;">Cheers,</p>
<p style="margin:0 0 2.4em;">Hannah</p>
<p style="margin:0;">Hannah<br>hannah@hammer-corp.com</p>"""
    else:
        service_block = f"""<strong>Your service description:</strong> {service}<br>
<strong>Subscription:</strong> {sub}<br>
<strong>Next Payment:</strong> <strong>{next_pay}</strong>"""
        body_html = f"""<p style="margin:0 0 1.2em;">Hello {dealer},</p>
<p style="margin:0 0 1.2em;"><em>Please reply to this email with &ldquo;<strong>I approve</strong>&rdquo; as your agreement to the terms below:</em></p>
<p style="margin:0 0 1.2em;">Welcome to <strong>HAMMER</strong>! Your subscription is month-to-month.</p>
<p style="margin:0 0 1.2em;">{service_block}</p>
<p style="margin:0 0 1.2em;">You will be charged <strong>{charge_today}</strong> for your first 30 days of Hammer. This charge is non-refundable.</p>
<p style="margin:0 0 1.2em;">{_cancellation_policy_html()}</p>
<p style="margin:0 0 1.2em;">{_data_html}</p>
<p style="margin:0 0 1.2em;"><strong>Thank you for choosing Hammer!</strong></p>
<p style="margin:0 0 1.2em;">Cheers,</p>
<p style="margin:0 0 2.4em;">Hannah</p>
<p style="margin:0;">Hannah<br>hannah@hammer-corp.com</p>"""
    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#222;line-height:1.5;">
  <div style="max-width:640px;margin:0 auto;padding:16px 20px 32px;">
    {logo_header}
    {body_html}
  </div>
</body>
</html>"""


def build_hammer_drive_agreement_email_html(
    dealership: str,
    pricing: dict[str, str],
    *,
    logo_src: str | None = None,
) -> str:
    return build_agreement_email_html(dealership, pricing, logo_src=logo_src)


def _agreement_email_subject(dealership: str, pricing: dict[str, str]) -> str:
    template = pricing.get("agreementTemplate", "hammer_drive")
    if template == "facebook_aia":
        return f"Facebook AIA agreement — {dealership}"
    if template == "marketposter":
        return f"MarketPoster agreement — {dealership}"
    if template == "hammer_connect":
        return f"Hammer Connect agreement — {dealership}"
    return f"Hammer agreement — {dealership}"


def enrich_agreement_payload(
    *,
    website: str,
    selected_plan: str | None,
    lot_size: str | None,
    payload: dict[str, str],
    dealership_name: str | None = None,
    seat_count: str | None = None,
) -> dict[str, str]:
    dealership = (dealership_name or "").strip() or dealership_display_name(website)
    payload["dealershipName"] = dealership
    payload["emailGreetingLine"] = f"Hello {dealership},"
    payload["emailSalutation"] = dealership

    product = resolve_agreement_pricing(
        selected_plan,
        lot_size,
        website=website,
        currency=payload.get("currency") or None,
        seat_count=seat_count,
    )
    if product:
        payload.update(product)
        payload["agreementEmailSubject"] = _agreement_email_subject(dealership, product)
        payload["agreementEmailBody"] = build_agreement_email(dealership, product)
        logo_for_email = agreement_logo_url_for_email()
        html_for_zapier = build_agreement_email_html(dealership, product, logo_src=logo_for_email)
        payload["agreementEmailHtml"] = html_for_zapier
        payload["agreementEmailHtmlEmbedded"] = build_agreement_email_html(
            dealership, product, logo_src=agreement_logo_src()
        )
        payload["agreementLogoUrl"] = logo_for_email
        payload["agreementLogoEmbedded"] = "1" if agreement_logo_src().startswith("data:") else "0"
    return payload

