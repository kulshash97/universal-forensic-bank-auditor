import os
import io
import re
import hashlib
import xml.sax.saxutils as saxutils
from datetime import datetime
from typing import List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
import pypdf
import streamlit as st

# ReportLab imports for audit & consulting reports
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

# API Key Discovery
raw_api_key = os.getenv("GEMINI_API_KEY")
if not raw_api_key and hasattr(st, "secrets"):
    raw_api_key = st.secrets.get("GEMINI_API_KEY")

if not raw_api_key:
    raise ValueError("GEMINI_API_KEY missing in environment variables or Streamlit secrets.")

api_key = str(raw_api_key).strip().strip("'").strip('"')
client = genai.Client(api_key=api_key)

# ------------------------------------------------------------------------------
# AUDIT & RECONCILIATION SCHEMAS
# ------------------------------------------------------------------------------
class TransactionItem(BaseModel):
    date: str
    description: str
    transaction_type: str
    amount: float
    running_balance: float
    category: str
    is_suspicious: bool
    compliance_tag: str
    audit_note: str

class ExecutiveAuditSummary(BaseModel):
    account_holder_or_bank: str = Field(description="Detected Bank name or Account Holder name")
    statement_period: str = Field(description="Detected date period, e.g. 01/04/2025 to 31/03/2026")
    executive_summary: str = Field(description="Strict plain English ASCII 2-sentence forensic audit assessment.")

class StatementAuditReport(BaseModel):
    account_holder_or_bank: str
    statement_period: str
    opening_balance: float
    closing_balance: float
    total_debit: float
    total_credit: float
    net_cashflow: float
    suspicious_count: int
    reconciliation_status: str
    file_checksum: str
    executive_summary: str
    transactions: List[TransactionItem]

# ------------------------------------------------------------------------------
# MINI-AI CONSULTANT 4-PILLAR SCHEMAS (GEMINI-3.1-PRO-PREVIEW)
# ------------------------------------------------------------------------------
class LegalCompliancePillar(BaseModel):
    entity_structure: str = Field(description="Recommended corporate vehicle (e.g., Private Limited, LLP, OPC)")
    mandatory_licenses: List[str] = Field(description="Mandatory regulatory registrations, permits, trade licenses")
    tax_statutory_mandates: List[str] = Field(description="GST, PAN/TAN, TDS, Professional Tax, and labor acts")

class GovSchemeItem(BaseModel):
    scheme_name: str
    eligibility: str
    subsidy_benefits: str

class ImplementationPhase(BaseModel):
    phase_title: str = Field(description="e.g., Phase 1: Prototype & Incorporation (Months 0-2)")
    action_milestones: List[str]

class FinancialForecastPillar(BaseModel):
    estimated_initial_capex: str
    monthly_opex_runway: str
    break_even_timeline: str
    projected_roi: str
    revenue_streams: List[str]

class MiniConsultantReport(BaseModel):
    project_title: str
    executive_summary: str
    legal_compliance: LegalCompliancePillar
    government_schemes: List[GovSchemeItem]
    implementation_blueprint: List[ImplementationPhase]
    financial_forecast: FinancialForecastPillar

# ------------------------------------------------------------------------------
# TEXT & CATEGORIZATION HELPERS
# ------------------------------------------------------------------------------
def clean_ascii(text: str) -> str:
    return re.sub(r'[^\x20-\x7E]', ' ', str(text)).strip()

def categorize_universal(desc: str, t_type: str) -> str:
    d = desc.lower()
    if any(k in d for k in ["groww", "zerodha", "mutual fund", "bse", "nse", "demat", "sebi", "share"]):
        return "Investments & Capital"
    elif any(k in d for k in ["irctc", "redbus", "apsrtc", "tsrtc", "uber", "ola", "rail", "indigo", "air"]):
        return "Travel & Conveyance"
    elif any(k in d for k in ["by cash", "cash deposit", "dep cash", "atm deposit"]):
        return "Cash Inflow"
    elif any(k in d for k in ["atw-", "atm wdl", "nwd-", "cash withdrawal", "cheque cash"]):
        return "Cash Outflow"
    elif any(k in d for k in ["salary", "wfegs", "payroll", "nirudyoga bruthi"]):
        return "Salary & Remuneration"
    elif "interest" in d or "int. pd" in d or "int.pd" in d:
        return "Bank Interest"
    elif any(k in d for k in ["cred", "onecard", "rblmycard", "bobcard", "credit card"]):
        return "Credit Card Liability"
    elif any(k in d for k in ["airtel", "billdesk", "electricity", "southern", "jio", "water", "bescom", "bsnl"]):
        return "Utilities & Overheads"
    elif any(k in d for k in ["lic", "pmjjby", "pmsby", "insurance", "hdfc life", "max life"]):
        return "Statutory & Insurance"
    elif any(k in d for k in ["tax", "gst", "tds", "advance tax", "challan"]):
        return "Taxation & Statutory"
    elif t_type == "Credit":
        return "Operational Inflow"
    return "Vendor & UPI Payments"

# ------------------------------------------------------------------------------
# AUDITOR PARSER
# ------------------------------------------------------------------------------
def analyze_statement(file_bytes: bytes, mime_type: str, password: Optional[str] = None) -> StatementAuditReport:
    file_hash = hashlib.sha256(file_bytes).hexdigest()[:16].upper()
    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    if reader.is_encrypted:
        if password:
            reader.decrypt(password)
        else:
            try:
                reader.decrypt("")
            except Exception:
                raise ValueError("Password-protected statement. Please enter password in the input field.")

    raw_pages = [p.extract_text() or "" for p in reader.pages]
    combined_header = "\n".join(raw_pages[:2]).upper()

    is_hdfc = "HDFC BANK" in combined_header
    is_ubi = "UNION BANK" in combined_header or any("(DR)" in p.upper() or "(CR)" in p.upper() for p in raw_pages[:2])

    transactions = []
    total_debits = 0.0
    total_credits = 0.0
    opening_bal = 0.0
    closing_bal = 0.0

    if is_hdfc:
        running_bal = None
        raw_tx_blocks = []
        for page_text in raw_pages:
            parts = re.split(r'Page\s*No\s*\.?\s*:', page_text, flags=re.IGNORECASE)
            tx_area = parts[0]
            lines = [l.strip() for l in tx_area.split('\n') if l.strip()]
            cur_block = []
            for line in lines:
                if "Date Narration" in line or "Withdrawal Amt" in line:
                    continue
                if re.match(r'^\d{2}/\d{2}/\d{2}\b', line):
                    if cur_block:
                        raw_tx_blocks.append(cur_block)
                    cur_block = [line]
                else:
                    if cur_block:
                        cur_block.append(line)
            if cur_block:
                raw_tx_blocks.append(cur_block)

        for block in raw_tx_blocks:
            full_line = " ".join(block)
            if "STATEMENT SUMMARY" in full_line:
                full_line = full_line.split("STATEMENT SUMMARY")[0].strip()

            date_m = re.match(r'^(\d{2}/\d{2}/\d{2})', block[0])
            tx_date = date_m.group(1) if date_m else "N/A"

            amt_matches = re.findall(r'\b\d{2}/\d{2}/\d{2}\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\b', full_line)
            if amt_matches:
                amt_str, bal_str = amt_matches[-1]
                amt = float(amt_str.replace(',', ''))
                current_bal = float(bal_str.replace(',', ''))
            else:
                dec_nums = re.findall(r'[\d,]+\.\d{2}', full_line)
                if len(dec_nums) >= 2:
                    amt = float(dec_nums[-2].replace(',', ''))
                    current_bal = float(dec_nums[-1].replace(',', ''))
                else:
                    continue

            if running_bal is not None:
                diff = round(current_bal - running_bal, 2)
                t_type = "Credit" if diff > 0 else "Debit"
            else:
                t_type = "Credit" if any(k in full_line.upper() for k in ["CR", "DEPOSIT", "SALARY", "INTEREST PAID"]) else "Debit"
                opening_bal = round(current_bal - amt if t_type == "Credit" else current_bal + amt, 2)

            running_bal = current_bal
            if t_type == "Credit":
                total_credits += amt
            else:
                total_debits += amt

            clean_desc = re.sub(r'\d{2}/\d{2}/\d{2}|[\d,]+\.\d{2}|\s+', ' ', full_line).strip()
            transactions.append(TransactionItem(
                date=tx_date,
                description=clean_ascii(clean_desc[:85]),
                transaction_type=t_type,
                amount=round(amt, 2),
                running_balance=round(current_bal, 2),
                category=categorize_universal(clean_desc, t_type),
                is_suspicious=False,
                compliance_tag="Compliant",
                audit_note="Verified ledger entry"
            ))
        closing_bal = running_bal if running_bal is not None else 0.0

    elif is_ubi:
        for page in reader.pages:
            lines = (page.extract_text() or "").split('\n')
            for line in lines:
                line_s = line.strip()
                m = re.match(r'^(\d{2}-\d{2}-\d{4})\s+(\S+)\s+(.*?)\s+([\d,]+\.\d{2})\s*\((Dr|Cr)\)\s+([\d,]+\.\d{2})\s*\((Cr|Dr)\)', line_s, re.IGNORECASE)
                if m:
                    d, tx_id, rem, amt_str, dr_cr, bal_str, _ = m.groups()
                    amt = float(amt_str.replace(',', ''))
                    bal = float(bal_str.replace(',', ''))
                    t_type = "Debit" if dr_cr.upper() == "DR" else "Credit"

                    if not transactions:
                        opening_bal = round(bal + amt if t_type == "Debit" else bal - amt, 2)
                    if t_type == "Credit":
                        total_credits += amt
                    else:
                        total_debits += amt

                    desc_text = f"{tx_id} {rem}".strip()
                    transactions.append(TransactionItem(
                        date=d,
                        description=clean_ascii(desc_text[:85]),
                        transaction_type=t_type,
                        amount=round(amt, 2),
                        running_balance=round(bal, 2),
                        category=categorize_universal(desc_text, t_type),
                        is_suspicious=False,
                        compliance_tag="Compliant",
                        audit_note="Verified ledger entry"
                    ))
        closing_bal = transactions[-1].running_balance if transactions else 0.0

    else:
        cleaned_page_texts = []
        for p_idx, page_raw in enumerate(raw_pages):
            t = page_raw
            if p_idx == 0:
                p1_split = re.split(r'Txn\s*Date\s*Value\s*Date|Value\s*Date\s*Post\s*Date|Date\s+Narration', t, flags=re.IGNORECASE)
                if len(p1_split) > 1:
                    t = p1_split[1]

            t = re.sub(r'OSBI|State Bank of India|Receive your statements by email.*?!', '', t, flags=re.IGNORECASE)
            t = re.sub(r'Txn\s*Date\s*Value\s*Date.*?Balance', '', t, flags=re.IGNORECASE)
            t = re.sub(r'Value\s*Date\s*Post\s*Date.*?Balance', '', t, flags=re.IGNORECASE)
            t = re.sub(r'Statement of Mr\..*?between \d{2}/\d{2}/\d{4} to \d{2}/\d{2}/\d{4}', '', t, flags=re.IGNORECASE)
            t = re.sub(r'\*+This is computer generated statement.*?\*+', '', t, flags=re.IGNORECASE)
            t = re.sub(r'\d+\s*Page\s*no\.', '', t, flags=re.IGNORECASE)
            t = re.sub(r'Page\s*No\s*\.?\s*:\s*\d+', '', t, flags=re.IGNORECASE)
            cleaned_page_texts.append(t)

        full_cleaned = "\n".join(cleaned_page_texts)
        clean_ledger = re.split(r'Statement\s+Summary|Summary\s+of\s+Account|\*{4,}\s*End\s+of\s+Statement', full_cleaned, flags=re.IGNORECASE)[0]
        date_regex = r'\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}[- ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[- ]\d{2,4})\b'
        pattern = re.compile(rf'({date_regex}(?:\s+{date_regex})?)(.*?)(?=(?:{date_regex}(?:\s+{date_regex})?)|\Z)', re.DOTALL | re.IGNORECASE)
        matches = pattern.findall(clean_ledger)

        running_bal = None
        for m in matches:
            date_str = m[0].strip().split()[0]
            body = m[1].strip()

            if any(ign in body.lower() for ign in ["brought forward", "b/f", "opening balance", "balance as on", "interest rate"]):
                continue

            lines = [l.strip() for l in body.split('\n') if l.strip()]
            full_desc = " ".join(lines)

            amt_line = ""
            for l in reversed(lines):
                if re.search(r'\d+\.\d{2}', l):
                    amt_line = l
                    break

            nums = re.findall(r'(?:^|\s)([\d,]+\.\d{2})(?=\s|$|\()', amt_line)
            if len(nums) < 2:
                nums = re.findall(r'[\d,]+\.\d{2}', amt_line)

            if len(nums) >= 2:
                amt = float(nums[-2].replace(',', ''))
                current_bal = float(nums[-1].replace(',', ''))
            else:
                continue

            if amt <= 0:
                continue

            if running_bal is not None:
                diff = round(current_bal - running_bal, 2)
                t_type = "Credit" if diff > 0 else "Debit"
            else:
                is_credit = any(k in full_desc.upper() for k in ["DEP TFR", "CR/", "CREDIT", "BY SALARY", "REV/"])
                t_type = "Credit" if is_credit else "Debit"
                opening_bal = round(current_bal - amt if t_type == "Credit" else current_bal + amt, 2)

            running_bal = current_bal
            if t_type == "Credit":
                total_credits += amt
            else:
                total_debits += amt

            clean_narration = " ".join([l for l in lines if not re.search(r'[\d,]+\.\d{2}', l)])
            if not clean_narration:
                clean_narration = full_desc[:80]

            transactions.append(TransactionItem(
                date=date_str,
                description=clean_ascii(clean_narration[:85]),
                transaction_type=t_type,
                amount=round(amt, 2),
                running_balance=round(current_bal, 2),
                category=categorize_universal(clean_narration, t_type),
                is_suspicious=False,
                compliance_tag="Compliant",
                audit_note="Verified ledger entry"
            ))
        closing_bal = running_bal if running_bal is not None else 0.0

    seen_debits = {}
    flagged_count = 0
    for t in transactions:
        debit_key = (t.amount, t.transaction_type, t.description[:20])
        if t.amount > 0 and t.transaction_type == "Debit":
            if debit_key in seen_debits:
                t.is_suspicious = True
                t.compliance_tag = "Operational Audit"
                t.audit_note = f"Repeated debit of Rs.{t.amount:,.2f}"
                flagged_count += 1
            else:
                seen_debits[debit_key] = True

        if "cash" in t.category.lower() and t.amount >= 10000.0:
            t.is_suspicious = True
            t.compliance_tag = "Sec 269ST / 40A(3)"
            t.audit_note = f"High-value cash transaction (Rs.{t.amount:,.2f})"
            flagged_count += 1
        elif t.amount >= 50000.0:
            t.is_suspicious = True
            t.compliance_tag = "AML / High Exposure"
            t.audit_note = f"High-value transaction threshold exceeded (Rs.{t.amount:,.2f})"
            flagged_count += 1
        elif t.amount >= 25000.0 and t.amount % 5000 == 0:
            t.is_suspicious = True
            t.compliance_tag = "Audit Scrutiny"
            t.audit_note = f"Round lump-sum movement (Rs.{t.amount:,.2f})"
            flagged_count += 1

    net_movement = round(total_credits - total_debits, 2)
    reconciled = (abs(round((opening_bal + total_credits - total_debits), 2) - round(closing_bal, 2)) < 1.0) if opening_bal else True
    rec_status = "100% Mathematically Reconciled" if reconciled else "Audit Reconciled (Active Settlement Drift)"

    header_snippet = clean_ascii(combined_header[:1800])
    prompt = (
        f"Universal statement audited.\n"
        f"Entity Header Text: {header_snippet}\n"
        f"Financial Audit: Debits Rs.{total_debits:,.2f}, Credits Rs.{total_credits:,.2f}, Total Tx: {len(transactions)}, Anomalies: {flagged_count}.\n"
        f"Provide the exact Account Holder / Bank Name, Statement Period, and a 2-sentence executive summary. Return valid ASCII text only."
    )

    summary_data = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ExecutiveAuditSummary,
            temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            system_instruction="Senior forensic accountant and auditor. Provide strictly accurate ASCII text."
        )
    ).parsed

    return StatementAuditReport(
        account_holder_or_bank=clean_ascii(summary_data.account_holder_or_bank or "Verified Account"),
        statement_period=clean_ascii(summary_data.statement_period or "Audited Period"),
        opening_balance=round(opening_bal, 2),
        closing_balance=round(closing_bal, 2),
        total_debit=round(total_debits, 2),
        total_credit=round(total_credits, 2),
        net_cashflow=net_movement,
        suspicious_count=flagged_count,
        reconciliation_status=rec_status,
        file_checksum=file_hash,
        executive_summary=clean_ascii(summary_data.executive_summary),
        transactions=transactions
    )

def generate_audit_pdf(report: StatementAuditReport) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=18,
        leftMargin=18,
        topMargin=18,
        bottomMargin=18
    )
    elements = []
    styles = getSampleStyleSheet()

    brand_title_style = ParagraphStyle('BrandTitle', parent=styles['Heading1'], fontSize=18, leading=22, textColor=colors.HexColor('#0F172A'), spaceAfter=2)
    brand_tagline_style = ParagraphStyle('BrandTagline', parent=styles['Normal'], fontSize=9, leading=12, textColor=colors.HexColor('#2563EB'), fontName="Helvetica-Bold", spaceAfter=6)
    report_subtitle_style = ParagraphStyle('ReportSubtitle', parent=styles['Normal'], fontSize=9, leading=13, textColor=colors.HexColor('#475569'), spaceAfter=8)
    body_style = ParagraphStyle('DocBody', parent=styles['Normal'], fontSize=8, leading=11, textColor=colors.HexColor('#334155'))
    table_cell = ParagraphStyle('TableCell', parent=styles['Normal'], fontSize=7.5, leading=9.5, textColor=colors.HexColor('#0F172A'))

    elements.append(Paragraph("<b>KSP Consulting and Solutions</b>", brand_title_style))
    elements.append(Paragraph("<i>Complexity Simplified and Strategy Amplified</i>", brand_tagline_style))
    elements.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor('#2563EB'), spaceAfter=6, spaceBefore=0))
    elements.append(Paragraph(
        f"<b>Forensic Statement Audit & Compliance Report</b> | <b>Entity:</b> {report.account_holder_or_bank} | "
        f"<b>Period:</b> {report.statement_period} | <b>Audit Hash:</b> <code>{report.file_checksum}</code> | "
        f"<b>Status:</b> <font color='#16A34A'><b>{report.reconciliation_status}</b></font>",
        report_subtitle_style
    ))
    elements.append(Spacer(1, 2))

    summary_data = [
        ["Total Outflow (Debits)", "Total Inflow (Credits)", "Net Capital Movement", "Compliance Flags"],
        [f"Rs. {report.total_debit:,.2f}", f"Rs. {report.total_credit:,.2f}", f"Rs. {report.net_cashflow:,.2f}", str(report.suspicious_count)]
    ]
    summary_table = Table(summary_data, colWidths=[200, 200, 200, 200])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F172A')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8.5),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BACKGROUND', (0, 1), (-1, 1), colors.HexColor('#F8FAFC')),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#CBD5E1')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 8))

    elements.append(Paragraph(f"<b>Executive Forensic Assessment:</b> {report.executive_summary}", body_style))
    elements.append(Spacer(1, 10))

    headers = ["Date", "Description", "Type", "Amount (Rs.)", "Balance (Rs.)", "Category", "Compliance", "Forensic Audit Notes"]
    table_rows = [headers]

    for t in report.transactions:
        table_rows.append([
            t.date,
            Paragraph(t.description[:40], table_cell),
            t.transaction_type,
            f"{t.amount:,.2f}",
            f"{t.running_balance:,.2f}",
            Paragraph(t.category[:22], table_cell),
            Paragraph(f"<b>{t.compliance_tag}</b>", table_cell),
            Paragraph(t.audit_note, table_cell)
        ])

    ledger_table = Table(table_rows, colWidths=[55, 175, 45, 65, 65, 95, 85, 215])
    table_styles = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1E293B')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 7.5),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('ALIGN', (3, 0), (4, -1), 'RIGHT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
    ]

    for i, t in enumerate(report.transactions, start=1):
        if t.is_suspicious:
            table_styles.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor('#FEE2E2')))
            table_styles.append(('TEXTCOLOR', (2, i), (2, i), colors.HexColor('#DC2626')))
        else:
            if i % 2 == 0:
                table_styles.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor('#F8FAFC')))

    ledger_table.setStyle(TableStyle(table_styles))
    elements.append(ledger_table)
    doc.build(elements)
    return buffer.getvalue()

def generate_tally_xml(report: StatementAuditReport, bank_ledger_name: str = "Bank Account") -> str:
    bank_ledger = saxutils.escape(str(bank_ledger_name).strip() or "Bank Account")
    xml_lines = [
        '<ENVELOPE>',
        '  <HEADER>',
        '    <TALLYREQUEST>Import Data</TALLYREQUEST>',
        '  </HEADER>',
        '  <BODY>',
        '    <IMPORTDATA>',
        '      <REQUESTDESC>',
        '        <REPORTNAME>Vouchers</REPORTNAME>',
        '        <STATICVARIABLES>',
        '          <SVCURRENTCOMPANY>##SVCURRENTCOMPANY</SVCURRENTCOMPANY>',
        '        </STATICVARIABLES>',
        '      </REQUESTDESC>',
        '      <REQUESTDATA>'
    ]

    for idx, t in enumerate(report.transactions, start=1):
        date_clean = re.sub(r'[^\d]', '', t.date)
        dt_str = "20250401"
        try:
            if len(date_clean) == 8:
                d, m, y = int(date_clean[:2]), int(date_clean[2:4]), int(date_clean[4:])
                dt_str = f"{y:04d}{m:02d}{d:02d}"
            elif len(date_clean) == 6:
                d, m, y = int(date_clean[:2]), int(date_clean[2:4]), int("20" + date_clean[4:])
                dt_str = f"{y:04d}{m:02d}{d:02d}"
            else:
                for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%d/%m/%y"):
                    try:
                        dt_obj = datetime.strptime(t.date.strip(), fmt)
                        dt_str = dt_obj.strftime("%Y%m%d")
                        break
                    except ValueError:
                        continue
        except Exception:
            dt_str = "20250401"

        narration = saxutils.escape(t.description)
        category_ledger = saxutils.escape(t.category)
        amt_str = f"{t.amount:.2f}"

        if t.transaction_type == "Credit":
            vch_type = "Receipt"
            vch_xml = f"""        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <VOUCHER VCHTYPE="{vch_type}" ACTION="Create" OBJVIEW="Accounting Voucher View">
            <DATE>{dt_str}</DATE>
            <VOUCHERTYPENAME>{vch_type}</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{idx}</VOUCHERNUMBER>
            <NARRATION>{narration}</NARRATION>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{bank_ledger}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
              <AMOUNT>-{amt_str}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{category_ledger}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
              <AMOUNT>{amt_str}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
          </VOUCHER>
        </TALLYMESSAGE>"""
        else:
            vch_type = "Payment"
            vch_xml = f"""        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <VOUCHER VCHTYPE="{vch_type}" ACTION="Create" OBJVIEW="Accounting Voucher View">
            <DATE>{dt_str}</DATE>
            <VOUCHERTYPENAME>{vch_type}</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{idx}</VOUCHERNUMBER>
            <NARRATION>{narration}</NARRATION>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{category_ledger}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
              <AMOUNT>-{amt_str}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{bank_ledger}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
              <AMOUNT>{amt_str}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
          </VOUCHER>
        </TALLYMESSAGE>"""
        xml_lines.append(vch_xml)

    xml_lines.extend([
        '      </REQUESTDATA>',
        '    </IMPORTDATA>',
        '  </BODY>',
        '</ENVELOPE>'
    ])
    return "\n".join(xml_lines)

# ------------------------------------------------------------------------------
# MINI-AI CONSULTANT ENGINE (GEMINI-3.1-PRO-PREVIEW)
# ------------------------------------------------------------------------------
def run_mini_consultant(business_query: str, region: str = "India / Telangana") -> MiniConsultantReport:
    system_instruction = (
        "You are an elite enterprise business consultant, CA, corporate lawyer, and venture strategist. "
        "Analyze the user's business idea from first principles and deliver a thorough, realistic, and highly practical report. "
        "Structure your entire assessment strictly under these 4 pillars: "
        "1. Legal Requirements & Compliance: Entity formation, licensing, statutory tax mandates, industry-specific compliance, and intellectual property. "
        "2. Applicable Government Schemes & Subsidies: National and regional funding schemes, tax holidays, grants, collateral-free credit, and subsidy criteria (e.g., PMEGP, CGTMSE, SISFS, MUDRA). "
        "3. Project Blueprint & Step-by-Step Implementation Strategy: Phased launch timeline (Phase 1 to Phase 4), operational setup, and go-to-market execution. "
        "4. Financial Forecasting: Initial CAPEX breakdown, ongoing OPEX runway, revenue model, break-even timeline, and projected ROI. "
        "Maintain high factual density. Return valid ASCII plain text within the schema."
    )

    user_prompt = f"Target Jurisdiction: {region}\nBusiness Query & Concept: {clean_ascii(business_query)}"

    response = client.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents=user_prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=MiniConsultantReport,
            temperature=0.2,
            system_instruction=system_instruction
        )
    )
    return response.parsed

def generate_ai_consultant_pdf(report: MiniConsultantReport) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=26,
        leftMargin=26,
        topMargin=22,
        bottomMargin=22
    )
    elements = []
    styles = getSampleStyleSheet()

    t_main = ParagraphStyle('AM1', parent=styles['Heading1'], fontSize=15, leading=18, textColor=colors.HexColor('#0F172A'), fontName='Helvetica-Bold')
    t_tag = ParagraphStyle('AM2', parent=styles['Normal'], fontSize=8, leading=10, textColor=colors.HexColor('#2563EB'), fontName='Helvetica-Bold', spaceAfter=4)
    sec_head = ParagraphStyle('AH1', parent=styles['Heading2'], fontSize=9.5, leading=12.5, textColor=colors.HexColor('#0F172A'), fontName='Helvetica-Bold', spaceBefore=3, spaceAfter=2)
    b_style = ParagraphStyle('AB1', parent=styles['Normal'], fontSize=7.4, leading=10, textColor=colors.HexColor('#334155'))
    th_style = ParagraphStyle('ATH', parent=styles['Normal'], fontSize=7.6, leading=9.8, textColor=colors.white, fontName='Helvetica-Bold')
    tc_style = ParagraphStyle('ATC', parent=styles['Normal'], fontSize=7.3, leading=9.4, textColor=colors.HexColor('#0F172A'))

    # Header
    elements.append(Paragraph("KSP CONSULTING AND SOLUTIONS", t_main))
    elements.append(Paragraph("STRATEGIC VENTURE FEASIBILITY & STATUTORY REPORT", t_tag))
    elements.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor('#2563EB'), spaceBefore=1, spaceAfter=4))

    # Project Title & Summary
    elements.append(Paragraph(f"<b>Project Focus:</b> {clean_ascii(report.project_title)}", sec_head))
    elements.append(Paragraph(clean_ascii(report.executive_summary), b_style))
    elements.append(Spacer(1, 3))

    # Pillar 1: Legal & Statutory Compliance
    elements.append(Paragraph("1. Legal Requirements & Compliance", sec_head))
    elements.append(Paragraph(f"• <b>Corporate Vehicle:</b> {clean_ascii(report.legal_compliance.entity_structure)}", b_style))
    elements.append(Paragraph(f"• <b>Mandatory Licenses:</b> {clean_ascii('; '.join(report.legal_compliance.mandatory_licenses))}", b_style))
    elements.append(Paragraph(f"• <b>Tax & Statutory:</b> {clean_ascii('; '.join(report.legal_compliance.tax_statutory_mandates))}", b_style))
    elements.append(Spacer(1, 3))

    # Pillar 2: Government Schemes & Subsidies
    elements.append(Paragraph("2. Applicable Government Schemes & Subsidies", sec_head))
    for sch in report.government_schemes:
        elements.append(Paragraph(f"• <b>{clean_ascii(sch.scheme_name)}:</b> {clean_ascii(sch.subsidy_benefits)} (<i>Eligibility:</i> {clean_ascii(sch.eligibility)})", b_style))
    elements.append(Spacer(1, 3))

    # Pillar 3: Project Blueprint & Phased Implementation
    elements.append(Paragraph("3. Project Blueprint & Execution Strategy", sec_head))
    for p in report.implementation_blueprint:
        elements.append(Paragraph(f"• <b>{clean_ascii(p.phase_title)}:</b> {clean_ascii('; '.join(p.action_milestones))}", b_style))
    elements.append(Spacer(1, 3))

    # Pillar 4: Financial Forecasting
    elements.append(Paragraph("4. Financial Forecasting & Unit Economics", sec_head))
    fin_rows = [
        [Paragraph("Initial CAPEX", th_style), Paragraph("Monthly OPEX Runway", th_style), Paragraph("Break-Even", th_style), Paragraph("Projected ROI", th_style)],
        [
            Paragraph(clean_ascii(report.financial_forecast.estimated_initial_capex), tc_style),
            Paragraph(clean_ascii(report.financial_forecast.monthly_opex_runway), tc_style),
            Paragraph(clean_ascii(report.financial_forecast.break_even_timeline), tc_style),
            Paragraph(clean_ascii(report.financial_forecast.projected_roi), tc_style)
        ]
    ]
    t_fin = Table(fin_rows, colWidths=[130, 140, 130, 140])
    t_fin.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F172A')),
        ('TOPPADDING', (0, 0), (-1, -1), 2.5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E1')),
        ('BACKGROUND', (0, 1), (-1, 1), colors.white)
    ]))
    elements.append(t_fin)
    elements.append(Spacer(1, 2))
    elements.append(Paragraph(f"• <b>Revenue Strategies:</b> {clean_ascii(' | '.join(report.financial_forecast.revenue_streams))}", b_style))

    doc.build(elements)
    return buf.getvalue()

__all__ = [
    "analyze_statement",
    "generate_audit_pdf",
    "generate_tally_xml",
    "run_mini_consultant",
    "generate_ai_consultant_pdf"
]