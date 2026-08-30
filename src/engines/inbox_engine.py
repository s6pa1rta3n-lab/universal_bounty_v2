"""
Inbox Engine — Gmail IMAP Feedback Loop, Maintainer Notice Ingestion & PR Correlation.

Connects to Gmail IMAP, searches UNSEEN messages, extracts plain-text MIME content,
strips reply quotes, suppresses [Bounty Engine ALERT] loopback emails, correlates maintainer
feedback / CI failure notifications with active PR records in `bounty_memory`, updates PR
lifecycle state, and marks processed messages as \\Seen.
"""

from __future__ import annotations

import email
import logging
import os
from datetime import datetime, timezone
from email.header import decode_header
from typing import Any

try:
    from imapclient import IMAPClient
except ImportError:
    IMAPClient = None

from src.core.config import COLLECTION_BOUNTY_MEMORY
from src.core.firestore_client import get_firestore_client
from src.core.path_guard import DEFAULT_PATH_GUARD, PathGuard

logger = logging.getLogger("UniversalBountyV2.InboxEngine")


def extract_text_from_email_message(msg: email.message.Message) -> str:
    """
    Extracts decoded plain-text content from a MIME email message.
    """
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdispo = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in cdispo:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        return payload.decode(charset, errors="replace")
                    except Exception:
                        return payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except Exception:
                return payload.decode("utf-8", errors="replace")
    return ""


def clean_reply_quotes(text: str) -> str:
    """
    Strips email quotation lines (e.g. '>', 'On ... wrote:', etc.).
    """
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        trimmed = line.strip()
        if (
            trimmed.startswith(">")
            or trimmed.startswith("On ")
            or "wrote:" in trimmed
            or trimmed.startswith("--- Original Message ---")
        ):
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    return cleaned if cleaned else text.strip()


def decode_mime_header(header_val: str | None) -> str:
    """
    Decodes RFC 2047 encoded email headers into unicode strings.
    """
    if not header_val:
        return ""
    try:
        decoded_fragments = decode_header(header_val)
        parts = []
        for fragment, encoding in decoded_fragments:
            if isinstance(fragment, bytes):
                parts.append(fragment.decode(encoding or "utf-8", errors="replace"))
            else:
                parts.append(str(fragment))
        return "".join(parts)
    except Exception:
        return str(header_val)


class InboxEngine:
    """
    Modular Inbox Engine for Milestone 4.
    Drains unread maintainer/CI feedback from Gmail IMAP, correlates with PRs in `bounty_memory`,
    and flags necessary action items.
    """

    def __init__(
        self,
        db: Any | None = None,
        gmail_user: str | None = None,
        gmail_app_password: str | None = None,
        imap_server: str = "imap.gmail.com",
        memory_collection: str = COLLECTION_BOUNTY_MEMORY,
        path_guard: PathGuard | None = None,
    ):
        self.db = db if db is not None else get_firestore_client()
        self.path_guard = path_guard or DEFAULT_PATH_GUARD
        self.gmail_user = gmail_user if gmail_user is not None else os.getenv("GMAIL_USER")
        self.gmail_app_password = (
            gmail_app_password
            if gmail_app_password is not None
            else os.getenv("GMAIL_APP_PASSWORD")
        )
        self.imap_server = imap_server
        self.memory_collection = memory_collection

    def is_configured(self) -> bool:
        """Checks if IMAP credentials are present."""
        return bool(self.gmail_user and self.gmail_app_password)

    def correlate_email_to_prs(
        self,
        subject: str,
        body: str,
        sender: str = "",
    ) -> list[tuple[str, dict[str, Any]]]:
        """
        Correlates an email to open PRs in `bounty_memory`.
        Searches for issue/PR numbers (#123, pull/123) and repository names.
        Returns list of (doc_id, doc_data) matching the email.
        """
        combined = f"{subject} {body} {sender}".lower()
        matches: list[tuple[str, dict[str, Any]]] = []

        try:
            col_ref = self.db.collection(self.memory_collection)
            snaps = list(col_ref.stream())

            for snap in snaps:
                data = snap.to_dict() or {}
                repo = str(data.get("repo", "")).lower()
                pr_num = str(data.get("pr_number") or data.get("number") or "")
                issue_num = str(data.get("issue_number") or "")

                repo_matched = bool(repo and repo in combined)
                pr_matched = bool(
                    pr_num
                    and (
                        f"#{pr_num}" in combined
                        or f"pull/{pr_num}" in combined
                        or f"pr #{pr_num}" in combined
                        or f"pr/{pr_num}" in combined
                    )
                )
                issue_matched = bool(
                    issue_num
                    and (
                        f"#{issue_num}" in combined
                        or f"issues/{issue_num}" in combined
                        or f"issue #{issue_num}" in combined
                    )
                )

                if (repo_matched and (pr_matched or issue_matched)) or (
                    pr_matched and issue_matched
                ):
                    matches.append((snap.id, data))
        except Exception as e:
            logger.warning(f"Error querying {self.memory_collection} for email correlation: {e}")

        return matches

    def process_email_record(
        self,
        subject: str,
        body: str,
        sender: str = "",
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Processes a single extracted email record, strips quotes, filters alerts,
        and correlates with Firestore memory.
        """
        # Anti-loop suppression: ignore alert emails sent by our own engine
        if subject.startswith("[Bounty Engine ALERT]") or "[bounty engine alert]" in subject.lower():
            logger.debug(f"Suppressed loopback alert email: {subject}")
            return {"subject": subject, "suppressed": True, "correlated_prs": []}

        clean_body = clean_reply_quotes(body)
        matched_prs = self.correlate_email_to_prs(subject, clean_body, sender)

        now_iso = datetime.now(timezone.utc).isoformat()
        notice_payload = {
            "subject": subject,
            "sender": sender,
            "body_snippet": clean_body[:1000],
            "received_at_iso": now_iso,
            "message_id": message_id,
        }

        correlated_doc_ids = []
        for doc_id, pr_data in matched_prs:
            try:
                doc_ref = self.db.collection(self.memory_collection).document(doc_id)
                inbox_list = pr_data.get("inbox_notifications", [])
                inbox_list.append(notice_payload)

                update_data = {
                    "inbox_notifications": inbox_list,
                    "has_unread_feedback": True,
                    "last_feedback_at_iso": now_iso,
                    "updated_at_iso": now_iso,
                }
                doc_ref.update(update_data)
                correlated_doc_ids.append(doc_id)
                logger.info(f"[Inbox] Correlated email to PR memory record: {doc_id}")
            except Exception as e:
                logger.error(f"Error updating PR memory {doc_id} with inbox notice: {e}")

        return {
            "subject": subject,
            "sender": sender,
            "clean_body": clean_body,
            "suppressed": False,
            "correlated_prs": correlated_doc_ids,
        }

    def fetch_unread_emails(self, mark_seen: bool = True) -> list[dict[str, Any]]:
        """
        Connects to Gmail IMAP, fetches UNSEEN emails, and marks them as \\Seen.
        Returns list of processed email dictionaries.
        """
        if not self.is_configured():
            logger.info("Gmail IMAP credentials not configured. Skipping live IMAP fetch.")
            return []

        if IMAPClient is None:
            logger.warning("imapclient library not installed; operating in fallback mode.")
            return []

        processed_emails: list[dict[str, Any]] = []

        try:
            with IMAPClient(self.imap_server) as client:
                client.login(self.gmail_user, self.gmail_app_password)
                client.select_folder("INBOX")
                messages = client.search("UNSEEN")
                logger.info(f"Found {len(messages)} unread messages in INBOX.")

                if not messages:
                    return []

                fetch_data = client.fetch(messages, ["RFC822"])
                for uid, msg_data in fetch_data.items():
                    raw_bytes = msg_data.get(b"RFC822")
                    if not raw_bytes:
                        continue

                    email_msg = email.message_from_bytes(raw_bytes)
                    subject = decode_mime_header(email_msg.get("Subject"))
                    sender = decode_mime_header(email_msg.get("From"))
                    msg_id = str(email_msg.get("Message-ID") or uid)

                    body = extract_text_from_email_message(email_msg)
                    rec = self.process_email_record(
                        subject=subject,
                        body=body,
                        sender=sender,
                        message_id=msg_id,
                    )
                    processed_emails.append(rec)

                    if mark_seen:
                        client.add_flags(uid, "\\Seen")

        except Exception as e:
            logger.warning(f"Error during Gmail IMAP sweep: {e}")

        return processed_emails

    def run_sweep(self) -> dict[str, Any]:
        """Runs single inbox drainage sweep."""
        logger.info("Executing InboxEngine sweep...")
        emails = self.fetch_unread_emails(mark_seen=True)
        return {
            "processed_count": len(emails),
            "processed_emails": emails,
        }
