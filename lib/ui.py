"""Command-line replacements for the original VBScript dialog boxes.

Everything here degrades to sensible non-interactive defaults when stdin is not
a TTY or when error-silencing is enabled, so the tool can run unattended (the
original's ``/silence:1`` / ``/silence:2`` switches).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def notice(message: str) -> None:
    """Informational box -> printed banner."""
    print("\n" + message + "\n", flush=True)


def confirm(message: str, default: bool = False) -> bool:
    """Yes/No prompt.  Returns ``default`` when non-interactive."""
    if not _interactive():
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(message + suffix).strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans[0] == "y"


def prompt_text(message: str, default: str = "") -> str:
    if not _interactive():
        return default
    try:
        ans = input(message + " ").strip()
    except EOFError:
        return default
    return ans or default


def prompt_folder(message: str) -> Optional[str]:
    """Replacement for pickfolder.vbs's BrowseForFolder dialog."""
    notice(message)
    path = prompt_text("Path to scan:")
    return path or None


@dataclass
class ClaimInfo:
    identifier: Optional[str]   # private contact, or None to keep files local
    public_cred: str            # public credit name
    send_ids: bool              # consent to share encrypted video ids


def ask_for_name(num_assets: int, num_videos: int) -> ClaimInfo:
    """Replacement for askforname.vbs.

    Asks whether the user wants to claim their verified findings and provide
    contact info.  Non-interactive runs keep everything local (no identifier,
    no id sharing) — the privacy-preserving default.
    """
    if not _interactive():
        return ClaimInfo(identifier=None, public_cred="none provided", send_ids=False)

    if num_assets > 0:
        notice(f"Decache verified {num_assets} piece(s) of lost media — see the "
               f"\"Verified\" folder.")
        identifier = prompt_text(
            "To claim these as your findings, enter contact info (email/Discord/"
            "anonymous), or leave blank to keep them local:")
        if not identifier:
            return ClaimInfo(identifier=None, public_cred="none provided", send_ids=False)
        public = prompt_text("Public credit name (blank for none):") or "none provided"
        send_ids = False
        if num_videos > 0:
            send_ids = confirm(
                "Encrypt and share the IDs of cached videos (lets us notify you "
                "if one becomes a known lost video)?", default=False)
        return ClaimInfo(identifier=identifier, public_cred=public, send_ids=send_ids)

    if num_videos > 0:
        notice("Decache could not verify any lost media on this computer.")
        identifier = prompt_text(
            f"Share encrypted IDs of the {num_videos} cached videos (in case one "
            "is added to Decache later)? Enter contact info, or leave blank to "
            "skip:")
        if identifier:
            return ClaimInfo(identifier=identifier, public_cred="none provided", send_ids=True)
        return ClaimInfo(identifier=None, public_cred="none provided", send_ids=False)

    notice("Decache could not verify any lost media on this computer.")
    return ClaimInfo(identifier=None, public_cred="none provided", send_ids=False)
