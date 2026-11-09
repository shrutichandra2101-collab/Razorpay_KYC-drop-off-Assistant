"""Feature 1 - Application data ingestion.

Reads ONLY the P3_Applications and P3_RejectionCatalog sheets, read-only (the source file is opened
for reading and never written). Validates headers, missing values, ID uniqueness and allowed values,
and returns a structured application-level model. Missing values are preserved as None - a blank
rejection_code is *missing information*, never coerced to a value.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd

APPLICATION_COLUMNS = [
    "application_id", "business_type", "industry", "city", "preferred_language", "signup_date",
    "expected_monthly_tpv_inr_lakh", "whatsapp_opted_in", "step_reached", "application_status",
    "days_since_last_activity", "attempts", "support_contacts", "rejection_code", "rejection_message",
]
CATALOG_COLUMNS = ["code", "reviewer_reason", "plain_language_meaning", "fixable", "standard_fix", "typical_days_to_fix"]

# Fields where blanks are legitimate (missing information, not an error).
NULLABLE_APP_FIELDS = {"rejection_code", "rejection_message"}

ALLOWED = {
    "application_status": {"Activated", "Stalled", "Rejected"},
    "whatsapp_opted_in": {"Yes", "No"},
    "preferred_language": {"English", "Hindi", "Hinglish", "Tamil", "Kannada", "Marathi"},
    "step_reached": {"Business details", "KYC docs", "Bank verification", "Website/App review", "Activated"},
}


@dataclass
class Application:
    application_id: str
    business_type: str
    industry: str
    city: str
    preferred_language: str
    signup_date: Optional[str]
    expected_monthly_tpv_inr_lakh: Optional[float]
    whatsapp_opted_in: str
    step_reached: str
    application_status: str
    days_since_last_activity: Optional[int]
    attempts: Optional[int]
    support_contacts: Optional[int]
    rejection_code: Optional[str]
    rejection_message: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CatalogEntry:
    code: str
    reviewer_reason: str
    plain_language_meaning: str
    fixable: str
    standard_fix: str
    typical_days_to_fix: Optional[int]


@dataclass
class IngestReport:
    source: str
    sha256: str
    sheets_found: List[str]
    application_rows: int = 0
    catalog_rows: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    missing_values: Dict[str, int] = field(default_factory=dict)
    catalog_missing_values: Dict[str, int] = field(default_factory=dict)
    duplicate_ids: List[str] = field(default_factory=list)
    unknown_codes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class Dataset:
    applications: List[Application]
    catalog: Dict[str, CatalogEntry]
    report: IngestReport
    applications_df: pd.DataFrame
    catalog_df: pd.DataFrame


def _clean(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s and s not in {"—", "-", "–"} else None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def _to_int(v: Any) -> Optional[int]:
    v = _clean(v)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> Optional[float]:
    v = _clean(v)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_workbook(source: Union[str, Path, bytes], applications_sheet: str = "P3_Applications",
                  catalog_sheet: str = "P3_RejectionCatalog", source_name: Optional[str] = None) -> Dataset:
    if isinstance(source, (str, Path)):
        raw = Path(source).read_bytes()  # read-only; original file is never modified
        name = source_name or Path(source).name
    else:
        raw = source
        name = source_name or "uploaded workbook"
    sha = hashlib.sha256(raw).hexdigest()
    xls = pd.ExcelFile(io.BytesIO(raw), engine="openpyxl")
    report = IngestReport(source=name, sha256=sha, sheets_found=list(xls.sheet_names))

    for sheet in (applications_sheet, catalog_sheet):
        if sheet not in xls.sheet_names:
            report.errors.append(f"Required sheet '{sheet}' not found. Sheets present: {xls.sheet_names}")
    if report.errors:
        return Dataset([], {}, report, pd.DataFrame(), pd.DataFrame())

    # dtype=object keeps codes/IDs as text and blanks as NaN (→ None), never coerced.
    apps_df = pd.read_excel(xls, applications_sheet, dtype=object)
    cat_df = pd.read_excel(xls, catalog_sheet, dtype=object)
    apps_df.columns = [str(c).strip() for c in apps_df.columns]
    cat_df.columns = [str(c).strip() for c in cat_df.columns]

    missing_cols = [c for c in APPLICATION_COLUMNS if c not in apps_df.columns]
    if missing_cols:
        report.errors.append(f"{applications_sheet}: missing columns {missing_cols}")
    extra_cols = [c for c in apps_df.columns if c not in APPLICATION_COLUMNS]
    if extra_cols:
        report.warnings.append(f"{applications_sheet}: unexpected extra columns ignored {extra_cols}")
    missing_cat = [c for c in CATALOG_COLUMNS if c not in cat_df.columns]
    if missing_cat:
        report.errors.append(f"{catalog_sheet}: missing columns {missing_cat}")
    if report.errors:
        return Dataset([], {}, report, apps_df, cat_df)

    apps_df = apps_df.dropna(how="all")
    cat_df = cat_df.dropna(how="all")
    report.application_rows = len(apps_df)
    report.catalog_rows = len(cat_df)

    for col in APPLICATION_COLUMNS:
        n = int(apps_df[col].apply(lambda v: _clean(v) is None).sum())
        report.missing_values[col] = n
        if n and col not in NULLABLE_APP_FIELDS:
            report.warnings.append(f"{applications_sheet}.{col}: {n} missing value(s)")
    for col in CATALOG_COLUMNS:
        n = int(cat_df[col].apply(lambda v: _clean(v) is None).sum())
        report.catalog_missing_values[col] = n
        if n:
            report.warnings.append(f"{catalog_sheet}.{col}: {n} missing value(s)")

    ids = apps_df["application_id"].astype(str).str.strip()
    dups = sorted(ids[ids.duplicated(keep=False)].unique().tolist())
    if dups:
        report.duplicate_ids = dups
        report.errors.append(f"Duplicate application_id values: {dups}")

    catalog: Dict[str, CatalogEntry] = {}
    for _, r in cat_df.iterrows():
        code = _clean(r["code"])
        if not code:
            continue
        catalog[str(code)] = CatalogEntry(
            code=str(code),
            reviewer_reason=_clean(r["reviewer_reason"]) or "",
            plain_language_meaning=_clean(r["plain_language_meaning"]) or "",
            fixable=_clean(r["fixable"]) or "",
            standard_fix=_clean(r["standard_fix"]) or "",
            typical_days_to_fix=_to_int(r["typical_days_to_fix"]),
        )

    applications: List[Application] = []
    for _, r in apps_df.iterrows():
        signup = _clean(r["signup_date"])
        if hasattr(signup, "strftime"):
            signup = signup.strftime("%d %b %Y")
        code = _clean(r["rejection_code"])
        app = Application(
            application_id=str(_clean(r["application_id"])),
            business_type=_clean(r["business_type"]) or "",
            industry=_clean(r["industry"]) or "",
            city=_clean(r["city"]) or "",
            preferred_language=_clean(r["preferred_language"]) or "",
            signup_date=str(signup) if signup is not None else None,
            expected_monthly_tpv_inr_lakh=_to_float(r["expected_monthly_tpv_inr_lakh"]),
            whatsapp_opted_in=_clean(r["whatsapp_opted_in"]) or "",
            step_reached=_clean(r["step_reached"]) or "",
            application_status=_clean(r["application_status"]) or "",
            days_since_last_activity=_to_int(r["days_since_last_activity"]),
            attempts=_to_int(r["attempts"]),
            support_contacts=_to_int(r["support_contacts"]),
            rejection_code=str(code).strip().upper() if code is not None else None,
            rejection_message=_clean(r["rejection_message"]),
        )
        applications.append(app)
        if app.rejection_code and app.rejection_code not in catalog:
            report.unknown_codes.append(f"{app.application_id}:{app.rejection_code}")

    for field_name, allowed in ALLOWED.items():
        bad = sorted({getattr(a, field_name) for a in applications if getattr(a, field_name) not in allowed})
        if bad:
            report.warnings.append(f"{field_name}: unexpected values {bad} (allowed: {sorted(allowed)})")

    # Consistency: Activated status <-> Activated step.
    incons = [a.application_id for a in applications
              if (a.application_status == "Activated") != (a.step_reached == "Activated")]
    if incons:
        report.warnings.append(f"status/step inconsistency for {incons}")
    if report.unknown_codes:
        report.warnings.append(f"Rejection codes not in catalogue (sent to human review): {report.unknown_codes}")

    return Dataset(applications, catalog, report, apps_df.reset_index(drop=True), cat_df.reset_index(drop=True))
