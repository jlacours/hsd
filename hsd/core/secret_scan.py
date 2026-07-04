"""Secret scanning — reject writes containing likely secrets."""

import re

# Conservative patterns that indicate secrets in text
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("PEM private key", re.compile(r"-----BEGIN\s+[A-Z\s]+\s+KEY-----")),
    ("GitHub PAT (ghp_)", re.compile(r"ghp_[a-zA-Z0-9]{36,}")),
    ("GitHub PAT (github_pat_)", re.compile(r"github_pat_[a-zA-Z0-9_]{22,}")),
    ("OpenAI API key (sk-)", re.compile(r"sk-[a-zA-Z0-9]{20,}")),
    ("Anthropic API key (sk-ant-)", re.compile(r"sk-ant-[a-zA-Z0-9]{20,}")),
    ("AWS access key (AKIA)", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("JWT-like token", re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{8,}")),
    ("Slack bot token (xoxb-)", re.compile(r"xoxb-[a-zA-Z0-9]{10,}")),
    ("Slack webhook (hooks.slack.com)", re.compile(r"hooks\.slack\.com/services/T[a-zA-Z0-9_]+/B[a-zA-Z0-9_]+/[a-zA-Z0-9_]+")),
    ("Generic token: secret/private key", re.compile(r"(?:secret|private.?key|password|token|api.?key)\s*[:=]\s*['\"]?[a-zA-Z0-9_\-./]{16,}", re.IGNORECASE)),
]


def scan_text(text: str) -> list[str]:
    """Scan text for secret patterns. Returns list of pattern names matched."""
    hits: list[str] = []
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            hits.append(name)
    return hits


def scan_dict(data: dict[str, str]) -> dict[str, list[str]]:
    """Scan all values in a dict and return field -> [pattern names]."""
    results: dict[str, list[str]] = {}
    for key, value in data.items():
        hits = scan_text(value)
        if hits:
            results[key] = hits
    return results


def validate_no_secrets(text: str) -> tuple[bool, str]:
    """Check text for secrets. Returns (ok, error_message)."""
    hits = scan_text(text)
    if hits:
        return False, (
            f"Secret scan blocked write: detected {', '.join(hits)}. "
            "Redact the value and reference it via an environment variable instead. "
            "No override is available."
        )
    return True, ""


SECTION_VALIDATORS: dict[str, str] = {
    "summary_for_review": "review-ready summary",
    "work_completed": "completed work",
    "commands_verification": "verification results",
    "next_actions": "ordered next actions",
}
