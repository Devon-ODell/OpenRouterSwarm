"""Small, explicit policy layer for synthetic public personas."""

import re


class SafetyError(ValueError):
    """Raised when a request crosses a product safety boundary."""


_BLOCKED = re.compile(
    r"\b(child|kid|minor|underage|schoolgirl|schoolboy|nude|naked|explicit|porn|onlyfans)\b",
    re.IGNORECASE,
)
_REAL_PERSON = re.compile(
    r"\b(?:look(?:s)? exactly like|clone|impersonate|deepfake of|identical to)\b",
    re.IGNORECASE,
)


def clean_text(value, field, *, limit=2000, required=False):
    if not isinstance(value, str):
        raise SafetyError(f"{field} must be text")
    value = " ".join(value.strip().split())
    if required and not value:
        raise SafetyError(f"{field} is required")
    if len(value) > limit:
        raise SafetyError(f"{field} must be {limit} characters or fewer")
    return value


def validate_creative_text(*values):
    text = " ".join(v for v in values if isinstance(v, str))
    if _BLOCKED.search(text):
        raise SafetyError("Sexual content and minor-coded personas are not supported")
    if _REAL_PERSON.search(text):
        raise SafetyError("Create an original persona; real-person impersonation is not supported")


def safe_handle(value):
    value = clean_text(value, "handle", limit=30, required=True).lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_.]{2,30}", value):
        raise SafetyError("handle must use 2–30 letters, numbers, underscores, or periods")
    return value
