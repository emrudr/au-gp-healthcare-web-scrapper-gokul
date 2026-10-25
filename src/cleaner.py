import os
from typing import List, Dict, Any, Tuple
import pandas as pd


REQUIRED_COLUMNS = [
    "doctor_name",
    "role",
    "gender",
    "qualification_codes",
    "practice_name",
    "address",
    "suburb",
    "state",
    "postcode",
    "phone_number",
    "search_hub_reference",
    "profile_url",
]


def score_record_completeness(row: pd.Series) -> int:
    """Calculates a completeness score so we retain the richest record during deduplication."""
    score = 0
    if row.get("phone_number"):
        score += 5
    if row.get("qualification_codes"):
        score += 3
    if row.get("gender"):
        score += 2
    if row.get("address"):
        score += 2
    if row.get("role") and row.get("role") != "General Practitioner":
        score += 1
    if row.get("profile_url"):
        score += 1
    return score


def clean_dataset(records: List[Dict[str, Any]]) -> Tuple[pd.DataFrame, int, int, int]:
    """
    Cleans and deduplicates scraped records.
    Deduplicates on profile_url and (doctor_name + practice_name),
    keeping the most complete record.

    Returns:
        (df_cleaned, raw_count, duplicate_count, clean_count)
    """
    raw_count = len(records)
    if raw_count == 0:
        empty_df = pd.DataFrame(columns=REQUIRED_COLUMNS)
        return empty_df, 0, 0, 0

    df = pd.DataFrame(records)

    # Ensure all required columns exist
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    # Reorder to exact requested order and clean strings
    df = df[REQUIRED_COLUMNS].copy()
    for col in REQUIRED_COLUMNS:
        df[col] = df[col].fillna("").astype(str).str.strip()

    # Calculate completeness score to prioritize records with phone numbers / qualifications
    df["_completeness_score"] = df.apply(score_record_completeness, axis=1)

    # Sort so most complete rows appear first
    df = df.sort_values(by="_completeness_score", ascending=False)

    # 1. Deduplicate by profile_url (where non-empty)
    non_empty_url_mask = df["profile_url"] != ""
    df_with_url = df[non_empty_url_mask].drop_duplicates(subset=["profile_url"], keep="first")
    df_without_url = df[~non_empty_url_mask]
    df = pd.concat([df_with_url, df_without_url], ignore_index=True)

    # 2. Deduplicate by doctor_name + practice_name (case-insensitive)
    df["_norm_doctor"] = df["doctor_name"].str.lower().str.replace(r"[^\w\s]", "", regex=True)
    df["_norm_practice"] = df["practice_name"].str.lower().str.replace(r"[^\w\s]", "", regex=True)

    # Filter out empty doctor names if any
    valid_name_mask = df["_norm_doctor"] != ""
    df_valid = df[valid_name_mask].drop_duplicates(subset=["_norm_doctor", "_norm_practice"], keep="first")
    df_invalid = df[~valid_name_mask]
    df = pd.concat([df_valid, df_invalid], ignore_index=True)

    # Clean temporary columns
    df = df.drop(columns=["_completeness_score", "_norm_doctor", "_norm_practice"])

    clean_count = len(df)
    duplicate_count = raw_count - clean_count

    return df, raw_count, duplicate_count, clean_count


def export_csv_quick(
    df: pd.DataFrame,
    output_dir: str = "output",
    filename_stem: str = "australian_gp_directory"
) -> str:
    """Fast non-blocking interim export of CSV only."""
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, f"{filename_stem}.csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    return os.path.abspath(csv_path)


def export_data(
    df: pd.DataFrame,
    output_dir: str = "output",
    filename_stem: str = "australian_gp_directory"
) -> Dict[str, str]:
    """
    Exports cleaned DataFrame to both Excel (.xlsx) and CSV (.csv).
    """
    os.makedirs(output_dir, exist_ok=True)

    excel_path = os.path.join(output_dir, f"{filename_stem}.xlsx")
    csv_path = os.path.join(output_dir, f"{filename_stem}.csv")

    # Export to Excel
    df.to_excel(excel_path, index=False, engine="openpyxl")

    # Export to CSV
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    return {
        "excel_path": os.path.abspath(excel_path),
        "csv_path": os.path.abspath(csv_path)
    }
