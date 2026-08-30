"""
Unit Tests for InboxEngine.
Validates email text extraction, quote stripping, alert loop suppression,
and correlation of maintainer feedback with Firestore PR memory.
"""

from email.message import EmailMessage

import pytest

from src.core.firestore_client import OfflineFirestoreClient
from src.engines.inbox_engine import (
    InboxEngine,
    clean_reply_quotes,
    decode_mime_header,
    extract_text_from_email_message,
)


class TestEmailParsingAndCleaning:
    def test_clean_reply_quotes(self):
        body = """
        Thanks for the PR. Can you fix the test case in test_auth.py?
        > On 2026-08-28T12:00:00, engineer wrote:
        > I opened the PR for issue #42.
        > Let me know what you think.
        """
        cleaned = clean_reply_quotes(body)
        assert "Thanks for the PR. Can you fix the test case in test_auth.py?" in cleaned
        assert ">" not in cleaned
        assert "On 2026-08-28T12:00:00" not in cleaned

    def test_extract_text_from_plain_email(self):
        msg = EmailMessage()
        msg.set_content("Hello from maintainer regarding issue #101.")
        msg["Subject"] = "Feedback on PR #105"
        text = extract_text_from_email_message(msg)
        assert "Hello from maintainer" in text

    def test_extract_text_from_multipart_email(self):
        msg = EmailMessage()
        msg["Subject"] = "Multipart email"
        msg.set_content("Plain text body content")
        msg.add_alternative("<p>HTML content</p>", subtype="html")
        text = extract_text_from_email_message(msg)
        assert "Plain text body content" in text

    def test_decode_mime_header(self):
        assert decode_mime_header("Simple subject") == "Simple subject"
        # RFC 2047 encoded subject
        encoded = "=?utf-8?b?UmV2aWV3IHJlcXVlc3RlZA==?="
        assert decode_mime_header(encoded) == "Review requested"


class TestInboxEngineCorrelation:
    @pytest.fixture
    def inbox_engine(self, tmp_path):
        db = OfflineFirestoreClient(state_dir=tmp_path / "firestore")
        return InboxEngine(db=db)

    def test_suppress_alert_loopback_emails(self, inbox_engine):
        res = inbox_engine.process_email_record(
            subject="[Bounty Engine ALERT] High CPU warning",
            body="CPU quota exceeded",
        )
        assert res["suppressed"] is True
        assert len(res["correlated_prs"]) == 0

    def test_correlate_maintainer_feedback_to_open_pr(self, inbox_engine):
        # Create a PR memory doc in Firestore
        doc_id = "stellar_example_42"
        inbox_engine.db.collection(inbox_engine.memory_collection).document(doc_id).set({
            "repo": "stellar/example",
            "pr_number": 105,
            "issue_number": 42,
            "title": "Fix Soroban Token",
            "inbox_notifications": [],
        })

        # Process incoming email mentioning repo and PR number
        res = inbox_engine.process_email_record(
            subject="[stellar/example] Changes requested on PR #105",
            body="Please update the error handler in src/lib.rs.\n> On yesterday, author wrote: ...",
            sender="maintainer@stellar.org",
        )

        assert res["suppressed"] is False
        assert doc_id in res["correlated_prs"]

        # Verify Firestore memory was updated
        updated_doc = (
            inbox_engine.db.collection(inbox_engine.memory_collection)
            .document(doc_id)
            .get()
            .to_dict()
        )
        assert updated_doc["has_unread_feedback"] is True
        assert len(updated_doc["inbox_notifications"]) == 1
        assert "Please update the error handler" in updated_doc["inbox_notifications"][0]["body_snippet"]

    def test_unconfigured_credentials_graceful_return(self, inbox_engine):
        inbox_engine.gmail_user = None
        inbox_engine.gmail_app_password = None
        assert inbox_engine.is_configured() is False
        res = inbox_engine.fetch_unread_emails()
        assert res == []
