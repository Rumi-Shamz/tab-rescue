"""Find browser profiles on this machine."""

from __future__ import annotations

from pathlib import Path


def classify_chromium(path: Path) -> str:
    s = str(path)
    if "BraveSoftware" in s or "Brave-Browser" in s:
        return "brave"
    if "google-chrome" in s:
        return "chrome"
    return "chromium"


def is_chromium_profile(p: Path) -> bool:
    return p.is_dir() and ((p / "Bookmarks").is_file() or (p / "Sessions").is_dir())


def is_firefox_profile(p: Path) -> bool:
    return p.is_dir() and (
        (p / "places.sqlite").is_file()
        or (p / "sessionstore-backups").is_dir()
        or (p / "sessionstore.jsonlz4").is_file()
    )


def _expand(home: Path, pattern: str) -> list[Path]:
    return sorted(p for p in home.glob(pattern))


FIREFOX_PATTERNS = (
    ".config/mozilla/firefox/*/",
    ".mozilla/firefox/*/",
    ".var/app/org.mozilla.firefox/.mozilla/firefox/*/",
)
CHROMIUM_PATTERNS = (
    ".config/BraveSoftware/Brave-Browser/*/",
    ".config/chromium/*/",
    ".config/google-chrome/*/",
    ".var/app/*/config/BraveSoftware/Brave-Browser/*/",
    ".var/app/*/config/chromium/*/",
    ".var/app/*/config/google-chrome/*/",
)


def discover(home: Path | None = None) -> tuple[list[Path], list[tuple[str, Path]]]:
    """Return ``(firefox_profiles, [(browser, chromium_profile), ...])``."""
    home = home or Path.home()
    ff: list[Path] = []
    for pat in FIREFOX_PATTERNS:
        for p in _expand(home, pat):
            if is_firefox_profile(p) and p not in ff:
                ff.append(p)
    ch: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for pat in CHROMIUM_PATTERNS:
        for p in _expand(home, pat):
            if p in seen or not is_chromium_profile(p):
                continue
            seen.add(p)
            ch.append((classify_chromium(p), p))
    return ff, ch
