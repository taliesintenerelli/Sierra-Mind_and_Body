"""
Weight logger: parses weight and/or body fat % from a text message via OpenAI (fast, low-token),
writes to Google Sheets in columns F (weight) and G (body fat %).
"""
import json
import os
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from openai import OpenAI

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL_WEIGHT = os.getenv("OPENAI_MODEL", "gpt-5.4")
OPENAI_REASONING_EFFORT_WEIGHT = "low"
OPENAI_VERBOSITY_WEIGHT = "low"

ENABLE_GOOGLE_SHEETS = os.getenv("ENABLE_GOOGLE_SHEETS", "true").lower() == "true"
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_TAB = os.getenv("GOOGLE_SHEET_TAB", "Calories")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "America/Los_Angeles")

WEIGHT_COLUMN = "F"
BODY_FAT_COLUMN = "G"

_client: Optional[OpenAI] = None
_sheets_service = None
gsheets_lock = threading.Lock()


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("Missing OPENAI_API_KEY in environment.")
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def _get_sheets_service():
    global _sheets_service
    if _sheets_service is None:
        if not GOOGLE_SERVICE_ACCOUNT_FILE:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_FILE missing")
        credentials = Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        _sheets_service = build("sheets", "v4", credentials=credentials)
    return _sheets_service


def _sheet_date_string(dt_utc_iso: str) -> str:
    dt_utc = datetime.fromisoformat(dt_utc_iso)
    local_dt = dt_utc.astimezone(ZoneInfo(APP_TIMEZONE))
    return (
        local_dt.strftime("%-m/%-d/%Y")
        if os.name != "nt"
        else local_dt.strftime("%#m/%#d/%Y")
    )


def _normalize_cell_value(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _find_date_row(values: list[list], target_date: str) -> Optional[int]:
    for idx, row in enumerate(values, start=1):
        if idx % 2 == 1 and row:
            if _normalize_cell_value(row[0]) == target_date:
                return idx
    return None


def _try_parse_sheet_date(value: str):
    value = value.strip()
    formats = ["%m/%d/%Y", "%-m/%-d/%Y", "%m/%d/%y"]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    parts = value.split("/")
    if len(parts) == 3:
        try:
            month, day, year = int(parts[0]), int(parts[1]), int(parts[2])
            return datetime(year, month, day).date()
        except ValueError:
            return None
    return None


def _latest_dated_odd_row(values: list[list]) -> Optional[int]:
    latest_row = None
    latest_dt = None
    for i, row in enumerate(values, start=1):
        if i % 2 == 0 or not row:
            continue
        cell = str(row[0]).strip()
        if not cell:
            continue
        dt = _try_parse_sheet_date(cell)
        if dt is None:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt
            latest_row = i
    return latest_row


def _extract_text_from_response(response) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text
    parts = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def extract_weight_and_body_fat(raw_message: str) -> dict:
    """
    Extract weight (decimal number, e.g. lbs) and/or body_fat_percentage from message.
    Returns {"weight": float|None, "body_fat_percentage": float|None}.
    """
    developer_prompt = (
        "You extract at most two numbers from the user's message: "
        "1) weight (in lbs or kg; if kg convert to lbs by multiplying by 2.205, or output as-is and we treat as lbs), "
        "2) body fat percentage. "
        "Return only valid JSON with keys: weight (number or null), body_fat_percentage (number or null). "
        "If the user gives only one number, set the other to null. If unclear, set to null. "
        "No explanation, no markdown. One short JSON object only."
    )
    user_prompt = (
        f"Message: {raw_message}\n\n"
        'Output format: {"weight": number|null, "body_fat_percentage": number|null}'
    )

    client = _get_client()
    response = client.responses.create(
        model=OPENAI_MODEL_WEIGHT,
        reasoning={"effort": OPENAI_REASONING_EFFORT_WEIGHT},
        text={"verbosity": OPENAI_VERBOSITY_WEIGHT},
        input=[
            {"role": "developer", "content": developer_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    content = _extract_text_from_response(response)
    if not content:
        return {"weight": None, "body_fat_percentage": None}

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {"weight": None, "body_fat_percentage": None}
        parsed = json.loads(content[start : end + 1])

    weight = parsed.get("weight")
    body_fat = parsed.get("body_fat_percentage")
    if weight is not None:
        weight = float(weight)
    if body_fat is not None:
        body_fat = float(body_fat)
    return {"weight": weight, "body_fat_percentage": body_fat}


def append_weight_to_google_sheets(
    weight_val: Optional[float],
    body_fat_percentage: Optional[float],
    received_at_utc: str,
    sheet_id: Optional[str] = None,
    sheet_tab: Optional[str] = None,
) -> None:
    if not ENABLE_GOOGLE_SHEETS:
        print("[WEIGHT] Google Sheets disabled (ENABLE_GOOGLE_SHEETS=false). Not writing.")
        return
    if weight_val is None and body_fat_percentage is None:
        return

    sid = sheet_id or GOOGLE_SHEET_ID
    tab = sheet_tab or GOOGLE_SHEET_TAB
    if not sid:
        print("[WEIGHT] No sheet_id (user or env). Skipping Google Sheets write.", flush=True)
        return

    print(f"[WEIGHT] Writing to sheet tab {tab!r}, date={_sheet_date_string(received_at_utc)}")
    service = _get_sheets_service()
    target_date = _sheet_date_string(received_at_utc)

    with gsheets_lock:
        values_resp = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=sid,
                range=f"{tab}!A:ZZ",
            )
            .execute()
        )
        values = values_resp.get("values", [])

        date_row = _find_date_row(values, target_date)
        if date_row is None:
            last_date_row = _latest_dated_odd_row(values)
            date_row = 3 if last_date_row is None else last_date_row + 2
            updates = [
                {"range": f"{tab}!A{date_row}", "values": [[target_date]]},
            ]
            print(
                f"[WEIGHT] No row for date {target_date}; using new row {date_row} "
                f"(last dated row was {last_date_row})"
            )
        else:
            updates = []
            print(f"[WEIGHT] Found existing row {date_row} for date {target_date}")
        date_row = max(3, date_row)

        row_values = []
        if weight_val is not None:
            row_values.append(
                {"range": f"{tab}!{WEIGHT_COLUMN}{date_row}", "values": [[weight_val]]}
            )
        if body_fat_percentage is not None:
            row_values.append(
                {
                    "range": f"{tab}!{BODY_FAT_COLUMN}{date_row}",
                    "values": [[body_fat_percentage]],
                }
            )
        updates.extend(row_values)

        if updates:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=sid,
                body={"valueInputOption": "USER_ENTERED", "data": updates},
            ).execute()
            print(
                f"[WEIGHT] Wrote to {tab} row {date_row} "
                f"for date {target_date}: weight={weight_val}, body_fat%={body_fat_percentage}"
            )


def process_message(item: dict) -> None:
    """Process one message: extract weight/body fat, write to Google Sheets."""
    raw_message = item["raw_message"]
    user = item.get("user")
    sheet_id = user.get("google_sheet_id") if user else None
    sheet_tab = None
    from_num = item.get("from_number", "")
    print(
        f"[WEIGHT] Processing SID={item['message_sid']} from={from_num!r} raw={raw_message!r}",
        flush=True,
    )
    if user:
        print(
            f"[WEIGHT] Using user sheet_id={sheet_id!r} "
            f"(tab={sheet_tab or GOOGLE_SHEET_TAB!r})",
            flush=True,
        )
    else:
        print("[WEIGHT] No user in item, using env GOOGLE_SHEET_ID", flush=True)

    extracted = extract_weight_and_body_fat(raw_message)
    weight = extracted.get("weight")
    body_fat = extracted.get("body_fat_percentage")
    print(
        f"[WEIGHT] Extracted for SID={item['message_sid']}: "
        f"weight={weight}, body_fat%={body_fat}"
    )

    if weight is None and body_fat is None:
        print(
            f"[WEIGHT SKIP] No weight or body fat found for "
            f"SID={item['message_sid']} (extraction returned both null)"
        )
        return

    try:
        append_weight_to_google_sheets(
            weight,
            body_fat,
            item["received_at_utc"],
            sheet_id=sheet_id,
            sheet_tab=sheet_tab,
        )
    except Exception as e:
        print(f"[WEIGHT ERROR] Failed to write to Sheets: {e}")
        traceback.print_exc()
        raise
    print(f"[WEIGHT OK] Done for SID={item['message_sid']}")

