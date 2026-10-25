import re
from typing import Dict, Any, Tuple, Optional


# Regular expressions for common Australian medical roles and qualifications
QUALIFICATION_TOKENS = (
    r"\b(MBBS|FRACGP|FACRRM|MD|MRCGP|RACGP|BMBS|BMed|BMedSci|DCH|DRANZCOG|"
    r"DGM|FAMAC|FACNEM|AMC|Dip|BSc|MSc|PhD|BA|MBChB|BChir|MRCP|LRCP|MRCS|"
    r"FRCP|FRCS|FRCOG|DFFP|DRCOG|Dermatology|Skin Cancer)\b"
)

ROLE_TOKENS = (
    r"\b(General Practitioner|GP Registrar|General Practice|Medical Practitioner|"
    r"Women's Health General Practitioner|Skin Cancer Doctor|Specialist General Practitioner|"
    r"Doctor / Physician|GP)\b"
)


def clean_text(text: Optional[str]) -> str:
    """Clean extra whitespace and invisible characters from text."""
    if not text:
        return ""
    cleaned = re.sub(r"[\r\n\t]+", " ", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def parse_doctor_name(raw_name: str) -> Tuple[str, str]:
    """
    Parses doctor name and extracts any attached role.
    Cleans prefixes (e.g. 'GP - Dr ') and trailing punctuation/hyphens.
    Example: 'Dr Paul Ewing - GP' -> ('Dr Paul Ewing', 'GP')
    Example: 'Dr Rachael Norman Women's Health General Practitioner'
    -> ('Dr Rachael Norman', 'Women's Health General Practitioner')
    """
    if not raw_name:
        return "", ""
    name = raw_name.strip()

    # Normalize 'GP - Dr ' to 'Dr '
    if name.startswith("GP - "):
        name = name[5:].strip()

    role = ""
    # Look for roles attached at the end of the name
    role_match = re.search(r"[\s\-:,|]+(" + ROLE_TOKENS + r".*)$", name, re.IGNORECASE)
    if role_match:
        role = role_match.group(1).strip()
        name = name[:role_match.start()].strip()

    # Clean trailing hyphens, colons, commas, slashes
    name = re.sub(r"[\s\-:,|/]+$", "", name).strip()

    return name, role


def parse_role_and_gender(text_or_lines: str) -> Tuple[str, str]:
    """
    Extracts role and gender from text.
    Example: 'General Practitioner, Male' -> ('General Practitioner', 'Male')
    Example: 'General Practitioner, Female' -> ('General Practitioner', 'Female')
    """
    if not text_or_lines:
        return "General Practitioner", ""

    text = clean_text(text_or_lines)
    gender = ""
    role = "General Practitioner"

    # Detect gender
    if re.search(r"\bMale\b", text, re.IGNORECASE):
        gender = "Male"
    elif re.search(r"\bFemale\b", text, re.IGNORECASE):
        gender = "Female"
    elif re.search(r"\bNon-binary\b", text, re.IGNORECASE):
        gender = "Non-binary"

    # Remove gender part to extract pure role
    role_cand = re.sub(r",?\s*(?:Male|Female|Non-binary)\b", "", text, flags=re.IGNORECASE).strip()
    role_cand = re.sub(r"^[\s,:\-]+|[\s,:\-]+$", "", role_cand).strip()

    if role_cand and any(w.lower() in role_cand.lower() for w in ["general practitioner", "gp", "doctor", "registrar", "physician"]):
        role = role_cand

    return role, gender


def parse_qualifications(text_or_lines: str) -> str:
    """
    Extracts recognized medical qualifications.
    Example: 'MBBS, FRACGP' -> 'MBBS, FRACGP'
    """
    if not text_or_lines:
        return ""
    text = clean_text(text_or_lines)

    # Don't treat General Practitioner as a qualification
    if text.lower() in ["general practitioner", "gp"]:
        return ""

    matches = re.findall(QUALIFICATION_TOKENS, text, re.IGNORECASE)
    if matches:
        cleaned = re.sub(r"\s+", " ", text).strip()
        cleaned = re.sub(r"^[\s,:\-]+|[\s,:\-]+$", "", cleaned)
        return cleaned
    return ""


def parse_practice_name(line_text: str) -> str:
    """
    Extracts practice/hospital name from card line.
    Example: 'Burleigh Heads Medical Centre, Burleigh Heads QLD 5.8km away'
    -> 'Burleigh Heads Medical Centre'
    """
    if not line_text:
        return ""
    text = clean_text(line_text)
    # Strip distance indicator e.g. '5.8km away' or '5km away'
    text = re.sub(r"\s*\d+(\.\d+)?\s*km\s*away", "", text, flags=re.IGNORECASE).strip()
    # Strip trailing suburb/state e.g. ', Robina QLD' or ', Southport QLD 4215'
    m = re.match(r"^(.*?)(?:,\s*[A-Za-z\s]+?\s+(?:NSW|QLD|VIC|WA|SA|TAS|ACT|NT)\s*\d*)$", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text


def parse_address(
    addr_str: Optional[str],
    default_suburb: str = "",
    default_state: str = "",
    default_postcode: str = ""
) -> Tuple[str, str, str, str]:
    """
    Parses Australian address string into street address, suburb, state, and postcode.
    Handles concatenated cases like '40 Benowa Road Shop 27Southport, QLD 4215'.
    """
    if not addr_str:
        return "", default_suburb, default_state, default_postcode

    addr = clean_text(addr_str)
    # Remove distance indicators
    addr = re.sub(r"\s*\d+(\.\d+)?\s*km\s*away", "", addr, flags=re.IGNORECASE).strip()

    state = default_state
    postcode = default_postcode
    suburb = default_suburb
    street = addr

    # Find State and 4-digit postcode (e.g. QLD 4215, NSW 2000)
    state_pc_match = re.search(r"\b(NSW|QLD|VIC|WA|SA|TAS|ACT|NT)\s*(\d{4})\b", addr, re.IGNORECASE)
    if state_pc_match:
        state = state_pc_match.group(1).upper()
        postcode = state_pc_match.group(2)
        before_state = addr[:state_pc_match.start()].rstrip(", ")

        # Look for suburb in portion before state
        parts = [p.strip() for p in before_state.split(",") if p.strip()]
        if len(parts) >= 2:
            suburb = parts[-1]
            street = ", ".join(parts[:-1])
        elif len(parts) == 1:
            part = parts[0]
            if default_suburb and default_suburb.lower() in part.lower():
                idx = part.lower().rfind(default_suburb.lower())
                suburb = part[idx:idx + len(default_suburb)]
                street = part[:idx].rstrip(", ")
            else:
                street = part

    # Clean glued suburb e.g. '27Southport'
    if suburb and street.endswith(suburb):
        street = street[:-len(suburb)].rstrip(", ")

    m_stuck = re.search(r"(\d+)([A-Z][a-z]+)$", street)
    if m_stuck and (not suburb or suburb.lower() == default_suburb.lower()):
        suburb = m_stuck.group(2)
        street = street[:m_stuck.start(2)].strip()

    return street.strip(), suburb.strip(), state.strip(), postcode.strip()


def format_phone_number(raw_phone: Optional[str]) -> str:
    """Formats phone number into a clean Australian phone representation."""
    if not raw_phone:
        return ""
    cleaned = re.sub(r"^tel:\s*", "", raw_phone, flags=re.IGNORECASE).strip()
    digits = re.sub(r"[^\d+]", "", cleaned)

    if digits.startswith("+61"):
        rest = digits[3:]
        if len(rest) == 9:
            return f"+61 {rest[0]} {rest[1:5]} {rest[5:]}"
        elif len(rest) == 8:
            return f"+61 {rest[:4]} {rest[4:]}"
        return digits
    elif digits.startswith("0") and len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]} {digits[6:]}"

    return cleaned


def parse_doctor_block(
    text: str,
    default_suburb: str = "",
    default_state: str = "",
    default_postcode: str = ""
) -> Dict[str, Any]:
    """
    Parses a multi-line raw doctor text block.
    Example:
    Dr Aung Thura
    General Practitioner, Male
    MBBS, FRACGP
    40 Benowa Road Shop 27Southport, QLD 4215
    +61 7057384657
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    doctor_name = ""
    role = "General Practitioner"
    gender = ""
    qualifications = ""
    phone = ""
    address_lines = []

    for line in lines:
        if not doctor_name and (
            line.startswith("Dr ") or line.startswith("Dr. ") or
            line.startswith("GP - Dr ") or line.startswith("A/Prof ") or
            line.startswith("Prof ")
        ):
            d_name, d_role = parse_doctor_name(line)
            doctor_name = d_name
            if d_role:
                role = d_role
            continue

        if re.search(QUALIFICATION_TOKENS, line, re.IGNORECASE) and not re.search(r"\bGeneral Practitioner\b", line, re.IGNORECASE):
            qualifications = parse_qualifications(line)
            continue

        if re.search(ROLE_TOKENS, line, re.IGNORECASE):
            r, g = parse_role_and_gender(line)
            if r:
                role = r
            if g:
                gender = g
            continue

        if line.lower() in ["male", "female", "non-binary"] and not gender:
            gender = line.capitalize()
            continue

        m_phone = re.search(r"(\+?61\s*\d[\d\s]{7,}|\b0\d[\d\s]{7,}\b)", line)
        if m_phone:
            phone = format_phone_number(m_phone.group(1))
            continue

        if re.search(r"\b(NSW|QLD|VIC|WA|SA|TAS|ACT|NT)\s*\d{4}\b", line) or any(
            w in line.lower() for w in ["road", "rd", "street", "st", "avenue", "ave", "way", "shop", "centre", "highway"]
        ):
            address_lines.append(line)

    addr_raw = " ".join(address_lines)
    street, sub, st, pc = parse_address(addr_raw, default_suburb, default_state, default_postcode)

    return {
        "doctor_name": doctor_name,
        "role": role,
        "gender": gender,
        "qualification_codes": qualifications,
        "address": street,
        "suburb": sub,
        "state": st,
        "postcode": pc,
        "phone_number": phone
    }


def parse_doctor_card(card_data: Dict[str, Any], hub_info: Dict[str, str]) -> Dict[str, Any]:
    """
    Parses card data extracted directly from HotDoc list cards.
    Extracts doctor_name, practice_name, baseline address, and profile_url.
    """
    raw_title = card_data.get("title", "")
    doctor_name, role_from_title = parse_doctor_name(raw_title)

    raw_line = card_data.get("line", "")
    practice_name = parse_practice_name(raw_line)

    href = card_data.get("href", "")
    profile_url = f"https://www.hotdoc.com.au{href}" if href.startswith("/") else href

    # Default location values from hub
    ref_label = hub_info.get("reference_label", "")
    suburb = hub_info.get("suburb", "")
    state = hub_info.get("state", "")
    postcode = hub_info.get("postcode", "")

    # If the clinic URL contains the true suburb, state, postcode, extract them
    m_url = re.search(r"/medical-centres/([a-zA-Z0-9-]+)-(NSW|QLD|VIC|WA|SA|TAS|ACT|NT)-(\d{4})/", profile_url)
    if m_url:
        suburb = m_url.group(1).replace("-", " ").title()
        state = m_url.group(2).upper()
        postcode = m_url.group(3)

    # Baseline address from card line if possible
    street, parsed_suburb, parsed_state, parsed_postcode = parse_address(raw_line, suburb, state, postcode)
    if parsed_suburb:
        suburb = parsed_suburb
    if parsed_state:
        state = parsed_state
    if parsed_postcode:
        postcode = parsed_postcode

    role = role_from_title if role_from_title else "General Practitioner"

    return {
        "doctor_name": doctor_name,
        "role": role,
        "gender": "",
        "qualification_codes": "",
        "practice_name": practice_name,
        "address": street,
        "suburb": suburb,
        "state": state,
        "postcode": postcode,
        "phone_number": "",
        "search_hub_reference": ref_label,
        "profile_url": profile_url
    }
