"""
Macro logger: parses calories and protein from a text message via OpenAI,
writes to CSV and Google Sheets. Uses high-reasoning model and optional web search.
"""
import csv
import json
import os
import threading
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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4")
OPENAI_REASONING_EFFORT = "xhigh"
OPENAI_VERBOSITY = "low"
ENABLE_WEB_SEARCH = True

ENABLE_GOOGLE_SHEETS = os.getenv("ENABLE_GOOGLE_SHEETS", "true").lower() == "true"
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_TAB = os.getenv("GOOGLE_SHEET_TAB", "Calories")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "America/Los_Angeles")
CSV_PATH = os.getenv("CSV_PATH", "data.csv")

_client: Optional[OpenAI] = None
_sheets_service = None
csv_lock = threading.Lock()
gsheets_lock = threading.Lock()

CSV_HEADERS = [
    "received_at_utc",
    "from_number",
    "to_number",
    "message_sid",
    "raw_message",
    "calories",
    "protein",
    "notes",
]


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


def _column_number_to_letters(n: int) -> str:
    letters = []
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters.append(chr(65 + remainder))
    return "".join(reversed(letters))


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


def _first_open_metric_column(values: list[list], date_row: int) -> str:
    row_values = values[date_row - 1] if len(values) >= date_row else []
    col_num = 11
    while True:
        idx = col_num - 1
        existing = row_values[idx] if idx < len(row_values) else ""
        if _normalize_cell_value(existing) == "":
            return _column_number_to_letters(col_num)
        col_num += 1


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


def _staples_path_for_phone(phone: Optional[str]) -> Path:
    """Per-user staples: staples/<digits>.json if it exists, else staples.json."""
    base = Path(__file__).parent
    default = base / "staples.json"
    if not phone:
        return default
    digits = "".join(c for c in str(phone) if c.isdigit())
    if not digits:
        return default
    per_user = base / "staples" / f"{digits}.json"
    return per_user if per_user.exists() else default


def extract_macros(raw_message: str, staples_path: Optional[Path] = None) -> dict:
    def load_staples(path: Path) -> dict:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}

    def format_staples_for_prompt(staples: dict) -> str:
        if not staples:
            return "No staple foods are currently defined."
        lines = []
        for staple_name, staple_data in staples.items():
            if not isinstance(staple_data, dict):
                continue
            serving = staple_data.get("serving", "1 serving")
            calories = staple_data.get("calories")
            protein = staple_data.get("protein")
            aliases = staple_data.get("aliases", [])
            if calories is None or protein is None:
                continue
            alias_text = (
                f" Aliases: {', '.join(str(a) for a in aliases)}."
                if isinstance(aliases, list) and aliases
                else ""
            )
            lines.append(
                f"- {staple_name}: {calories} calories, {protein} g protein per {serving}."
                f"{alias_text}"
            )
        return "\n".join(lines) if lines else "No valid staple foods are currently defined."

    path = staples_path if staples_path is not None else Path(__file__).parent / "staples.json"
    staples = load_staples(path)
    staples_text = format_staples_for_prompt(staples)

    developer_prompt = (
        "You estimate TOTAL calories and TOTAL protein for a food log message. "
        "Return only valid JSON with keys calories, protein, and notes. "
        "Use the highest-reasoning approach available when macronutrients are not explicitely provided. "
        "When quantities are missing or vague, estimate on the high side for calories and protein rather than the low side—unless the user indicates a typical, average, or medium size (see below). "
        "If the user describes the item as a typical, average, medium, or normal size/portion (e.g. 'medium cookie', 'average portion', 'normal serving') - including other simular adjectives, estimate for that typical size rather than erring on the high side; use an average-expected, representative estimate. "
        "For restaurant dishes, assume realistic restaurant portions, sauces, oils, marinades, and condiments are included unless the user clearly says otherwise. "
        "However, respect portion-limiting language such as 'small', 'half', 'split', 'handful', 'light drizzle', 'most but not all', or explicit fractions. In those cases estimate on the high side within that portion, not as a full or large portion. "
        "When the user reports eating only part of a dish (half, two thirds, most, etc.), estimate the full dish first and then apply the fraction. "
        "If a specific restaurant or brand is mentioned, search the web and prefer official nutrition sources or the restaurant website when available. "
        "If no official source exists, use the best reasonable estimate and say that in notes. "
        "Do not ask follow-up questions. Do not include markdown fences.\n\n"
        "Staple foods reference:\n"
        f"{staples_text}\n\n"
        "Rules for staple foods:\n"
        "- If the message clearly refers to one of the staple foods above and is not describing a premade or resturant style dish, and the user does not give a specific macro breakdown, use the staple food's listed macros as the default basis.\n"
        "- If a staple has aliases listed, treat those aliases as equivalent to the staple.\n"
        "- Scale the staple macros proportionally when the user gives a quantity, weight, or number of servings.\n"
        "- If the user gives exact calories or protein directly, use the user's numbers instead of the staple defaults.\n"
        "- If a restaurant, branded, or otherwise more specific item is mentioned and it conflicts with a staple food default, prefer the more specific source.\n"
        "- If the food is not clearly one of the staple foods, follow the normal estimation process."
    )

    user_prompt = (
        f"Food log message: {raw_message}\n\n"
        "Output format:\n"
        '{"calories": number|null, "protein": number|null, "notes": string}\n\n'
        "Requirements:\n"
        "- Give one total calories number and one total protein number for the entire message.\n"
        "- If the user gives exact macros directly, use those.\n"
        "- If the user gives foods and amounts, reason through them.\n"
        "- If the message is restaurant food and a specific restaurant is mentioned, use web search.\n"
        "- If the message matches a staple food, use the staple reference unless the user provided more specific macros.\n"
        "- If the message is vague, estimate calories and protein on the high side, unless the user says typical/average/medium/normal size—then use a moderate estimate.\n"
        "- Keep notes concise: 1 to 3 sentences.\n"
        "- Return JSON only."
    )

    tools = [{"type": "web_search"}] if ENABLE_WEB_SEARCH else []
    client = _get_client()
    response = client.responses.create(
        model=OPENAI_MODEL,
        reasoning={"effort": OPENAI_REASONING_EFFORT},
        text={"verbosity": OPENAI_VERBOSITY},
        tools=tools,
        input=[
            {"role": "developer", "content": developer_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    content = _extract_text_from_response(response)
    if not content:
        raise ValueError("Model returned empty response")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"Model did not return valid JSON: {content}")
        parsed = json.loads(content[start : end + 1])

    calories = parsed.get("calories")
    protein = parsed.get("protein")
    notes = parsed.get("notes", "")
    if calories is not None:
        calories = float(calories)
    if protein is not None:
        protein = float(protein)
    return {"calories": calories, "protein": protein, "notes": notes}


def validate_numbers(calories: Optional[float], protein: Optional[float]) -> bool:
    if calories is None or protein is None:
        return False
    if calories < 0 or calories > 10000 or protein < 0 or protein > 1000:
        return False
    return True


def ensure_csv_exists() -> None:
    path = Path(CSV_PATH)
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()
        return
    try:
        with path.open("r", newline="", encoding="utf-8") as f:
            existing_headers = next(csv.reader(f), None)
    except Exception:
        existing_headers = None
    if existing_headers != CSV_HEADERS:
        backup_name = (
            f"{path.stem}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{path.suffix}"
        )
        path.replace(path.with_name(backup_name))
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()


def append_row(row: dict) -> None:
    import time

    last_error = None
    for _ in range(5):
        try:
            with csv_lock:
                with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
                    csv.DictWriter(f, fieldnames=CSV_HEADERS).writerow(row)
            return
        except PermissionError as e:
            last_error = e
            time.sleep(1)
    raise last_error or PermissionError(f"Permission denied writing to {CSV_PATH}")


def append_to_google_sheets(
    calories: float,
    protein: float,
    received_at_utc: str,
    sheet_id: Optional[str] = None,
    sheet_tab: Optional[str] = None,
) -> None:
    if not ENABLE_GOOGLE_SHEETS:
        return
    sid = sheet_id or GOOGLE_SHEET_ID
    tab = sheet_tab or GOOGLE_SHEET_TAB
    if not sid:
        print("[MACRO] No sheet_id (user or env). Skipping Google Sheets write.", flush=True)
        return
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
        updates = []
        if date_row is None:
            last_date_row = _latest_dated_odd_row(values)
            date_row = 3 if last_date_row is None else last_date_row + 2
            updates.append({"range": f"{tab}!A{date_row}", "values": [[target_date]]})
        date_row = max(3, date_row)

        col_letter = _first_open_metric_column(values, date_row)
        updates.append(
            {
                "range": f"{tab}!{col_letter}{date_row}:{col_letter}{date_row + 1}",
                "values": [[calories], [protein]],
            }
        )
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sid,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()
        print(
            f"[GOOGLE] Wrote calories/protein to "
            f"{tab}!{col_letter}{date_row}:{col_letter}{date_row + 1} for date {target_date}"
        )


def process_message(item: dict) -> None:
    """Process one message: extract macros, validate, append CSV and Google Sheets."""
    raw_message = item["raw_message"]
    user = item.get("user")
    sheet_id = user.get("google_sheet_id") if user else None
    sheet_tab = None
    staples_path = _staples_path_for_phone(item.get("from_number")) if user else None
    print(f"[MACRO] Processing SID={item['message_sid']} raw={raw_message!r}")

    extracted = extract_macros(raw_message, staples_path=staples_path)
    print(f"[MACRO] Extracted for SID={item['message_sid']}: {extracted}")

    calories, protein = extracted["calories"], extracted["protein"]
    if not validate_numbers(calories, protein):
        print(
            f"[MACRO SKIP] Validation failed for SID={item['message_sid']} "
            f"message={raw_message!r} extracted={extracted}"
        )
        return

    row = {
        "received_at_utc": item["received_at_utc"],
        "from_number": item["from_number"],
        "to_number": item["to_number"],
        "message_sid": item["message_sid"],
        "raw_message": raw_message,
        "calories": calories,
        "protein": protein,
        "notes": extracted.get("notes", ""),
    }
    print(f"[MACRO] Writing row to CSV: {row}")
    append_row(row)
    append_to_google_sheets(
        calories,
        protein,
        item["received_at_utc"],
        sheet_id=sheet_id,
        sheet_tab=sheet_tab,
    )
    print(f"[MACRO OK] Appended row for SID={item['message_sid']}: {row}")

