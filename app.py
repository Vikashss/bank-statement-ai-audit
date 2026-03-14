import re
import os
import base64
from io import BytesIO
from datetime import datetime

import pandas as pd
import pdfplumber
import streamlit as st

# OCR imports (optional fallback)
try:
    import pytesseract
    from PIL import ImageOps, ImageFilter
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

# AI model imports
try:
    from transformers import pipeline
    AI_AVAILABLE = True
except Exception:
    AI_AVAILABLE = False

st.set_page_config(page_title="Bank Statement Reader", page_icon="📄", layout="wide")

# -------------------- Constants --------------------
AG_IMAGE_PATH = "assets/AG_Audit.jpg"
USAGE_LOG_FILE = "data/app_usage_log.xlsx"
ADMIN_PASSWORD = "Audit@123"   # change this password

DATE_START_RE = re.compile(r'^\s*(\d{2}-\d{2}-\d{4})\b')
DATE_ANY_RE = re.compile(r'(\d{2}-\d{2}-\d{4})')
BAL_RE = re.compile(r'(\d+(?:,\d{3})*\.\d{2}(?:Cr|Dr))')
REF_CODE_RE = re.compile(r'\b[A-Z]{4}\d{6,}\b')

SKIP_TEXT = [
    "JAMMU AND KASHMIR BANK LTD",
    "MOVING SECRETARIAT",
    "MOVING SECRETRAIT",
    "CIVIL SECRETARIAT",
    "CIVIL SECRETRAIT",
    "IFSC Code",
    "MICR Code",
    "PHONE Code",
    "TYPE:",
    "A/C NO:",
    "Printed By",
    "STATEMENT OF ACCOUNT",
    "Transaction Details Page",
    "Date Stamp Manager",
    "Unless the constituent",
    "immediately of any discrepancy found",
    "by him in this statement of Account",
    "it will be taken that he has found",
    "the account correct",
    "Interest Rate",
    "No Nomination",
    "No Nomination Available",
    "cKYC Id",
    "TO:",
    "OPP GURUDWARA",
    "CHANNI RAMA JAMMU",
    "JAMMU,JAMMU AND KASHMIR",
    "180001",
    "https://",
    "http://",
    "Grand Total:",
    "Funds in clearing:",
    "Total available Amount:",
    "Effective Available Amount:",
    "Effective Available Amount",
    "FFD Contribution:",
    "FFD Contribution",
    "Page Total:",
    "Printed By ****END OF STATEMENT****",
    "END OF STATEMENT",
]

STOP_WORDS = [
    "Grand Total:",
    "Funds in clearing:",
    "Total available Amount:",
    "Effective Available Amount:",
    "Effective Available Amount",
    "FFD Contribution:",
    "FFD Contribution",
    "Page Total:",
    "Printed By ****END OF STATEMENT****",
    "END OF STATEMENT",
]

TRANSFER_WORDS = ["NEFT", "IMPS", "UPI", "RTGS", "TRF", "TRANSFER"]

DISPLAY_COLUMNS = [
    "Date",
    "Description",
    "IFSC / Ref No",
    "Parsed Amount",
    "Debit",
    "Credit",
    "Closing Balance",
    "Correction Flag",
    "Correction Note",
    "AI Entity Type",
    "AI Confidence",
    "AI Risk Score",
    "AI Risk Level",
    "AI Risk Reason",
]

# -------------------- Utility Functions --------------------
def clean(text):
    return " ".join(str(text).split()) if text is not None else ""


def should_skip(line):
    line = clean(line)
    if not line:
        return True
    return any(x in line for x in SKIP_TEXT)


def balance_to_float(balance_text):
    balance_text = clean(balance_text)
    if not balance_text:
        return None

    sign = -1 if balance_text.endswith("Dr") else 1
    num = balance_text.replace("Cr", "").replace("Dr", "").replace(",", "").strip()

    try:
        return sign * float(num)
    except Exception:
        return None


def amount_to_float(amount_text):
    amount_text = clean(amount_text).replace(",", "")
    try:
        return float(amount_text)
    except Exception:
        return None


def fmt_amount(x):
    if x is None:
        return "0"
    return f"{float(x):.2f}"


def split_description_and_ref(text):
    text = clean(text)
    if not text:
        return "", ""

    ref_match = REF_CODE_RE.search(text)
    ref_code = ref_match.group(0) if ref_match else ""

    if ref_code:
        desc = re.sub(r'\b' + re.escape(ref_code) + r'\b', '', text).strip()
        desc = clean(desc)
        return desc, ref_code

    return text, ""


def cut_footer_text(block):
    block = clean(block)
    for word in STOP_WORDS:
        pos = block.find(word)
        if pos != -1:
            block = block[:pos].strip()
    return block


def score_page_text(text):
    if not text:
        return 0
    lines = [clean(x) for x in text.split("\n") if clean(x)]
    date_starts = sum(1 for x in lines if DATE_START_RE.match(x))
    balances = len(BAL_RE.findall(text))
    dates_any = len(DATE_ANY_RE.findall(text))
    return (date_starts * 10) + (balances * 4) + dates_any + len(lines) * 0.05


# -------------------- OCR Functions --------------------
def preprocess_ocr_image(pil_img):
    img = pil_img.convert("L")
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def ocr_extract_page_text(page):
    if not OCR_AVAILABLE:
        return ""
    try:
        page_img = page.to_image(resolution=300).original
        page_img = preprocess_ocr_image(page_img)
        text = pytesseract.image_to_string(page_img, config="--psm 6")
        return text or ""
    except Exception:
        return ""


def get_best_page_text(page):
    extracted_text = page.extract_text() or ""
    extracted_score = score_page_text(extracted_text)

    if extracted_score >= 12:
        return extracted_text, False

    ocr_text = ocr_extract_page_text(page)
    ocr_score = score_page_text(ocr_text)

    if ocr_score > extracted_score:
        return ocr_text, True

    return extracted_text, False


# -------------------- Parser Functions --------------------
def parse_transaction_block(block):
    block = cut_footer_text(block)
    if not block:
        return None

    m_date = DATE_ANY_RE.search(block)
    if not m_date:
        return None
    date = m_date.group(1)

    balances = BAL_RE.findall(block)
    if not balances:
        return None
    closing_balance = balances[-1]

    bal_pos = block.rfind(closing_balance)
    usable = block[:bal_pos + len(closing_balance)].strip()
    usable = usable.replace(date, "", 1).strip()

    pattern = (
        r'^(.*)\s'
        r'(\d+(?:,\d{3})*\.\d{2})\s'
        r'(' + re.escape(closing_balance) + r')$'
    )

    m = re.search(pattern, usable)
    if not m:
        return None

    left_text = m.group(1).strip()
    txn_amount = m.group(2).strip()
    description, ref_code = split_description_and_ref(left_text)

    return {
        "date": date,
        "description": description,
        "ref_code": ref_code,
        "amount": txn_amount,
        "closing_balance": closing_balance,
    }


def build_transaction_blocks(file_obj):
    blocks = []
    current_block = ""
    ocr_used_pages = 0

    file_obj.seek(0)
    with pdfplumber.open(file_obj) as pdf:
        for page in pdf.pages:
            text, used_ocr = get_best_page_text(page)

            if used_ocr:
                ocr_used_pages += 1

            if not text:
                continue

            for raw_line in text.split("\n"):
                line = clean(raw_line)
                if not line:
                    continue

                if should_skip(line):
                    continue

                if DATE_START_RE.match(line):
                    if current_block:
                        blocks.append(current_block.strip())
                    current_block = line
                else:
                    if current_block:
                        current_block += " " + line

    if current_block:
        blocks.append(current_block.strip())

    return blocks, ocr_used_pages


def process_pdf(file_obj, opening_balance=None):
    blocks, ocr_used_pages = build_transaction_blocks(file_obj)

    parsed_rows = []
    failed_blocks = []

    for block in blocks:
        row = parse_transaction_block(block)
        if row:
            parsed_rows.append(row)
        else:
            failed_blocks.append(block)

    final_rows = []
    prev_balance = opening_balance

    for row in parsed_rows:
        date = row["date"]
        description = row["description"]
        ref_code = row["ref_code"]
        parsed_amount = row["amount"]
        closing_balance = row["closing_balance"]

        curr_balance = balance_to_float(closing_balance)
        parsed_amt_float = amount_to_float(parsed_amount)

        debit = "0"
        credit = "0"
        final_amount = parsed_amt_float
        correction_flag = "No"
        correction_note = ""

        text_check = (description + " " + ref_code).upper()

        if prev_balance is None or curr_balance is None:
            final_amount = parsed_amt_float
            debit = fmt_amount(final_amount) if final_amount is not None else "0"
            credit = "0"
            correction_note = "Opening balance / previous balance unavailable"
        else:
            delta = round(curr_balance - prev_balance, 2)
            abs_delta = round(abs(delta), 2)

            if parsed_amt_float is None or round(parsed_amt_float, 2) != abs_delta:
                final_amount = abs_delta
                correction_flag = "Yes"
                correction_note = f"Parsed amount replaced by balance difference {abs_delta:.2f}"
            else:
                final_amount = parsed_amt_float

            if delta < 0:
                debit = fmt_amount(final_amount)
                credit = "0"
            elif delta > 0:
                debit = "0"
                credit = fmt_amount(final_amount)
            else:
                reversal_words = [
                    "REV", "REVERSED", "RETURN", "RETURNED",
                    "INVALID", "FROM:", "B/F", "ACC CLOSED",
                ]
                if any(word in text_check for word in reversal_words):
                    debit = "0"
                    credit = fmt_amount(final_amount) if final_amount is not None else "0"
                    correction_note = correction_note or "Same balance row classified as credit by keyword"
                else:
                    debit = fmt_amount(final_amount) if final_amount is not None else "0"
                    credit = "0"
                    correction_note = correction_note or "Same balance row classified as debit by default"

        final_rows.append([
            date,
            description,
            ref_code,
            parsed_amount,
            debit,
            credit,
            closing_balance,
            correction_flag,
            correction_note,
        ])

        prev_balance = balance_to_float(closing_balance)

    df = pd.DataFrame(final_rows, columns=[
        "Date",
        "Description",
        "IFSC / Ref No",
        "Parsed Amount",
        "Debit",
        "Credit",
        "Closing Balance",
        "Correction Flag",
        "Correction Note",
    ])

    if not df.empty:
        df["Debit_num"] = pd.to_numeric(df["Debit"], errors="coerce").fillna(0.0)
        df["Credit_num"] = pd.to_numeric(df["Credit"], errors="coerce").fillna(0.0)
    else:
        df["Debit_num"] = pd.Series(dtype=float)
        df["Credit_num"] = pd.Series(dtype=float)

    return df, failed_blocks, len(blocks), ocr_used_pages


@st.cache_data(show_spinner=False)
def process_pdf_cached(file_bytes, opening_balance):
    file_obj = BytesIO(file_bytes)
    return process_pdf(file_obj, opening_balance=opening_balance)


# -------------------- AI Functions --------------------
@st.cache_resource(show_spinner=False)
def load_zero_shot_model():
    if not AI_AVAILABLE:
        return None
    try:
        classifier = pipeline(
            "zero-shot-classification",
            model="valhalla/distilbart-mnli-12-1"
        )
        return classifier
    except Exception:
        return None


def lightweight_preclassify(text):
    text = clean(text).upper()

    if not text:
        return {"label": "UNKNOWN", "score": 0.0}

    if any(word in text for word in [
        "BANK CHARGE", "BANK CHARGES", "GST", "SMS CHARGE",
        "INTEREST", "SERVICE CHARGE", "ATM CHARGE", "ANNUAL CHARGE"
    ]):
        return {"label": "BANK_INTERNAL", "score": 0.99}

    if any(word in text for word in [
        "GOVT", "GOVERNMENT", "TREASURY", "SECRETARIAT"
    ]):
        return {"label": "GOVERNMENT", "score": 0.95}

    return None


def classify_narration_ai(text, classifier):
    text = clean(text)

    pre = lightweight_preclassify(text)
    if pre:
        return pre

    if classifier is None:
        return {"label": "UNKNOWN", "score": 0.0}

    candidate_labels = [
        "individual person",
        "private company or business",
        "government office",
        "bank internal transaction",
        "unknown entity"
    ]

    try:
        result = classifier(text, candidate_labels)
        label_map = {
            "individual person": "INDIVIDUAL",
            "private company or business": "PRIVATE_COMPANY",
            "government office": "GOVERNMENT",
            "bank internal transaction": "BANK_INTERNAL",
            "unknown entity": "UNKNOWN",
        }

        return {
            "label": label_map.get(result["labels"][0], "UNKNOWN"),
            "score": float(result["scores"][0])
        }
    except Exception:
        return {"label": "UNKNOWN", "score": 0.0}


@st.cache_data(show_spinner=False)
def classify_unique_descriptions(descriptions_tuple):
    classifier = load_zero_shot_model()
    result_map = {}

    for desc in descriptions_tuple:
        result_map[desc] = classify_narration_ai(desc, classifier)

    return result_map


def ai_risk_decision(description, debit, credit, ai_result):
    text = clean(description).upper()

    entity = ai_result["label"]
    confidence = ai_result["score"]
    amount = max(float(debit or 0), float(credit or 0))

    score = 0
    reasons = []

    if entity == "INDIVIDUAL":
        score += 35
        reasons.append("Individual detected")
    elif entity == "PRIVATE_COMPANY":
        score += 30
        reasons.append("Private company detected")
    elif entity == "UNKNOWN":
        score += 15
        reasons.append("Unknown entity")

    if any(x in text for x in TRANSFER_WORDS):
        score += 20
        reasons.append("Electronic transfer")

    if amount >= 50000:
        score += 10
        reasons.append("Amount > 50k")

    if amount >= 200000:
        score += 15
        reasons.append("Amount > 2L")

    if 0 < confidence < 0.45:
        score += 10
        reasons.append("Low AI confidence")

    if score >= 75:
        level = "Very High"
    elif score >= 50:
        level = "High"
    elif score >= 30:
        level = "Medium"
    else:
        level = "Low"

    return {
        "entity_type": entity,
        "confidence": round(confidence, 4),
        "risk_score": score,
        "risk_level": level,
        "risk_reason": "; ".join(reasons)
    }


def detect_high_risk_ai(df):
    if df.empty:
        return df.copy(), df.copy(), df.copy()

    result_df = df.copy()

    unique_desc = tuple(
        result_df["Description"].fillna("").astype(str).map(clean).unique().tolist()
    )

    classification_map = classify_unique_descriptions(unique_desc)

    ai_results = result_df.apply(
        lambda row: ai_risk_decision(
            row["Description"],
            row["Debit_num"],
            row["Credit_num"],
            classification_map.get(clean(row["Description"]), {"label": "UNKNOWN", "score": 0.0})
        ),
        axis=1
    )

    result_df["AI Entity Type"] = ai_results.apply(lambda x: x["entity_type"])
    result_df["AI Confidence"] = ai_results.apply(lambda x: x["confidence"])
    result_df["AI Risk Score"] = ai_results.apply(lambda x: x["risk_score"])
    result_df["AI Risk Level"] = ai_results.apply(lambda x: x["risk_level"])
    result_df["AI Risk Reason"] = ai_results.apply(lambda x: x["risk_reason"])

    high_debit = result_df[
        (result_df["Debit_num"] > 0) &
        (result_df["AI Risk Level"].isin(["High", "Very High"]))
    ].copy()

    high_credit = result_df[
        (result_df["Credit_num"] > 0) &
        (result_df["AI Risk Level"].isin(["High", "Very High"]))
    ].copy()

    return result_df, high_debit, high_credit


# -------------------- Excel Export --------------------
def to_excel_bytes(df, sheet_name="Statement"):
    output = BytesIO()
    export_df = df.drop(columns=["Debit_num", "Credit_num"], errors="ignore")

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name=sheet_name)
        ws = writer.sheets[sheet_name]

        for idx, column_name in enumerate(export_df.columns, start=1):
            max_len = max(
                len(str(column_name)),
                *(len(str(v)) for v in export_df[column_name].fillna(""))
            )
            col_letter = ws.cell(row=1, column=idx).column_letter
            ws.column_dimensions[col_letter].width = min(max(max_len + 2, 12), 55)

    output.seek(0)
    return output


# -------------------- Usage Log --------------------
def log_user_usage_to_excel(
    name,
    email,
    section_field_party,
    file_name,
    total_rows,
    corrected_rows,
    failed_blocks,
    ocr_used_pages
):
    os.makedirs(os.path.dirname(USAGE_LOG_FILE), exist_ok=True)

    log_row = {
        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Name": name,
        "Email": email,
        "Section / Field Party No.": section_field_party,
        "Uploaded File": file_name,
        "Total Rows": total_rows,
        "Corrected Rows": corrected_rows,
        "Failed Blocks": failed_blocks,
        "OCR Pages Used": ocr_used_pages,
    }

    new_df = pd.DataFrame([log_row])

    if os.path.exists(USAGE_LOG_FILE):
        try:
            existing_df = pd.read_excel(USAGE_LOG_FILE)
            updated_df = pd.concat([existing_df, new_df], ignore_index=True)
        except Exception:
            updated_df = new_df
    else:
        updated_df = new_df

    with pd.ExcelWriter(USAGE_LOG_FILE, engine="openpyxl") as writer:
        updated_df.to_excel(writer, index=False, sheet_name="Usage Log")
        ws = writer.sheets["Usage Log"]

        for idx, column_name in enumerate(updated_df.columns, start=1):
            max_len = max(
                len(str(column_name)),
                *(len(str(v)) for v in updated_df[column_name].fillna(""))
            )
            col_letter = ws.cell(row=1, column=idx).column_letter
            ws.column_dimensions[col_letter].width = min(max(max_len + 2, 15), 40)


# -------------------- Theme-Friendly Style --------------------
st.markdown(
    """
    <style>
    :root {
        --card-bg: color-mix(in srgb, var(--background-color, #ffffff) 88%, white 12%);
        --soft-border: color-mix(in srgb, var(--text-color, #111827) 14%, transparent);
        --muted-text: color-mix(in srgb, var(--text-color, #111827) 72%, transparent);
        --banner-bg: linear-gradient(
            90deg,
            rgba(155, 0, 34, 0.14),
            rgba(155, 0, 34, 0.08)
        );
        --banner-border: rgba(155, 0, 34, 0.30);
        --sidebar-bg: linear-gradient(
            180deg,
            rgba(155, 0, 34, 0.08),
            rgba(15, 23, 42, 0.04)
        );
        --shadow: 0 4px 14px rgba(0, 0, 0, 0.06);
        --accent: #9b0022;
        --accent-hover: #7f001c;
    }

    .stApp {
        background: transparent;
        color: inherit;
    }

    .block-container {
        padding-top: 0.35rem;
        padding-bottom: 2rem;
        max-width: 1200px;
    }

    section[data-testid="stSidebar"] {
        background: var(--sidebar-bg);
        border-right: 1px solid var(--soft-border);
    }

    section[data-testid="stSidebar"] * {
        color: inherit !important;
    }

    .office-banner {
        background: var(--banner-bg);
        color: inherit;
        text-align: center;
        font-size: 18px;
        font-weight: 700;
        padding: 10px 18px;
        border-radius: 10px;
        margin-top: 0px;
        margin-bottom: 16px;
        border: 1px solid var(--banner-border);
        box-shadow: var(--shadow);
        backdrop-filter: blur(4px);
    }

    .top-divider {
        border: none;
        border-top: 1px solid var(--soft-border);
        margin: 8px 0 28px 0;
    }

    .main-title-wrap {
        text-align: center;
        margin-bottom: 22px;
    }

    .main-title {
        font-size: 3rem;
        font-weight: 750;
        color: inherit;
        margin-bottom: 8px;
        line-height: 1.2;
    }

    .main-subtitle {
        font-size: 1.12rem;
        color: var(--muted-text);
        margin-bottom: 4px;
        font-weight: 600;
    }

    .main-subtitle2 {
        font-size: 1rem;
        color: var(--muted-text);
        margin-bottom: 0;
    }

    .access-box {
        background: var(--card-bg);
        border: 1px solid var(--soft-border);
        border-radius: 14px;
        padding: 18px;
        margin-bottom: 18px;
        box-shadow: var(--shadow);
        backdrop-filter: blur(6px);
    }

    div[data-testid="stMetric"] {
        background: var(--card-bg);
        border: 1px solid var(--soft-border);
        border-radius: 12px;
        padding: 12px;
        box-shadow: var(--shadow);
    }

    .stTextInput input {
        background-color: color-mix(in srgb, var(--background-color, #ffffff) 92%, white 8%) !important;
        color: inherit !important;
        border: 1px solid var(--soft-border) !important;
        border-radius: 10px !important;
    }

    .stTextInput input::placeholder {
        color: var(--muted-text) !important;
        opacity: 0.85 !important;
    }

    section[data-testid="stFileUploader"] {
        background: var(--card-bg);
        border: 1px solid var(--soft-border);
        border-radius: 14px;
        padding: 8px;
        box-shadow: var(--shadow);
    }

    button[data-baseweb="tab"] {
        color: inherit !important;
        font-weight: 600 !important;
    }

    .stDownloadButton button,
    .stButton button {
        background-color: var(--accent) !important;
        color: #ffffff !important;
        border: none !important;
        border-radius: 10px !important;
        font-weight: 600 !important;
    }

    .stDownloadButton button:hover,
    .stButton button:hover {
        background-color: var(--accent-hover) !important;
        color: #ffffff !important;
    }

    .creator-footer {
        text-align: center;
        font-size: 15px;
        margin-top: 42px;
        padding-top: 18px;
        border-top: 1px solid var(--soft-border);
        color: var(--muted-text);
        font-weight: 500;
    }

    section[data-testid="stSidebar"] .stMarkdown p,
    section[data-testid="stSidebar"] .stMarkdown li,
    section[data-testid="stSidebar"] label {
        line-height: 1.55;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# -------------------- Header --------------------
st.markdown(
    """
    <div class="office-banner">
        Office of the Accountant General (Audit), Jammu &amp; Kashmir
    </div>

    <hr class="top-divider">

    <div class="main-title-wrap">
        <div class="main-title">📄 Bank Statement Reader</div>
        <div class="main-subtitle">Office Use Only</div>
        <div class="main-subtitle2">
            Supported Format: Jammu &amp; Kashmir Bank Statement PDF Only
        </div>
    </div>
    """,
    unsafe_allow_html=True
)

# -------------------- Sidebar --------------------
with st.sidebar:
    try:
        with open(AG_IMAGE_PATH, "rb") as img_file:
            encoded_img = base64.b64encode(img_file.read()).decode()

        st.markdown(
            f"""
            <div style="text-align:center; margin-top:10px; margin-bottom:18px;">
                <img src="data:image/jpg;base64,{encoded_img}"
                     width="200"
                     style="border-radius:14px; box-shadow:0 4px 16px rgba(0,0,0,0.15);">
            </div>
            """,
            unsafe_allow_html=True
        )
    except Exception:
        st.info("Sidebar logo not found.")

    st.header("About")
    st.write(
        "This utility is designed for internal audit use, including parsing, "
        "validation, correction review, and AI-assisted risk-based transaction analysis."
    )

    st.markdown("**Steps**")
    st.write("1. Fill user access details")
    st.write("2. Upload PDF")
    st.write("3. Enter Opening Balance manually")
    st.write("4. Click Run Analysis")
    st.write("5. Review totals and AI risk flags")
    st.write("6. Download Excel")

    if not OCR_AVAILABLE:
        st.warning("OCR fallback is not available in this environment.")

    if not AI_AVAILABLE:
        st.warning("Transformers package not available. AI model detection will use fallback mode.")

    st.divider()
    st.subheader("Admin Access")
    admin_password = st.text_input("Admin Password", type="password")

    if admin_password == ADMIN_PASSWORD:
        st.success("Admin access granted")
        if os.path.exists(USAGE_LOG_FILE):
            with open(USAGE_LOG_FILE, "rb") as f:
                st.download_button(
                    label="Download Usage Log",
                    data=f,
                    file_name="app_usage_log.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )
    elif admin_password:
        st.error("Invalid admin password")

# -------------------- User Access --------------------
st.markdown('<div class="access-box">', unsafe_allow_html=True)
st.markdown("### User Access Details")

u1, u2, u3 = st.columns(3)

with u1:
    user_name = st.text_input("Your Name *", placeholder="Enter your full name")

with u2:
    user_email = st.text_input("Official Email ID *", placeholder="Enter official email")

with u3:
    user_section = st.text_input("Section / Field Party No. *", placeholder="Enter section or field party no.")

st.caption("These details are mandatory and recorded for internal monitoring and audit support.")
st.markdown('</div>', unsafe_allow_html=True)

if not (user_name.strip() and user_email.strip() and user_section.strip()):
    st.warning("Please fill Name, Email ID and Section / Field Party No. to use this app.")
    st.stop()

# -------------------- Inputs --------------------
uploaded_file = st.file_uploader("Upload statement PDF", type=["pdf"])

opening_balance_input = st.text_input(
    "Enter Opening Balance manually (example: 90817476.00Cr or 1250.00Dr)",
    value=""
)

opening_balance = balance_to_float(opening_balance_input) if opening_balance_input.strip() else None

if opening_balance_input.strip() and opening_balance is None:
    st.error("Invalid opening balance format. Use format like 90817476.00Cr or 1250.00Dr")
    st.stop()

run_analysis = st.button("Run Analysis", use_container_width=True)

# -------------------- Main --------------------
if uploaded_file is None:
    st.info("Please upload a Jammu & Kashmir Bank statement PDF to begin analysis.")
elif not run_analysis:
    st.info("Upload the file and click Run Analysis.")
else:
    try:
        with st.spinner("Processing PDF and running AI risk analysis..."):
            file_bytes = uploaded_file.getvalue()

            df, failed_blocks, total_blocks, ocr_used_pages = process_pdf_cached(
                file_bytes,
                opening_balance
            )

            df, high_debit, high_credit = detect_high_risk_ai(df)

        if df.empty:
            st.error("No transactions could be parsed from the uploaded PDF.")
        else:
            total_rows = len(df)
            total_debit = float(df["Debit_num"].sum())
            total_credit = float(df["Credit_num"].sum())
            corrected_rows = int((df["Correction Flag"] == "Yes").sum())
            failed_count = len(failed_blocks)
            ai_high_rows = int(df["AI Risk Level"].isin(["High", "Very High"]).sum())

            log_key = f"{user_email}_{uploaded_file.name}"
            if "last_logged_key" not in st.session_state:
                st.session_state["last_logged_key"] = ""

            if st.session_state["last_logged_key"] != log_key:
                log_user_usage_to_excel(
                    name=user_name,
                    email=user_email,
                    section_field_party=user_section,
                    file_name=uploaded_file.name,
                    total_rows=total_rows,
                    corrected_rows=corrected_rows,
                    failed_blocks=failed_count,
                    ocr_used_pages=ocr_used_pages
                )
                st.session_state["last_logged_key"] = log_key

            st.subheader("Statement Overview")
            m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
            m1.metric("Rows", total_rows)
            m2.metric("Total Debit", f"{total_debit:,.2f}")
            m3.metric("Total Credit", f"{total_credit:,.2f}")
            m4.metric("Corrected Rows", corrected_rows)
            m5.metric("Failed Blocks", failed_count)
            m6.metric("OCR Pages Used", ocr_used_pages)
            m7.metric("AI High Risk Rows", ai_high_rows)

            tab1, tab2, tab3, tab4 = st.tabs([
                "Parsed Data",
                "AI High Risk Rows",
                "Corrected Rows",
                "Failed Blocks"
            ])

            with tab1:
                st.dataframe(df[DISPLAY_COLUMNS], use_container_width=True, height=520)

            with tab2:
                risk_df = df[df["AI Risk Level"].isin(["High", "Very High"])][DISPLAY_COLUMNS]
                if risk_df.empty:
                    st.success("No AI high-risk rows.")
                else:
                    st.dataframe(risk_df, use_container_width=True, height=420)

            with tab3:
                corrected_df = df[df["Correction Flag"] == "Yes"][DISPLAY_COLUMNS]
                if corrected_df.empty:
                    st.success("No corrected rows.")
                else:
                    st.dataframe(corrected_df, use_container_width=True, height=420)

            with tab4:
                if not failed_blocks:
                    st.success("No failed blocks.")
                else:
                    st.warning(
                        f"Parsed {total_rows} rows from {total_blocks} detected blocks. "
                        f"{failed_count} block(s) could not be parsed."
                    )
                    for idx, block in enumerate(failed_blocks, start=1):
                        st.text_area(f"Failed Block {idx}", block, height=120)

            excel_data = to_excel_bytes(df, sheet_name="Statement")
            st.download_button(
                label="Download Full Excel",
                data=excel_data,
                file_name="statement_output.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

            st.divider()
            st.subheader("AI High Risk Analysis")

            d_rows = len(high_debit)
            d_total = float(high_debit["Debit_num"].sum()) if not high_debit.empty else 0.0
            c_rows = len(high_credit)
            c_total = float(high_credit["Credit_num"].sum()) if not high_credit.empty else 0.0

            h1, h2, h3, h4 = st.columns(4)
            h1.metric("High Risk Debit Rows", d_rows)
            h2.metric("High Risk Debit Amount", f"{d_total:,.2f}")
            h3.metric("High Risk Credit Rows", c_rows)
            h4.metric("High Risk Credit Amount", f"{c_total:,.2f}")

            col1, col2 = st.columns(2)

            with col1:
                st.markdown("### High Risk Debit Entries")
                if not high_debit.empty:
                    st.dataframe(high_debit[DISPLAY_COLUMNS], use_container_width=True, height=380)
                    excel_high_debit = to_excel_bytes(high_debit, sheet_name="High Risk Debit")
                    st.download_button(
                        "Download High Risk Debit Excel",
                        data=excel_high_debit,
                        file_name="high_risk_debit.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
                else:
                    st.info("No high risk debit entries found.")

            with col2:
                st.markdown("### High Risk Credit Entries")
                if not high_credit.empty:
                    st.dataframe(high_credit[DISPLAY_COLUMNS], use_container_width=True, height=380)
                    excel_high_credit = to_excel_bytes(high_credit, sheet_name="High Risk Credit")
                    st.download_button(
                        "Download High Risk Credit Excel",
                        data=excel_high_credit,
                        file_name="high_risk_credit.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
                else:
                    st.info("No high risk credit entries found.")

    except Exception as e:
        st.error(f"Error while processing PDF: {e}")

# -------------------- Footer --------------------
st.markdown(
    """
    <div class="creator-footer">
        Internal utility for bank statement review and AI-assisted audit analysis.
    </div>
    """,
    unsafe_allow_html=True
)
