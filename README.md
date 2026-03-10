### Sierra Mind and Body – SMS Macro & Weight Logger

This is a minimal, GitHub-ready version of the SMS logging app. It receives SMS messages from Twilio, classifies them as either:

- **macronutrient logging** (calories + protein), or  
- **weight/body fat logging**,  

and then writes the structured data to a Google Sheet (and to a local CSV for macros).

No secrets or personal data are included; you must provide your own keys and IDs via a `.env` file and your own `users.json`.

---

### Project structure

- `server.py` – FastAPI app + Twilio webhook, OpenAI classifier, worker queues.
- `macro_logger.py` – High-reasoning macro logger (calories/protein), writes:
  - CSV: `data.csv`
  - Google Sheets: dynamic metric columns on the `Calories` tab, paired odd/even rows per date.
- `weight.py` – Fast, low-token weight/body-fat logger, writes:
  - Weight to column `F`
  - Body fat % to column `G`
  - On the same `Calories` tab as macros.
- `staples.json` – Default “staple foods” database for macro estimates.
- `staples/` – Optional per-user staples folder (see below).
- `requirements.txt` – Python dependencies.
- `.env.example` – Example environment configuration (no real keys).
- `users.example.json` – Example multi-user mapping, for reference.
- `start_server.bat` – Convenience script to activate a local `venv` and run `server.py`.
- `start_ngrok.bat` – Convenience script to expose port `8000` via `ngrok`.

You can upload this folder as-is to GitHub; just **do not commit your actual `.env`, service account JSON, or real users.json**.

---

### Requirements

- **Python** 3.11+ recommended
- A **virtual environment** (recommended): `python -m venv venv`
- A **Twilio** account with:
  - A phone number
  - A webhook configured to point to your public URL + `/sms`
- An **OpenAI** API key
- A **Google Cloud** service account with:
  - The Sheets API enabled
  - A JSON key file
  - Access to your target Google Sheet (shared with the service account email)
- An **ngrok** account (or other tunneling solution) for local development

Install Python dependencies from the `Sierra-mind-and-body` directory:

```bash
pip install -r requirements.txt
```

---

### Environment configuration (`.env`)

1. Copy `.env.example` to `.env` in the same folder:

```bash
cp .env.example .env
```

2. Edit `.env` and fill in your real values:

- **OpenAI / Twilio**
  - `OPENAI_API_KEY=...`
  - `TWILIO_AUTH_TOKEN=...`
- **OpenAI model defaults**
  - `OPENAI_MODEL=gpt-5.4` (or any compatible model)
  - `OPENAI_REASONING_EFFORT=xhigh`
  - `OPENAI_VERBOSITY=low`
- **Server**
  - `HOST=0.0.0.0`
  - `PORT=8000`
  - `ENABLE_TWILIO_SIGNATURE_CHECK=false` (set to `true` in production)
- **Google Sheets**
  - `GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json`  
    (relative path to the JSON key you place in this folder)
  - `GOOGLE_SHEET_ID=...`  
    (spreadsheet ID from your Sheet URL)
  - `GOOGLE_SHEET_TAB=Calories`
  - `ENABLE_GOOGLE_SHEETS=true`
  - `APP_TIMEZONE=America/Los_Angeles`
- **CSV**
  - `CSV_PATH=data.csv`

**Important:** `.env` and `service-account.json` should **never** be committed to GitHub.

---

### Google Sheet layout & row logic

The app assumes a sheet/tab like:

- Column **A**: date strings (e.g. `3/10/2026`).
- Data rows are in **odd rows only** (3, 5, 7, …).  
  Each date uses two rows:
  - Row N (odd): date + data
  - Row N+1 (even): paired macros row

**Macro logger (`macro_logger.py`):**

- For a given date:
  - If the date already exists on an odd row in column A, it reuses that row.
  - If not, it:
    - Finds the latest existing dated odd row.
    - Writes the new date on the next odd row (e.g. last row 41 → new row 43).
  - It never writes earlier than **row 3**, even on an empty sheet.
- Macros are written into the **first empty metric column** starting at column `K` (11):
  - Row N: calories
  - Row N+1: protein

**Weight logger (`weight.py`):**

- Uses the **same date row** logic as macros:
  - Existing date → reuse its odd row.
  - No date yet → new odd row after the last dated odd row, never before row 3.
- Writes:
  - Weight to column **F**
  - Body fat % to column **G**

---

### Multi-user configuration (`users.json`)

`server.py` supports multiple users via a `users.json` file that you create (not committed).

Use `users.example.json` as a template and place your real `users.json` in the same folder as `server.py`:

```json
{
  "+15555550123": {
    "name": "Example User One",
    "google_sheet_id": "your-google-sheet-id-here"
  }
}
```

Details:

- Keys are phone numbers (string). The server **normalizes** numbers (digits only, 10-digit US gets a leading `1`), so:
  - `+15555550123`, `15555550123`, and `5555550123` all match the same user.
- `google_sheet_id` can be:
  - Just the raw spreadsheet ID, or
  - A full Google Sheets URL; the code extracts the ID automatically.
- If `users.json` is missing or empty, the app falls back to **single-user mode**, using `GOOGLE_SHEET_ID` from `.env`.

---

### Staples (macro estimation helpers)

`macro_logger.py` can use staple foods to stabilize estimates:

- **Default:** `staples.json` (included).
- **Per-user (optional):**
  - Put JSON files into `staples/` named with the digits of the user’s phone number:
    - e.g. `15555550123.json`
  - Structure is the same as `staples.json`.
  - If a per-user file exists, it is used; otherwise `staples.json` is used.

You can edit `staples.json` to reflect your default foods; no personal identifiers are stored there.

---

### Running locally

From inside the `Sierra-mind-and-body` folder:

1. **Create and activate a venv** (if you don’t already have one at this path):

```bash
python -m venv venv
venv\Scripts\activate
```

2. **Install dependencies**:

```bash
pip install -r requirements.txt
```

3. **Add config files (not committed)**:

- Copy `.env.example` → `.env` and fill in real values.
- Create `users.json` based on `users.example.json`.
- Download your service account JSON to `service-account.json` (or match the path in `.env`).

4. **Run the server**:

On Windows, you can use:

```bash
start_server.bat
```

This script:

- `cd`s to the project folder
- Activates `venv\Scripts\activate.bat`
- Runs `python server.py` (FastAPI app with uvicorn)

5. **Expose the server to Twilio with ngrok**:

In another terminal:

```bash
start_ngrok.bat
```

This exposes `http://localhost:8000` at a public URL like `https://abcd1234.ngrok.io`.

6. **Configure Twilio webhook:**

- In your Twilio Console, for the phone number you’re using:
  - Set the **Messaging webhook URL** to:
    - `https://YOUR_NGROK_ID.ngrok.io/sms`
  - HTTP method: `POST`

7. **Test:**

- Send:
  - A food log SMS (e.g. `"medium cookie and a protein shake"`) → should log calories/protein.
  - A weight log SMS (e.g. `"180.5 lbs, 14.2% body fat"`) → should log weight/body fat to columns F/G.

Logs will appear in the terminal (`[RECEIVED]`, `[CLASSIFIER]`, `[PROCESSOR]`, `[MACRO]`, `[WEIGHT]`).

---

### Files to keep private (do not commit)

When pushing to GitHub, **do not commit**:

- `.env`
- `service-account.json` (or any Google Cloud credentials)
- `users.json` (real phone numbers + sheet IDs)
- Any other files containing secrets, tokens, or personal information.

You can safely commit:

- `server.py`
- `macro_logger.py`
- `weight.py`
- `requirements.txt`
- `staples.json` and `staples/README.md`
- `start_server.bat`, `start_ngrok.bat`
- `.env.example`
- `users.example.json`

