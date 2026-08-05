import csv
import logging
import os
import re
import tempfile
from collections import Counter
from decimal import Decimal

from titlecase import titlecase

import csv_aliases

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# --- Configuration ---
# Set of acronyms and initialisms to always keep uppercase.
# This could be loaded from a config file or environment variables in a larger application.
DEFAULT_KEEP_UPPERCASE: set[str] = {
    # Common Business / Legal
    "LLC",
    "INC",
    "LLP",
    "LTD",
    "L.L.C.",
    "I.N.C.",
    "L.L.P.",
    "L.T.D.",
    # Geographical / Governmental
    "USA",
    "US",
    "UK",
    # Organizations / Agencies
    "NASA",
    "ESA",
    "JAXA",
    # NASA Facilities & Major Programs (add more as needed)
    "JPL",  # Jet Propulsion Laboratory
    "JSC",  # Johnson Space Center
    "KSC",  # Kennedy Space Center
    "GSFC",  # Goddard Space Flight Center
    "MSFC",  # Marshall Space Flight Center
    "ARC",  # Ames Research Center
    "GRC",  # Glenn Research Center
    "LARC",  # Langley Research Center (or LaRC - handled by case-insensitive check)
    "AFRC",  # Armstrong Flight Research Center
    "SSC",  # Stennis Space Center
    "ISS",  # International Space Station
    "JWST",  # James Webb Space Telescope
    # Specific examples from user input
    "CSOS",
    "CL",
    "FL",
    "FPRW",
    "PADF",
    "ICAT",
    "ICATEQ",
    "AC"  # For A.C. style
    # Add other common contract/technical acronyms as needed
    "RFQ",
    "RFP",
    "SOW",
    "CDR",
    "PDR",
    "QA",
    "PI",
    "COTS",
}

# Maximum length for parenthesized text to be uppercased
PAREN_UPPERCASE_MAX_LEN: int = 9  # Fewer than 10 characters

# --- Helper Function ---


def smart_sentence_case(
    text: str | None,
    keep_uppercase: set[str] = DEFAULT_KEEP_UPPERCASE,
    paren_max_len: int = PAREN_UPPERCASE_MAX_LEN,
) -> str:
    """
    Converts an uppercase string to sentence case, preserving specified acronyms
    and short parenthesized text in uppercase.

    Rules:
    1. Converts the text to lowercase as a base.
    2. Capitalizes the first letter of the resulting string.
    3. Keeps specified words/acronyms (case-insensitive match) in uppercase.
    4. Keeps text within parentheses uppercase if its length is less than
       paren_max_len + 1 characters.
    5. Handles standard punctuation like apostrophes correctly.

    Args:
        text: The input string, expected to be mostly uppercase.
              Can be None or empty.
        keep_uppercase: A set of strings (acronyms, initialisms) that should
                        remain in uppercase. Defaults to DEFAULT_KEEP_UPPERCASE.
        paren_max_len: The maximum character length of text inside parentheses
                       to be kept uppercase. Defaults to PAREN_UPPERCASE_MAX_LEN.

    Returns:
        The processed string in smart sentence case, or an empty string if
        the input was None or empty.
    """
    if not text:
        return ""

    try:
        # 1. Start with lowercase
        processed_text = text.lower()

        # 2. Handle parenthesized text: Uppercase if short
        # Uses a lambda function to check length and conditionally uppercase
        def paren_replacer(match):
            content = match.group(1)  # Content inside parentheses
            if len(content) <= paren_max_len:
                return f"({content.upper()})"
            else:
                # Return original match (lowercase parens + content)
                return match.group(0)

        processed_text = re.sub(r"\(([^)]+)\)", paren_replacer, processed_text)

        # 3. Handle specified acronyms/words: Uppercase if in the set
        # Uses a lambda function to check against the keep_uppercase set
        # We use word boundaries (\b) to match whole words only.
        # Need to handle cases like "NASA's" correctly - the regex only matches letters.
        # We convert the matched word to upper to check against the set.
        def acronym_replacer(match):
            word = match.group(1)
            if word.upper() in keep_uppercase:
                return word.upper()
            else:
                # If not in the set, return the word as it is (lowercase)
                return word

        # This regex finds sequences of letters bounded by non-word characters (or start/end)
        # It won't capture A.C. directly but will process A and C individually if AC is in the set.
        processed_text = re.sub(r"\b([a-zA-Z]+)\b", acronym_replacer, processed_text)

        # 4. Capitalize the first letter of the entire string
        if processed_text:
            processed_text = processed_text[0].upper() + processed_text[1:]

        return processed_text

    except Exception as e:
        logging.error(f"Error processing text: '{text[:50]}...' - {e}", exc_info=True)
        # Decide on fallback behavior: return original text or empty string?
        # Returning original might be safer if processing fails unexpectedly.
        return text  # Fallback to original text on error


# Define a callback function for custom word handling
def custom_titlecase_callback(word, **kwargs):
    # If the word is enclosed in parentheses, preserve the case inside.
    if word.startswith("(") and word.endswith(")"):
        return word

    # Special NASA acronyms - always uppercase.
    nasa_acronyms = [
        "nasa",
        "sbir",
        "sttr",
        "iss",
        "tdm",
        "tdrs",
        "fy",
        "scan",
        "epscor",
        "stem",
    ]
    if word.lower() in nasa_acronyms:
        return word.upper()

    # Special NASA acronyms - always uppercase.
    business_acronyms = [
        "llc",
        "inc",
        "llp",
        "ltd",
        "l.l.c.",
        "i.n.c.",
        "l.l.p.",
        "l.t.d.",
    ]
    if word.lower() in business_acronyms:
        return word.upper()

    # Handle special cases.
    if word.upper() == "OSIRIS-REX":
        return "OSIRIS-REx"
    if word.upper() == "SCAN":
        return "SCaN"
    if word.upper() == "EPSCOR":
        return "EPSCoR"

    # For small words that should be lowercase (like 'and', 'of', 'the', etc.).
    small_words = ["and", "of", "for", "the", "a", "an", "in", "on", "at", "to"]
    if word.lower() in small_words and not word.istitle():
        return word.lower()

    # If no other rule matched, let titlecase apply its default,
    # or, to be more forceful for simple words, apply basic title()
    return word.title() if word else word


def contracts_titlecase(text):
    """Apply NASA-specific title casing rules to text"""
    if not text:
        return ""
    return titlecase(text, callback=custom_titlecase_callback)


# Splits a modification number into digit/non-digit runs.
_MOD_NUMBER_PARTS = re.compile(r"(\d+)")


def natural_modification_key(value) -> tuple:
    """Sort key ordering modification numbers naturally: "2" before "10".

    Lexicographic order gets this wrong, and same-day mods are ordered by
    modification number in two places that must agree - reverify_awards, which
    decides whether activity followed a termination, and initial_end_dates,
    which picks an award's earliest reported end date. Distinct from
    parse_mod_number, which extracts a single int and so collapses "P2" and
    "P0002"; this preserves every run and never loses the non-numeric parts.
    """
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in _MOD_NUMBER_PARTS.split(str(value or ""))
        if part
    )


def parse_mod_number(contract_mod_str: str | None) -> tuple[str, int]:
    """
    Parses a string potentially containing an award ID and a modification identifier.

    Handles formats like:
        - "AWARD_ID Modification P001"
        - "AWARD_ID Modification S022"
        - "AWARD_ID Modification A00002"
        - "AWARD_ID Modification 215"
        - "AWARD_ID Modification 0 (Base Record)"
        - "AWARD_ID" (no modification)

    Args:
        contract_mod_str: The input string to parse.

    Returns:
        A tuple containing:
        - The extracted award ID (string). Returns the original string if no
            " Modification " part is found. Returns empty string if input is None/empty.
        - The extracted modification number (int). Returns 0 if no modification
            part is found, if the modification part doesn't contain digits that can be
            parsed according to the rules, or if the input is None/empty.
    """
    # 1. Handle None or empty input
    if not contract_mod_str:
        return "", 0

    # Ensure input is treated as string and strip whitespace
    contract_mod_str = str(contract_mod_str).strip()
    if (
        not contract_mod_str
    ):  # Check again after potential stripping of whitespace-only string
        return "", 0

    # 2. Split by " Modification" followed by (space or end of string)
    # This correctly handles "AWARD_ID Modification", "AWARD_ID Modification ", and "AWARD_ID Modification P001"
    parts = re.split(
        r"\s+Modification(?:\s+|$)", contract_mod_str, maxsplit=1, flags=re.IGNORECASE
    )

    # 3. If no " Modification" part that matches criteria, parts[0] is original string.
    if len(parts) < 2:
        # This case implies "Modification" was not found in a way that allows splitting off an award ID.
        # e.g. input is just "AWARD_ID" or "SomeText"
        return contract_mod_str, 0

    award_id = parts[0].strip()
    mod_part_full = parts[1].strip()  # This will be "" if "Modification" was at the end

    # Handle case where the part after "Modification" is empty (already handled by mod_part_full = "" from split)
    if not mod_part_full:
        logging.info(
            f"Input '{contract_mod_str}' resulted in empty mod_part_full. Award ID: '{award_id}'. Mod num: 0."
        )
        return award_id, 0

    # 5. Attempt to extract modification number (int) from non-empty mod_part_full
    mod_num = 0
    mod_num_found = False

    # First, try to match leading digits directly (handles "215", "0 (Base Record)")
    match_leading_digits = re.match(r"^(\d+)", mod_part_full)
    if match_leading_digits:
        mod_str = match_leading_digits.group(1)
        try:
            mod_num = int(mod_str)
            mod_num_found = True
        except (ValueError, TypeError):
            # Should be unlikely if regex matched \d+, but handle anyway
            logging.warning(
                f"Failed converting leading digits '{mod_str}' from '{mod_part_full}' in '{contract_mod_str}'."
            )
            # Continue to next check

    # If leading digits weren't found, take the first digit run. Handles
    # "P001", "S022", "A00002" - and letter-SUFFIXED mods like "P0037A"
    # (a sub-modification of mod 37, first seen 2026-07-30 on 80JSC023FA010).
    # Stripping only leading non-digits left "0037A", which failed int() and
    # defaulted to 0 - so a letter-suffixed FINAL mod would silently lose the
    # latest-mod contest to every numbered one and NPDV would keyword-scan a
    # stale description. Parsing it as its base number instead makes it tie
    # with its base mod, and the >= comparison lets the later row win the tie.
    if not mod_num_found:
        match_digits = re.search(r"\d+", mod_part_full)
        if match_digits:
            mod_num = int(match_digits.group())
            mod_num_found = True

    # If no number was found by either method, log a warning
    if not mod_num_found:
        logging.warning(
            f"Could not extract numeric modification from '{mod_part_full}' in '{contract_mod_str}'. Defaulting mod to 0."
        )
        # mod_num is already 0

    # 6. Return the extracted award ID and modification number
    return award_id, mod_num


# The four shapes USAspending's generated (composite) award ids come in. The
# regex is built from the same tuple so a fifth prefix only has to be added
# once - the prefix list and the extraction pattern used to drift apart.
GENERATED_AWARD_ID_PREFIXES = ("ASST_NON_", "ASST_AGG_", "CONT_AWD_", "CONT_IDV_")

GENERATED_AWARD_ID_RE = re.compile(
    r"^(?:%s)_(.+?)_\d+(?:_|$)"
    % "|".join(prefix.rstrip("_") for prefix in GENERATED_AWARD_ID_PREFIXES)
)


def is_generated_award_id(value: str | None) -> bool:
    """True when this looks like a USAspending generated id, not a PIID/FAIN."""
    return (value or "").strip().startswith(GENERATED_AWARD_ID_PREFIXES)


def canonical_generated_award_id(value: str | None) -> str:
    """Normalize legacy NASA assistance ids to USAspending's canonical form.

    Historical snapshots used NASA's subtier code ``8000`` as the final
    component of assistance ids. USAspending's transaction endpoint keys those
    awards with the top-tier agency code ``080`` instead.
    """
    text = (value or "").strip()
    match = re.fullmatch(r"(ASST_(?:NON|AGG)_([^_]+))_8000", text)
    if match and match.group(2).upper().startswith(("80", "NN")):
        return f"{match.group(1)}_080"
    return text


def canonical_usaspending_url(value: str | None) -> str:
    """Canonicalize the generated award id embedded in a USAspending URL."""
    text = (value or "").strip()
    match = re.search(r"/award/([^/?#]+)", text)
    if not match:
        return text
    award_id = canonical_generated_award_id(match.group(1))
    return f"{text[: match.start(1)]}{award_id}{text[match.end(1) :]}"


def award_id_from_generated_id(value: str | None) -> str:
    """Pull the PIID/FAIN out of a USAspending generated award id.

    USAspending's own award URLs carry a composite id -
    ``ASST_NON_80NSSC24K0913_8000``, ``CONT_AWD_<piid>_<agency>_<parent>_<pa>``
    - and DOGE's grant links quote it verbatim. Award lookups take the PIID or
    FAIN, so passing the composite through matches nothing: 26 DOGE-claimed
    grants were silently dropped from every run this way, and the same award
    reported by two sources looked like two different awards.

    Returns the embedded id, or the input unchanged when it isn't a generated
    id. An id that itself ends in ``_<digits>`` is ambiguous here; callers
    should fall back to a lookup by generated id for anything that still fails
    to resolve.
    """
    text = (value or "").strip()
    match = GENERATED_AWARD_ID_RE.match(text)
    return match.group(1) if match else text


def format_as_currency(amount: int) -> str:
    """
    Convert an integer to a nicely formatted US currency string.

    Args:
        amount (int): The amount to format

    Returns:
        str: Formatted currency string with $ symbol and commas
    """
    # Convert to Decimal for precise arithmetic, though for int input it might be overkill
    # but good practice if floats were allowed.
    amount_decimal = Decimal(amount)

    # Format to two decimal places, with commas for thousands separator
    formatted_string = f"${amount_decimal:,.2f}"

    return formatted_string


def read_header(path: str) -> list[str]:
    """The stored column names of a repo-owned CSV, before any aliasing."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        return list(csv.DictReader(fh).fieldnames or [])


def read_rows(
    path: str,
    *,
    aliases: dict[str, str] | None = None,
    values: dict[str, dict[str, str]] | None = None,
    columns=None,
    exact_columns: bool = False,
    errors: str | None = None,
) -> list[dict]:
    """Read a repo-owned CSV into rows, translating the stored header.

    The read counterpart to write_sidecar_csv, and the single point at which a
    stored column name becomes the name the code uses. The translation is
    applied to `reader.fieldnames` *before* the rows are read, which is what
    makes DictReader key every row by the current name - no caller sees, or has
    to rewrite, a stored name.

    Which translation applies is resolved from the path (see csv_aliases), so
    a caller cannot read a file with the wrong table or forget one. Omitting it
    would not raise: an un-aliased read returns stored keys, `row.get()`
    returns None, and whatever was checking that value stops checking it. Pass
    `aliases={}` to read a header exactly as stored.

    `values` does the same for cells whose *contents* are a vocabulary the code
    renamed - the source labels in a snapshot's Source column. It is keyed by
    the renamed column, so the header pass has to run first.

    `columns` asserts the header carries what the caller needs: by default that
    every named column is present, or with `exact_columns` that the header is
    exactly this list in this order.

    Always decodes as utf-8-sig. That is identical to utf-8 for a file without
    a byte-order mark and tolerant of one with it, so whether a human's editor
    wrote a BOM stops being a per-caller decision - dropped_award_status.csv is
    hand-edited and was previously read as utf-8 here and utf-8-sig elsewhere.
    """
    if aliases is None:
        aliases = csv_aliases.aliases_for(path)
    value_aliases = csv_aliases.value_aliases_for(path) if values is None else values
    with open(path, newline="", encoding="utf-8-sig", errors=errors) as fh:
        reader = csv.DictReader(fh)
        names = list(reader.fieldnames or [])
        if aliases:
            names = [aliases.get(name, name) for name in names]
        duplicates = {name for name, count in Counter(names).items() if count > 1}
        if duplicates:
            raise RuntimeError(
                f"{path} has duplicate column name(s) "
                f"{', '.join(sorted(duplicates))}; rows would silently lose data."
            )
        if columns is not None:
            if exact_columns:
                if names != list(columns):
                    raise RuntimeError(
                        f"{path} has columns {names}; expected {list(columns)}"
                    )
            else:
                missing = set(columns) - set(names)
                if missing:
                    raise RuntimeError(
                        f"{path} is missing column(s): {', '.join(sorted(missing))}"
                    )
        # DictReader keys rows off this attribute, so assigning it before
        # iteration is what renames the columns.
        reader.fieldnames = names
        rows = list(reader)

    # Cells second, and only for the columns that declare a mapping. Keyed by
    # the renamed column, so this can only run after the header pass.
    for column, mapping in value_aliases.items():
        if column not in names:
            continue
        for row in rows:
            stored = row.get(column)
            if stored in mapping:
                row[column] = mapping[stored]
    return rows


def write_sidecar_csv(path: str, fieldnames, rows: dict[str, dict]) -> None:
    """Atomically rewrite a machine-owned sidecar in deterministic order.

    Every sidecar in this repo is a full rewrite keyed by Award ID, and each
    one is committed by CI, so a partially written file is a corrupt data
    commit. The write-to-temp-then-os.replace contract lives here once rather
    than being restated per sidecar, where a hardening of one copy (an fsync,
    a leak fix) would silently not reach the others.
    """
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            newline="",
            encoding="utf-8",
            dir=parent,
            prefix="." + os.path.basename(path) + ".",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_path = fh.name
            writer = csv.DictWriter(fh, fieldnames=list(fieldnames))
            writer.writeheader()
            for key in sorted(rows):
                writer.writerow(rows[key])
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
