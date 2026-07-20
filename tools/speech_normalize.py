"""Normalize text for TTS pronunciation (ElevenLabs & friends).

Local addition (not upstream): converts money, percentages, math symbols,
large numbers, decimals, and common abbreviations into words so TTS engines
pronounce them naturally instead of stumbling over raw symbols.

Kept in its own module so upstream engine syncs never clobber it.
Entry point: ``normalize_for_speech(text) -> str``.
"""

from __future__ import annotations

import re

# ── Integer → words ─────────────────────────────────────────────────────────

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]
_SCALE = [(10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"),
          (10 ** 3, "thousand")]


def _int_to_words(n: int) -> str:
    """Spell out an integer (0 <= n < 10^15)."""
    if n < 0:
        return "negative " + _int_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, rem = divmod(n, 10)
        return _TENS[tens] + ("-" + _ONES[rem] if rem else "")
    if n < 1000:
        hundreds, rem = divmod(n, 100)
        out = _ONES[hundreds] + " hundred"
        return out + (" " + _int_to_words(rem) if rem else "")
    for value, name in _SCALE:
        if n >= value:
            major, rem = divmod(n, value)
            out = _int_to_words(major) + " " + name
            return out + (" " + _int_to_words(rem) if rem else "")
    return str(n)  # out of range — leave as digits


def _digits_to_words(digits: str) -> str:
    """Read digits one by one ("14" -> "one four") for decimals."""
    return " ".join(_ONES[int(d)] for d in digits if d.isdigit())


def _num_token_to_words(token: str) -> str:
    """'1,234.56' -> words. Decimals read digit-by-digit after 'point'."""
    token = token.replace(",", "")
    if "." in token:
        whole, frac = token.split(".", 1)
        whole_words = _int_to_words(int(whole)) if whole else "zero"
        return f"{whole_words} point {_digits_to_words(frac)}"
    return _int_to_words(int(token))


# ── Currency ────────────────────────────────────────────────────────────────

_MONEY_SUFFIX = {
    "k": "thousand", "m": "million", "b": "billion", "t": "trillion",
    "mm": "million", "bn": "billion",
}

_MONEY_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{1,2}))?"
    r"(?:\s?(k|K|[mM]{1,2}|[bB][nN]?|[tT]|thousand|million|billion|trillion)\b)?"
)


def _money_repl(m: re.Match) -> str:
    whole_raw = m.group(1).replace(",", "")
    cents_raw = m.group(2)
    suffix = (m.group(3) or "").lower()

    if suffix:
        scale = _MONEY_SUFFIX.get(suffix, suffix)
        amount = whole_raw + ("." + cents_raw if cents_raw else "")
        return f"{_num_token_to_words(amount)} {scale} dollars"

    whole = int(whole_raw)
    cents = int(cents_raw) if cents_raw else None
    dollars_part = f"{_int_to_words(whole)} dollar{'s' if whole != 1 else ''}"
    if cents:
        cents_part = f"{_int_to_words(cents)} cent{'s' if cents != 1 else ''}"
        if whole == 0:
            return cents_part
        return f"{dollars_part} and {cents_part}"
    return dollars_part


# ── Percent / math symbols ──────────────────────────────────────────────────

_PERCENT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?%")

_MATH_REPLACEMENTS = [
    # symbol between numbers/space contexts only, to avoid mangling prose
    (re.compile(r"(?<=[\d\s])\+(?=[\s\d])"), " plus "),
    # bare ascii "x" only when clearly an operator (spaces on both sides);
    # "1920x1080", "x64", "4x4" style tokens are left alone.
    (re.compile(r"(?<=\d) [x] (?=\d)"), " times "),
    (re.compile(r"(?<=\d)\s?[×]\s?(?=\d)"), " times "),
    (re.compile(r"(?<=\d) \* (?=\d)"), " times "),
    (re.compile(r"(?<=\d)\s?÷\s?(?=\d)"), " divided by "),
    (re.compile(r"(?<=\d)\s?/\s?(?=\d)"), " divided by "),
    (re.compile(r"(?<=[\d\s])=(?=[\s\d])"), " equals "),
    (re.compile(r"≈"), " approximately "),
    (re.compile(r"≠"), " is not equal to "),
    (re.compile(r"≤"), " is at most "),
    (re.compile(r"≥"), " is at least "),
    (re.compile(r"(?<=[\d\s])<(?=[\s\d])"), " is less than "),
    (re.compile(r"(?<=[\d\s])>(?=[\s\d])"), " is greater than "),
    (re.compile(r"±"), " plus or minus "),
    (re.compile(r"(?<=\d)\s?\^\s?(?=\d)"), " to the power of "),
    (re.compile(r"√"), " square root of "),
    (re.compile(r"°F\b"), " degrees Fahrenheit"),
    (re.compile(r"°C\b"), " degrees Celsius"),
    (re.compile(r"°"), " degrees"),
]

# number range "5-10" (digit hyphen digit) → "5 to 10".
# Chained hyphen groups (dates 2026-07-18, phone 555-123-4567, UUID-ish
# tokens) must NOT be rewritten — only a single, standalone digit-digit pair.
_RANGE_RE = re.compile(
    r"(?<![\d-])(\d{1,4})\s?[-–—]\s?(\d{1,4})(?![\d-]|\s?[-–—])"
)

# ── Abbreviations ───────────────────────────────────────────────────────────

_ABBREVIATIONS = [
    (re.compile(r"\be\.g\.,?", re.I), "for example,"),
    (re.compile(r"\bi\.e\.,?", re.I), "that is,"),
    (re.compile(r"\betc\.", re.I), "et cetera."),
    (re.compile(r"\bvs\b\.?", re.I), "versus"),
    (re.compile(r"\bapprox\.", re.I), "approximately"),
    (re.compile(r"\bw/o\b", re.I), "without"),
    (re.compile(r"\bw/\s", re.I), "with "),
    (re.compile(r"\bhrs?\b", re.I), "hours"),
    (re.compile(r"\bmins?\b(?!\.)", re.I), "minutes"),
    (re.compile(r"\bsecs?\b", re.I), "seconds"),
    (re.compile(r"(?<=\d)\s?mph\b", re.I), " miles per hour"),
    (re.compile(r"(?<=\d)\s?km/h\b", re.I), " kilometers per hour"),
    (re.compile(r"(?<=\d)\s?kg\b"), " kilograms"),
    (re.compile(r"(?<=\d)\s?lbs?\b", re.I), " pounds"),
    (re.compile(r"(?<=\d)\s?oz\b", re.I), " ounces"),
    (re.compile(r"(?<=\d)\s?km\b"), " kilometers"),
    (re.compile(r"(?<=\d)\s?mi\b"), " miles"),
    (re.compile(r"(?<=\d)\s?ft\b"), " feet"),
    (re.compile(r"(?<=\d)\s?(TB|tb)\b"), " terabytes"),
    (re.compile(r"(?<=\d)\s?(GB|gb)\b"), " gigabytes"),
    (re.compile(r"(?<=\d)\s?(MB|mb)\b"), " megabytes"),
    (re.compile(r"(?<=\d)\s?(KB|kb)\b"), " kilobytes"),
    (re.compile(r"(?<=\d)\s?(GHz|ghz)\b"), " gigahertz"),
    (re.compile(r"(?<=\d)\s?(MHz|mhz)\b"), " megahertz"),
    (re.compile(r"(?<=\d)\s?(ms)\b"), " milliseconds"),
]

# ── Standalone numbers ──────────────────────────────────────────────────────

# Only spell out numbers that TTS tends to garble: anything with a comma
# separator, a decimal point, or 5+ digits. Small integers (years, counts,
# "call me at 3") are pronounced fine as digits and read more naturally.
_BIG_NUM_RE = re.compile(
    r"(?<![\w.\-])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{5,})(?![\w.\-])"
)

# ordinals like 1st, 2nd, 3rd, 4th — leave alone (TTS handles), but
# "21st-century" style hyphens are fine too.


def normalize_for_speech(text: str) -> str:
    """Rewrite money/math/number/abbreviation tokens into speakable words.

    Safe on arbitrary prose: URLs and code should already be stripped by the
    caller's markdown sanitizer; this only rewrites well-delimited tokens.
    """
    if not text:
        return text

    out = _MONEY_RE.sub(_money_repl, text)
    out = _PERCENT_RE.sub(
        lambda m: f"{_num_token_to_words(m.group(1))} percent", out
    )
    out = _RANGE_RE.sub(r"\1 to \2", out)
    for pattern, repl in _MATH_REPLACEMENTS:
        out = pattern.sub(repl, out)
    for pattern, repl in _ABBREVIATIONS:
        out = pattern.sub(repl, out)
    out = _BIG_NUM_RE.sub(lambda m: _num_token_to_words(m.group(1)), out)
    # collapse doubled spaces introduced by the replacements
    out = re.sub(r" {2,}", " ", out)
    return out.strip()
