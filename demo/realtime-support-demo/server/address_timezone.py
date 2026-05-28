"""Infer Hammer Office timezone from a US/Canada business address."""

from __future__ import annotations

import re

# Primary IANA zone per US state / DC (dealer rooftops — single zone per state).
_US_STATE_IANA: dict[str, str] = {
    "AL": "America/Chicago",
    "AK": "America/Anchorage",
    "AZ": "America/Phoenix",
    "AR": "America/Chicago",
    "CA": "America/Los_Angeles",
    "CO": "America/Denver",
    "CT": "America/New_York",
    "DE": "America/New_York",
    "DC": "America/New_York",
    "FL": "America/New_York",
    "GA": "America/New_York",
    "HI": "Pacific/Honolulu",
    "ID": "America/Denver",
    "IL": "America/Chicago",
    "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago",
    "KS": "America/Chicago",
    "KY": "America/New_York",
    "LA": "America/Chicago",
    "ME": "America/New_York",
    "MD": "America/New_York",
    "MA": "America/New_York",
    "MI": "America/Detroit",
    "MN": "America/Chicago",
    "MS": "America/Chicago",
    "MO": "America/Chicago",
    "MT": "America/Denver",
    "NE": "America/Chicago",
    "NV": "America/Los_Angeles",
    "NH": "America/New_York",
    "NJ": "America/New_York",
    "NM": "America/Denver",
    "NY": "America/New_York",
    "NC": "America/New_York",
    "ND": "America/Chicago",
    "OH": "America/New_York",
    "OK": "America/Chicago",
    "OR": "America/Los_Angeles",
    "PA": "America/New_York",
    "RI": "America/New_York",
    "SC": "America/New_York",
    "SD": "America/Chicago",
    "TN": "America/Chicago",
    "TX": "America/Chicago",
    "UT": "America/Denver",
    "VT": "America/New_York",
    "VA": "America/New_York",
    "WA": "America/Los_Angeles",
    "WV": "America/New_York",
    "WI": "America/Chicago",
    "WY": "America/Denver",
}

_CA_PROVINCE_IANA: dict[str, str] = {
    "AB": "America/Edmonton",
    "BC": "America/Vancouver",
    "MB": "America/Winnipeg",
    "NB": "America/Moncton",
    "NL": "America/St_Johns",
    "NS": "America/Halifax",
    "NT": "America/Yellowknife",
    "NU": "America/Iqaluit",
    "ON": "America/Toronto",
    "PE": "America/Halifax",
    "QC": "America/Toronto",
    "SK": "America/Regina",
    "YT": "America/Whitehorse",
}

_STATE_NAME_TO_CODE: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "alberta": "AB",
    "british columbia": "BC",
    "manitoba": "MB",
    "new brunswick": "NB",
    "newfoundland and labrador": "NL",
    "newfoundland": "NL",
    "nova scotia": "NS",
    "northwest territories": "NT",
    "nunavut": "NU",
    "ontario": "ON",
    "prince edward island": "PE",
    "quebec": "QC",
    "québec": "QC",
    "saskatchewan": "SK",
    "yukon": "YT",
}

# Rails ActiveSupport labels used on Hammer Office (subset).
_IANA_TO_HAMMER_LABEL: dict[str, str] = {
    "America/Los_Angeles": "Pacific Time (US & Canada)",
    "America/Vancouver": "Pacific Time (US & Canada)",
    "America/Whitehorse": "Pacific Time (US & Canada)",
    "America/Denver": "Mountain Time (US & Canada)",
    "America/Edmonton": "Mountain Time (US & Canada)",
    "America/Phoenix": "Arizona",
    "America/Chicago": "Central Time (US & Canada)",
    "America/Winnipeg": "Central Time (US & Canada)",
    "America/Regina": "Saskatchewan",
    "America/New_York": "Eastern Time (US & Canada)",
    "America/Toronto": "Eastern Time (US & Canada)",
    "America/Detroit": "Eastern Time (US & Canada)",
    "America/Indiana/Indianapolis": "Eastern Time (US & Canada)",
    "America/Halifax": "Atlantic Time (Canada)",
    "America/Moncton": "Atlantic Time (Canada)",
    "America/St_Johns": "Newfoundland",
    "America/Anchorage": "Alaska",
    "Pacific/Honolulu": "Hawaii",
    "America/Yellowknife": "Mountain Time (US & Canada)",
    "America/Iqaluit": "Eastern Time (US & Canada)",
}

_DEFAULT_IANA = "America/Chicago"


def region_code_from_address(address: str) -> str | None:
    """Two-letter US state or Canadian province/territory code, if parseable."""
    return _region_code_from_address(address)


def _region_code_from_address(address: str) -> str | None:
    text = " ".join(address.strip().split())
    if not text:
        return None

    # Canadian: ..., ON A1A 1A1
    ca = re.search(
        r",\s*([A-Za-z]{2})\s+([A-Za-z]\d[A-Za-z])\s*(\d[A-Za-z]\d)\s*$",
        text,
        re.IGNORECASE,
    )
    if ca:
        return ca.group(1).upper()

    # US: ..., ST 12345 or ..., ST 12345-6789
    us = re.search(r",\s*([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?\s*$", text, re.IGNORECASE)
    if us:
        return us.group(1).upper()

    # Trailing two-letter code: ", TX" or " TX"
    tail = re.search(r",\s*([A-Za-z]{2})\s*$", text, re.IGNORECASE)
    if tail:
        code = tail.group(1).upper()
        if code in _US_STATE_IANA or code in _CA_PROVINCE_IANA:
            return code

    # Full state / province name before optional postal
    lower = text.lower()
    for name, code in sorted(_STATE_NAME_TO_CODE.items(), key=lambda x: -len(x[0])):
        if re.search(rf",\s*{re.escape(name)}\b", lower):
            return code
        if re.search(rf"\b{re.escape(name)}\s*,?\s*(\d{{5}}|[A-Za-z]\d[A-Za-z])", lower):
            return code

    return None


def country_from_address(address: str) -> str | None:
    """Return ``US``, ``CA``, or ``None`` when the address is ambiguous."""
    text = " ".join(address.strip().split())
    if not text:
        return None
    lower = text.lower()
    if re.search(r"\b(canada|canadian)\b", lower):
        return "CA"
    if re.search(r"\b(united states|u\.?s\.?a?\.?)\b", lower):
        return "US"

    code = _region_code_from_address(address)
    if not code:
        return None
    if code in _US_STATE_IANA:
        return "US"
    if code in _CA_PROVINCE_IANA:
        return "CA"
    return None


def infer_billing_currency_from_address(address: str) -> str | None:
    """``USD`` or ``CAD`` when the address clearly indicates country."""
    country = country_from_address(address)
    if country == "US":
        return "USD"
    if country == "CA":
        return "CAD"
    return None


def is_quebec_address(address: str) -> bool:
    code = _region_code_from_address(address)
    if code == "QC":
        return True
    lower = address.lower()
    return bool(re.search(r"\b(quebec|québec)\b", lower))


def address_billing_context(address: str) -> dict[str, str | bool | None]:
    """
    Voice-agent hints after a dealership address is confirmed.

    Keys: country (US|CA|None), region_code, currency (USD|CAD|None),
    is_quebec, tax_field (none|gst_hst|qst), tax_prompt (short instruction).
    """
    region = _region_code_from_address(address)
    country = country_from_address(address)
    currency = infer_billing_currency_from_address(address)
    quebec = is_quebec_address(address)

    tax_field = "none"
    tax_prompt = ""
    if country == "US":
        tax_prompt = "US rooftop — do not ask GST/HST or QST; currency is USD"
    elif country == "CA":
        if quebec:
            tax_field = "qst"
            tax_prompt = "Canadian rooftop (Quebec) — ask QST number only; currency is CAD"
        else:
            tax_field = "gst_hst"
            tax_prompt = "Canadian rooftop — ask GST/HST number; currency is CAD"
    else:
        tax_prompt = (
            "Could not tell US vs Canada from address — confirm country, then set currency"
        )

    return {
        "country": country,
        "region_code": region,
        "currency": currency,
        "is_quebec": quebec,
        "tax_field": tax_field,
        "tax_prompt": tax_prompt,
    }


def iana_timezone_from_address(address: str) -> str:
    code = _region_code_from_address(address)
    if not code:
        return _DEFAULT_IANA
    if code in _US_STATE_IANA:
        return _US_STATE_IANA[code]
    if code in _CA_PROVINCE_IANA:
        return _CA_PROVINCE_IANA[code]
    return _DEFAULT_IANA


def hammer_timezone_label(iana: str) -> str:
    return _IANA_TO_HAMMER_LABEL.get(iana, _IANA_TO_HAMMER_LABEL[_DEFAULT_IANA])


def infer_hammer_timezone(address: str, *, form_options: list[str] | None = None) -> str:
    """Return a value suitable for Hammer Office account[timezone] select."""
    iana = iana_timezone_from_address(address)
    label = hammer_timezone_label(iana)
    if not form_options:
        return label

    lowered_label = label.lower()
    for opt in form_options:
        opt_l = opt.lower()
        if iana.lower() in opt_l:
            return opt
        if lowered_label in opt_l:
            return opt

    # Match core region name inside "(GMT-06:00) Central Time (US & Canada)"
    core = label.split("(")[0].strip().lower()
    for opt in form_options:
        if core and core in opt.lower():
            return opt

    return form_options[0] if form_options else label
