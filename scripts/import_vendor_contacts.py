"""Merge vendor emails/phones from the Vendors.xlsx sheet into vendors.json.

Reads the "Feed Providers" sheet (NAME / FEED EMAIL / PHONE NUMBERS columns),
matches rows to knowledge_support/vendors/vendors.json by normalized name, and
fills the `emails` / `phones` fields. Existing values are overwritten only when
the sheet has data for that vendor.

Usage:
  py -3 scripts/import_vendor_contacts.py [path/to/Vendors.xlsx]

Push the merged contacts to the live Fly host afterwards (merges into the live
list by id/name — does not wholesale replace other fields):
  set ADMIN_SECRET, then: py -3 scripts/import_vendor_contacts.py --push
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

HOST = "https://hammer-support-sync.fly.dev"
REPO_ROOT = Path(__file__).resolve().parents[1]
VENDORS_JSON = REPO_ROOT / "knowledge_support" / "vendors" / "vendors.json"
DEFAULT_XLSX = Path.home() / "Downloads" / "Vendors.xlsx"
SHEET = "Feed Providers"
FIELD_ORDER = ("id", "name", "supported", "country", "integration", "status", "notes", "emails", "phones")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _name_keys(name: str) -> list[str]:
    """Match keys, most to least specific: full name, name without
    parentheticals, then the part before any separator."""
    full = _norm(name)
    stripped = _norm(re.sub(r"[(\[].*?[)\]]", " ", name))
    base = _norm(re.split(r"[(\[/—|]| - ", name)[0])
    keys: list[str] = []
    for k in (full, stripped, base):
        if len(k) >= 3 and k not in keys:
            keys.append(k)
    return keys


def _clean_cell(value) -> str:
    text = str(value).strip() if value is not None else ""
    if not text or text.lower() in ("n/a", "na", "none", "unknown", "-"):
        return ""
    # Collapse newlines / runs of whitespace so the field is one tidy line.
    text = re.sub(r"\s*\n\s*", ", ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip(" ,")


def read_sheet(xlsx_path: Path) -> dict[str, dict[str, str]]:
    import openpyxl

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb[SHEET]
    rows = ws.iter_rows(values_only=True)
    headers = [str(h or "").strip().upper() for h in next(rows)]
    col = {h: i for i, h in enumerate(headers)}
    i_name, i_email, i_phone = col["NAME"], col["FEED EMAIL"], col["PHONE NUMBERS"]

    contacts: dict[str, dict[str, str]] = {}
    owner: dict[str, str] = {}
    ambiguous: set[str] = set()
    for row in rows:
        name = str(row[i_name] or "").strip()
        if len(name) <= 2:  # skip blank and A/B/C divider rows
            continue
        emails = _clean_cell(row[i_email] if i_email < len(row) else None)
        phones = _clean_cell(row[i_phone] if i_phone < len(row) else None)
        if not emails and not phones:
            continue
        for key in _name_keys(name):
            if key in ambiguous:
                continue
            if key in owner and owner[key] != name:
                # Two different sheet rows reduce to the same key (e.g. the
                # "Dealer Socket || ..." family) — too risky to match on it.
                ambiguous.add(key)
                contacts.pop(key, None)
                continue
            owner[key] = name
            contacts.setdefault(key, {"name": name, "emails": emails, "phones": phones})
    return contacts


def merge(vendors: list[dict], contacts: dict[str, dict[str, str]]) -> tuple[list[dict], int, set[str]]:
    matched_names: set[str] = set()
    updated = 0
    out = []
    for v in vendors:
        hit = None
        for key in _name_keys(str(v.get("name") or "")):
            if key in contacts:
                hit = contacts[key]
                break
        if hit:
            matched_names.add(hit["name"])
            changed = False
            if hit["emails"] and hit["emails"] != (v.get("emails") or ""):
                v["emails"] = hit["emails"]
                changed = True
            if hit["phones"] and hit["phones"] != (v.get("phones") or ""):
                v["phones"] = hit["phones"]
                changed = True
            if changed:
                updated += 1
        ordered = {k: v.get(k, "") for k in FIELD_ORDER}
        ordered.update({k: v[k] for k in v if k not in FIELD_ORDER})
        out.append(ordered)
    return out, updated, matched_names


def push_live(contacts: dict[str, dict[str, str]]) -> None:
    secret = os.environ["ADMIN_SECRET"]

    def api(method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            HOST + path,
            data=data,
            method=method,
            headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"},
        )
        return json.load(urllib.request.urlopen(req, timeout=60))

    live = api("GET", "/api/admin/support/vendors")["vendors"]
    merged, updated, _ = merge(live, contacts)
    res = api("PUT", "/api/admin/support/vendors/bulk", {"vendors": merged})
    print(f"live host: {updated} vendors updated -> {res}")


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--push"]
    push = "--push" in sys.argv[1:]
    xlsx = Path(args[0]) if args else DEFAULT_XLSX

    contacts = read_sheet(xlsx)
    sheet_names = {c["name"] for c in contacts.values()}
    print(f"sheet: {len(sheet_names)} vendors with contact info")

    data = json.loads(VENDORS_JSON.read_text(encoding="utf-8"))
    merged, updated, matched_names = merge(data["vendors"], contacts)
    VENDORS_JSON.write_text(
        json.dumps({"vendors": merged}, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"repo: {updated} of {len(merged)} vendors updated -> {VENDORS_JSON}")

    unmatched = sorted(sheet_names - matched_names, key=str.lower)
    if unmatched:
        print(f"\n{len(unmatched)} sheet rows had contact info but no matching vendor:")
        for name in unmatched:
            print(f"  - {name}")

    if push:
        push_live(contacts)


if __name__ == "__main__":
    main()
