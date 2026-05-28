"""Zapier Catch Hook payloads and agreement-approval tracking for voice / form leads."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field, model_validator

from agreement_approvals import (
    agreement_approval_status,
    normalize_email,
    record_agreement_approval,
    register_pending_agreement,
    reply_indicates_approval,
)
from hammer_agreement import dealership_display_name, enrich_agreement_payload


class LeadCaptureRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    dealership_name: str | None = Field(
        None,
        max_length=200,
        description="Store name for agreement email greeting (required for voice agreement send)",
    )
    name: str = Field(default="", max_length=120)
    phone: str = Field(default="", max_length=32)
    website: str = Field(default="", max_length=300)
    role: str = Field(default="", max_length=64)
    selected_plan: str | None = Field(None, max_length=200)
    lot_size: str | None = Field(None, max_length=64)
    seat_count: str | None = Field(
        None,
        max_length=32,
        description="MarketPoster users/seats (e.g. 3 users) — used for seat-based pricing",
    )
    preferred_callback_time: str | None = Field(
        None,
        max_length=120,
        description="Rep walkthrough time booked or requested on the voice call",
    )
    appointment_link: str | None = Field(
        None,
        max_length=500,
        description="Google Calendar event link when book_appointment succeeded",
    )
    channel: str | None = Field(None, max_length=32)
    currency: str | None = Field(None, max_length=8)
    deliver_async: bool = Field(
        default=False,
        description="When true, API returns immediately and delivers to Zapier in the background (voice latency).",
    )

    @model_validator(mode="after")
    def validate_channel_fields(self) -> LeadCaptureRequest:
        channel = lead_channel(self)
        if channel == "website":
            if not self.name.strip():
                raise ValueError("name is required for website leads")
            if len(self.phone.strip()) < 5:
                raise ValueError("phone is required for website leads")
            if len(self.website.strip()) < 4:
                raise ValueError("website is required for website leads")
            if not self.role.strip():
                raise ValueError("role is required for website leads")
            return self
        if not (self.dealership_name or "").strip():
            raise ValueError("dealership_name is required for voice agreement email")
        if not (self.selected_plan or "").strip():
            raise ValueError("selected_plan is required for voice agreement email")
        return self


class AgreementApprovalRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    approved: bool = True
    reply_text: str | None = Field(None, max_length=8000)


class AgreementPendingRegisterRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    dealership: str = Field(default="", max_length=200)
    selected_plan: str = Field(default="", max_length=200)


ROLE_LABELS: dict[str, str] = {
    "dealer-principal": "Dealer Principal",
    "general-manager": "General Manager",
    "sales-manager": "Sales Manager",
    "bdc": "BDC / Internet Manager",
    "marketing": "Marketing",
    "other": "Other",
}

def role_title(role: str) -> str:
    key = role.strip().lower().replace("_", "-")
    return ROLE_LABELS.get(key, role.strip())


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def normalize_website_for_notes(website: str) -> str:
    s = website.strip()
    for prefix in ("https://", "http://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :]
    return s.rstrip("/") or website.strip()


def normalize_phone_e164(phone: str) -> str:
    raw = phone.strip()
    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw)
        return f"+{digits}" if digits else raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if digits:
        return f"+{digits}"
    return raw


def lead_channel(body: LeadCaptureRequest) -> str:
    raw = (body.channel or "").strip().lower()
    if raw in ("voice", "website"):
        return raw
    if body.selected_plan or body.lot_size:
        return "voice"
    return "website"


def resolve_dealership_name(body: LeadCaptureRequest) -> str:
    explicit = (body.dealership_name or "").strip()
    if explicit:
        return explicit
    return dealership_display_name(body.website)


def build_zapier_payload(body: LeadCaptureRequest) -> dict[str, str]:
    channel = lead_channel(body)
    name = body.name.strip()
    first, last = split_name(name) if name else ("", "")
    role_raw = body.role.strip()
    title = role_title(role_raw) if role_raw else ""
    dealer_name = resolve_dealership_name(body)
    website_raw = body.website.strip()
    website_host = normalize_website_for_notes(website_raw) if website_raw else ""
    notes = f"Dealership: {dealer_name}"
    if title:
        notes = f"Title: {title} Dealership: {dealer_name}"
    if website_host and website_host.lower() != dealer_name.lower().replace(" ", ""):
        notes += f" Website: {website_host}"
    if body.selected_plan:
        notes += f" Plan: {body.selected_plan.strip()}"
    if body.lot_size:
        notes += f" Lot: {body.lot_size.strip()}"
    if body.seat_count:
        notes += f" Seats: {body.seat_count.strip()}"
    if body.preferred_callback_time:
        notes += f" Walkthrough: {body.preferred_callback_time.strip()}"

    event = "agreement_email_request" if channel == "voice" else "website_lead"

    payload: dict[str, str] = {
        "responseId": str(uuid4()),
        "createTime": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        "channel": channel,
        "fullName": name,
        "firstName": first or (normalize_email(body.email).split("@")[0] if "@" in body.email else ""),
        "lastName": last,
        "notes": notes,
        "email": normalize_email(body.email),
        "phoneNumber": normalize_phone_e164(body.phone) if body.phone.strip() else "",
        "website": website_raw,
        "dealership": dealer_name,
        "dealershipName": dealer_name,
        "role": role_raw,
        "roleTitle": title,
        "selectedPlan": (body.selected_plan or "").strip(),
        "lotSize": (body.lot_size or "").strip(),
        "seatCount": (body.seat_count or "").strip(),
        "leadSource": "voice signup" if channel == "voice" else "website form",
        "replyInstruction": "Reply to this email with: I approve",
    }
    if body.preferred_callback_time:
        payload["appointmentTime"] = body.preferred_callback_time.strip()
    if body.appointment_link:
        payload["appointmentLink"] = body.appointment_link.strip()
    if body.currency:
        payload["currency"] = body.currency.strip().upper()
    payload = enrich_agreement_payload(
        website=website_raw or dealer_name,
        dealership_name=dealer_name,
        selected_plan=body.selected_plan,
        lot_size=body.lot_size,
        seat_count=body.seat_count,
        payload=payload,
    )
    if payload.get("event") == "agreement_email_request" and payload.get("agreementEmailSubject"):
        register_pending_agreement(
            payload["email"],
            dealership=dealer_name,
            product_line=payload.get("productLine", ""),
            selected_plan=payload.get("selectedPlan", ""),
        )
    return payload


def zapier_voice_lead_webhook_url() -> str:
    """Voice AI signup + agreement email (Zap 1). Not used for the website lead modal."""
    return os.environ.get("ZAPIER_LEAD_WEBHOOK_URL", "").strip()


def zapier_voice_lead_webhook_hook_id() -> str:
    """Last path segment of the Catch Hook URL (e.g. 4od2z1k) — safe for /api/health verification."""
    url = zapier_voice_lead_webhook_url().rstrip("/")
    if not url or "/hooks/catch/" not in url:
        return ""
    return url.split("/")[-1]


def zapier_website_lead_webhook_url() -> str:
    """Website “Get started” form only — separate Catch Hook from voice."""
    return os.environ.get("ZAPIER_WEBSITE_LEAD_WEBHOOK_URL", "").strip()


def zapier_website_lead_webhook_hook_id() -> str:
    """Last path segment of the website Catch Hook (e.g. 4o1aob8) — safe for /api/health verification."""
    url = zapier_website_lead_webhook_url().rstrip("/")
    if not url or "/hooks/catch/" not in url:
        return ""
    return url.split("/")[-1]


def zapier_webhook_url() -> str:
    """Alias for :func:`zapier_voice_lead_webhook_url` (backward compatibility)."""
    return zapier_voice_lead_webhook_url()


def zapier_lead_webhook_url_for_channel(channel: str) -> str:
    if channel == "website":
        return zapier_website_lead_webhook_url()
    return zapier_voice_lead_webhook_url()


def lead_webhook_env_name(channel: str) -> str:
    return "ZAPIER_WEBSITE_LEAD_WEBHOOK_URL" if channel == "website" else "ZAPIER_LEAD_WEBHOOK_URL"


def lead_webhook_configured(channel: str) -> bool:
    return bool(zapier_lead_webhook_url_for_channel(channel))


def _channel_from_payload(payload: dict[str, str]) -> str:
    raw = (payload.get("channel") or "").strip().lower()
    if raw in ("voice", "website"):
        return raw
    if payload.get("event") == "website_lead":
        return "website"
    return "voice"


def approval_callback_secret() -> str:
    return os.environ.get("ZAPIER_APPROVAL_CALLBACK_SECRET", "").strip()


def _is_production_deploy() -> bool:
    if os.environ.get("REALTIME_SALES_SERVERLESS", "").strip() in ("1", "true", "yes"):
        return True
    return os.environ.get("REALTIME_SALES_PRODUCTION", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def verify_approval_secret(provided: str | None) -> bool:
    expected = approval_callback_secret()
    if not expected:
        # Local dev only — production must set ZAPIER_APPROVAL_CALLBACK_SECRET.
        return not _is_production_deploy()
    return bool(provided and provided.strip() == expected)


def post_lead_to_zapier(payload: dict[str, str]) -> None:
    channel = _channel_from_payload(payload)
    url = zapier_lead_webhook_url_for_channel(channel)
    if not url:
        raise RuntimeError(f"{lead_webhook_env_name(channel)} is not configured on the server")

    with httpx.Client(timeout=20.0) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()


def record_agreement_approval_request(body: AgreementApprovalRequest) -> dict[str, str | bool]:
    return record_agreement_approval(
        body.email,
        approved=body.approved,
        reply_text=body.reply_text,
        source="zapier",
    )
