"""Scan files eligible for Git commit without printing secret values."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PARTS = {".venv", "venv", "__pycache__", "work", "deliverables"}
FORBIDDEN_NAMES = {".env"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key"}
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".cmd",
    ".bat",
    ".vbs",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".example",
}

PATTERNS = (
    ("OpenAI-style API token", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("Feishu App ID", re.compile(r"cli_[A-Za-z0-9]{10,}")),
    ("Feishu user/chat ID", re.compile(r"(?:ou|oc)_[A-Za-z0-9]{10,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "Private key block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "Literal credential assignment",
        re.compile(
            r"(?i)\b(?:api_key|app_secret|access_token|client_secret|password)\b"
            r"\s*[:=]\s*[\"']([^\"']{8,})[\"']"
        ),
    ),
)


def git_candidates() -> list[Path] | None:
    if not (ROOT / ".git").exists():
        return None
    completed = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [ROOT / line for line in completed.stdout.splitlines() if line]


def local_candidates() -> list[Path]:
    result = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in FORBIDDEN_PARTS or part == ".git" for part in relative.parts):
            continue
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            continue
        result.append(path)
    return result


def is_placeholder(value: str) -> bool:
    lowered = value.lower()
    for prefix in ("sk-", "cli_", "ou_", "oc_"):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :]
            break
    compact = lowered.replace("-", "").replace("_", "")
    return bool(compact) and (set(compact) <= {"x"} or "your" in compact or "example" in compact)


def main() -> int:
    candidates = git_candidates()
    mode = "Git candidate files" if candidates is not None else "local pre-Git files"
    if candidates is None:
        candidates = local_candidates()

    findings: list[tuple[str, int, str]] = []
    for path in candidates:
        relative = path.relative_to(ROOT)
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append((str(relative), 0, "forbidden local/runtime file"))
            continue
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            findings.append((str(relative), 0, "forbidden generated/private directory"))
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".gitignore":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS:
                for match in pattern.finditer(line):
                    value = match.group(1) if match.lastindex else match.group(0)
                    if is_placeholder(value):
                        continue
                    findings.append((str(relative), line_number, label))

    print(f"Security scan mode: {mode}")
    if findings:
        print("Potential secret risks found (values intentionally hidden):")
        for path, line, label in findings:
            location = f"{path}:{line}" if line else path
            print(f"- {location} | {label}")
        return 1

    print(f"Security check passed: {len(candidates)} candidate files scanned; no secret pattern found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
