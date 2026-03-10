import json
import os
import queue
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from openai import OpenAI
from twilio.request_validator import RequestValidator
import uvicorn

import macro_logger
import weight

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
ENABLE_TWILIO_SIGNATURE_CHECK = os.getenv("ENABLE_TWILIO_SIGNATURE_CHECK", "false").lower() == "true"

OPENAI_MODEL_CLASSIFIER = os.getenv("OPENAI_MODEL", "gpt-5.4")
OPENAI_REASONING_EFFORT_CLASSIFIER = "low"
OPENAI_VERBOSITY_CLASSIFIER = "low"

if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in environment.")

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

# Queue 1: raw incoming Twilio messages
message_queue: "queue.Queue[dict]" = queue.Queue()
# Queue 2: classified tasks { "task_type": str, "item": dict }
task_queue: "queue.Queue[dict]" = queue.Queue()

TASK_MACRO = "macronutrient_logging"
TASK_WEIGHT = "weight_body_fat_logging"

USERS_FILE = Path(__file__).with_name("users.json")


def _normalize_phone(phone: str) -> str:
    """Digits only. US 10-digit numbers get leading 1 so +15093069737 and 5093069737 match."""
    digits = "".join(c for c in str(phone).strip() if c.isdigit())
    if not digits:
        return ""
    if len(digits) == 10 and digits[0] in "23456789":
        digits = "1" + digits
    return digits


def _load_users() -> dict:
    """Load users from users.json. Returns {} if missing or invalid."""
    if not USERS_FILE.exists():
        return {}
    try:
        with USERS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[WARN] Could not load {USERS_FILE}: {e}", flush=True)
        return {}


def _extract_sheet_id(val: str) -> str:
    """If val is a Google Sheets URL, return the spreadsheet ID; otherwise return val as-is."""
    if not val or not isinstance(val, str):
        return val or ""
    s = val.strip()
    if "/spreadsheets/d/" in s:
        start = s.find("/spreadsheets/d/") + len("/spreadsheets/d/")
        end = s.find("/", start)
        if end == -1:
            end = len(s)
        return s[start:end].split("?")[0]
    return s


def _lookup_user(users: dict, from_number: str) -> Optional[dict]:
    """Return user dict (name, google_sheet_id) or None. Match by normalized digits."""
    if not users:
        return None
    digits = _normalize_phone(from_number)
    if not digits:
        return None
    for key, val in users.items():
        if not isinstance(val, dict):
            continue
        if _normalize_phone(key) == digits:
            out = {**val}
            if "google_sheet_id" in out:
                out["google_sheet_id"] = _extract_sheet_id(out["google_sheet_id"])
            return out
    return None


def _extract_text_from_response(response) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text.strip()
    parts = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def classify_task_type(raw_message: str) -> str:
    """
    Call OpenAI to classify the message as either macronutrient_logging or weight_body_fat_logging.
    Returns one of TASK_MACRO, TASK_WEIGHT.
    """
    developer_prompt = (
        "You classify the user's message into exactly one of two task types. "
        "Reply with only the task type string, nothing else.\n"
        "- macronutrient_logging: logging food, calories, protein, meals, what they ate.\n"
        "- weight_body_fat_logging: logging body weight, scale weight, body fat percentage, weigh-in."
    )
    user_prompt = (
        f"Message: {raw_message}\n\n"
        "What task type? Reply with exactly: macronutrient_logging or weight_body_fat_logging"
    )

    response = client.responses.create(
        model=OPENAI_MODEL_CLASSIFIER,
        reasoning={"effort": OPENAI_REASONING_EFFORT_CLASSIFIER},
        text={"verbosity": OPENAI_VERBOSITY_CLASSIFIER},
        input=[
            {"role": "developer", "content": developer_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = _extract_text_from_response(response)
    raw_content = (content or "").strip()
    print(f"[CLASSIFIER] Raw API response: {raw_content!r}")
    if not raw_content:
        return TASK_MACRO
    content_lower = raw_content.lower()
    if TASK_WEIGHT in content_lower or "weight" in content_lower or "body_fat" in content_lower:
        return TASK_WEIGHT
    return TASK_MACRO


def classifier_worker() -> None:
    """Dequeue from message_queue, classify task type, push to task_queue."""
    while True:
        item = message_queue.get()
        try:
            raw = item.get("raw_message", "")
            task_type = classify_task_type(raw)
            print(f"[CLASSIFIER] SID={item.get('message_sid')} -> {task_type}")
            task_queue.put({"task_type": task_type, "item": item})
        except Exception as e:
            print(f"[CLASSIFIER ERROR] SID={item.get('message_sid')}: {e}, defaulting to macronutrient_logging")
            task_queue.put({"task_type": TASK_MACRO, "item": item})
        finally:
            message_queue.task_done()


def processor_worker() -> None:
    """Dequeue from task_queue and call the appropriate logger (macro_logger or weight)."""
    while True:
        task = task_queue.get()
        try:
            task_type = task.get("task_type", TASK_MACRO)
            item = task["item"]
            print(f"[PROCESSOR] Running {task_type} for SID={item.get('message_sid')}")
            if task_type == TASK_WEIGHT:
                weight.process_message(item)
            else:
                macro_logger.process_message(item)
        except Exception as e:
            print(
                f"[PROCESSOR ERROR] SID={task.get('item', {}).get('message_sid')}: {e}",
                file=sys.stderr,
            )
            traceback.print_exc()
        finally:
            task_queue.task_done()


def verify_twilio_signature(
    request: Request,
    form_data: dict,
    x_twilio_signature: Optional[str],
) -> None:
    if not ENABLE_TWILIO_SIGNATURE_CHECK:
        return
    if not TWILIO_AUTH_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="TWILIO_AUTH_TOKEN missing for signature validation.",
        )
    if not x_twilio_signature:
        raise HTTPException(status_code=403, detail="Missing X-Twilio-Signature header.")
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    if not validator.validate(str(request.url), form_data, x_twilio_signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature.")


@app.on_event("startup")
def startup_event() -> None:
    macro_logger.ensure_csv_exists()
    t1 = threading.Thread(target=classifier_worker, daemon=True)
    t2 = threading.Thread(target=processor_worker, daemon=True)
    t1.start()
    t2.start()
    print(
        f"[STARTUP] Classifier + processor workers started. "
        f"Classifier model: {OPENAI_MODEL_CLASSIFIER} | reasoning: {OPENAI_REASONING_EFFORT_CLASSIFIER}",
        flush=True,
    )
    print(
        "[STARTUP] Twilio webhook should POST to: http://YOUR_HOST/sms "
        "(e.g. ngrok URL + /sms)",
        flush=True,
    )


@app.get("/", response_class=PlainTextResponse)
def healthcheck() -> str:
    return "Server is running."


@app.post("/sms", response_class=PlainTextResponse)
async def receive_sms(
    request: Request,
    Body: str = Form(...),
    From: str = Form(...),
    To: str = Form(...),
    MessageSid: str = Form(...),
    x_twilio_signature: Optional[str] = Header(default=None, alias="X-Twilio-Signature"),
) -> str:
    print(f"[RECEIVED] SMS request from {From!r} Body={Body!r}", flush=True)
    form_data = {"Body": Body, "From": From, "To": To, "MessageSid": MessageSid}
    verify_twilio_signature(request, form_data, x_twilio_signature)

    from_stripped = (From or "").strip()
    users = _load_users()
    if users:
        user = _lookup_user(users, from_stripped)
        if not user or "google_sheet_id" not in user:
            norm = _normalize_phone(from_stripped)
            print(
                f"[RECEIVED] Unknown number From={From!r} (normalized={norm!r}), "
                "not in users.json — skipping",
                flush=True,
            )
            print(
                "[RECEIVED] users.json keys (normalized): "
                f"{[_normalize_phone(k) for k in users if isinstance(users.get(k), dict)]}",
                flush=True,
            )
            return "OK"
        item = {
            "received_at_utc": datetime.now(timezone.utc).isoformat(),
            "from_number": from_stripped,
            "to_number": To,
            "message_sid": MessageSid,
            "raw_message": Body.strip(),
            "user": {
                "name": user.get("name", ""),
                "google_sheet_id": user["google_sheet_id"],
            },
        }
    else:
        item = {
            "received_at_utc": datetime.now(timezone.utc).isoformat(),
            "from_number": from_stripped,
            "to_number": To,
            "message_sid": MessageSid,
            "raw_message": Body.strip(),
        }

    message_queue.put(item)
    print(f"[RECEIVED] Queued SID={MessageSid} from {From}: {Body!r}", flush=True)
    return "OK"


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)

