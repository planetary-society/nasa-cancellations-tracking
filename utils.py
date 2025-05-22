import re
from decimal import Decimal
import logging
from typing import Set, Optional, Tuple
from titlecase import titlecase

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
# Set of acronyms and initialisms to always keep uppercase.
# This could be loaded from a config file or environment variables in a larger application.
DEFAULT_KEEP_UPPERCASE: Set[str] = {
    # Common Business / Legal
    "LLC", "INC", "LLP", "LTD", "L.L.C.", "I.N.C.", "L.L.P.", "L.T.D.",
    # Geographical / Governmental
    "USA", "US", "UK",
    # Organizations / Agencies
    "NASA", "ESA", "JAXA",
    # NASA Facilities & Major Programs (add more as needed)
    "JPL", # Jet Propulsion Laboratory
    "JSC", # Johnson Space Center
    "KSC", # Kennedy Space Center
    "GSFC", # Goddard Space Flight Center
    "MSFC", # Marshall Space Flight Center
    "ARC", # Ames Research Center
    "GRC", # Glenn Research Center
    "LARC", # Langley Research Center (or LaRC - handled by case-insensitive check)
    "AFRC", # Armstrong Flight Research Center
    "SSC", # Stennis Space Center
    "ISS", # International Space Station
    "JWST", # James Webb Space Telescope
    # Specific examples from user input
    "CSOS", "CL", "FL", "FPRW", "PADF", "ICAT", "ICATEQ", "AC" # For A.C. style
    # Add other common contract/technical acronyms as needed
    "RFQ", "RFP", "SOW", "CDR", "PDR", "QA", "PI", "COTS",
}

# Maximum length for parenthesized text to be uppercased
PAREN_UPPERCASE_MAX_LEN: int = 9 # Fewer than 10 characters

# --- Helper Function ---

def smart_sentence_case(
    text: Optional[str],
    keep_uppercase: Set[str] = DEFAULT_KEEP_UPPERCASE,
    paren_max_len: int = PAREN_UPPERCASE_MAX_LEN
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
            content = match.group(1) # Content inside parentheses
            if len(content) <= paren_max_len:
                return f"({content.upper()})"
            else:
                # Return original match (lowercase parens + content)
                return match.group(0)

        processed_text = re.sub(r'\(([^)]+)\)', paren_replacer, processed_text)

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
        processed_text = re.sub(r'\b([a-zA-Z]+)\b', acronym_replacer, processed_text)

        # 4. Capitalize the first letter of the entire string
        if processed_text:
            processed_text = processed_text[0].upper() + processed_text[1:]

        return processed_text

    except Exception as e:
        logging.error(f"Error processing text: '{text[:50]}...' - {e}", exc_info=True)
        # Decide on fallback behavior: return original text or empty string?
        # Returning original might be safer if processing fails unexpectedly.
        return text # Fallback to original text on error


# Define a callback function for custom word handling
def custom_titlecase_callback(word, **kwargs):
    # If the word is enclosed in parentheses, preserve the case inside.
    if word.startswith('(') and word.endswith(')'):
        return word

    # Special NASA acronyms - always uppercase.
    nasa_acronyms = ['nasa', 'sbir', 'sttr', 'iss', 'tdm', 'tdrs', 'fy', 'scan', 'epscor', 'stem']
    if word.lower() in nasa_acronyms:
        return word.upper()

    # Special NASA acronyms - always uppercase.
    business_acronyms = ['llc', 'inc', 'llp', 'ltd', 'l.l.c.', 'i.n.c.', 'l.l.p.', 'l.t.d.']
    if word.lower() in business_acronyms:
        return word.upper()

    # Handle special cases.
    if word.upper() == 'OSIRIS-REX':
        return 'OSIRIS-REx'
    if word.upper() == 'SCAN':
        return 'SCaN'
    if word.upper() == 'EPSCOR':
        return 'EPSCoR'

    # For small words that should be lowercase (like 'and', 'of', 'the', etc.).
    small_words = ['and', 'of', 'for', 'the', 'a', 'an', 'in', 'on', 'at', 'to']
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

def parse_mod_number(contract_mod_str: Optional[str]) -> Tuple[str, int]:
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
    if not contract_mod_str: # Check again after potential stripping of whitespace-only string
        return "", 0

    # 2. Split by " Modification" followed by (space or end of string)
    # This correctly handles "AWARD_ID Modification", "AWARD_ID Modification ", and "AWARD_ID Modification P001"
    parts = re.split(r'\s+Modification(?:\s+|$)', contract_mod_str, maxsplit=1, flags=re.IGNORECASE)

    # 3. If no " Modification" part that matches criteria, parts[0] is original string.
    if len(parts) < 2:
        # This case implies "Modification" was not found in a way that allows splitting off an award ID.
        # e.g. input is just "AWARD_ID" or "SomeText"
        return contract_mod_str, 0
    
    award_id = parts[0].strip()
    mod_part_full = parts[1].strip() # This will be "" if "Modification" was at the end

    # Handle case where the part after "Modification" is empty (already handled by mod_part_full = "" from split)
    if not mod_part_full:
        logging.info(f"Input '{contract_mod_str}' resulted in empty mod_part_full. Award ID: '{award_id}'. Mod num: 0.")
        return award_id, 0

    # 5. Attempt to extract modification number (int) from non-empty mod_part_full
    mod_num = 0
    mod_num_found = False

    # First, try to match leading digits directly (handles "215", "0 (Base Record)")
    match_leading_digits = re.match(r'^(\d+)', mod_part_full)
    if match_leading_digits:
        mod_str = match_leading_digits.group(1)
        try:
            mod_num = int(mod_str)
            mod_num_found = True
        except (ValueError, TypeError):
                # Should be unlikely if regex matched \d+, but handle anyway
                logging.warning(f"Failed converting leading digits '{mod_str}' from '{mod_part_full}' in '{contract_mod_str}'.")
                # Continue to next check

    # If leading digits weren't found or failed conversion,
    # try stripping leading non-digits (handles "P001", "S022", "A00002")
    if not mod_num_found:
        mod_part_numeric = re.sub(r'^\D+', '', mod_part_full) # Remove leading non-digits
        if mod_part_numeric: # Check if anything numeric remains
            try:
                mod_num = int(mod_part_numeric)
                mod_num_found = True
            except (ValueError, TypeError):
                logging.warning(f"Failed converting digits '{mod_part_numeric}' (after stripping non-digits) from '{mod_part_full}' in '{contract_mod_str}'.")
                # Mod num remains 0

    # If no number was found by either method, log a warning
    if not mod_num_found:
            logging.warning(f"Could not extract numeric modification from '{mod_part_full}' in '{contract_mod_str}'. Defaulting mod to 0.")
            # mod_num is already 0

    # 6. Return the extracted award ID and modification number
    return award_id, mod_num

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
    formatted_string = "${:,.2f}".format(amount_decimal)
    
    return formatted_string
