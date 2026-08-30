"""
Empirical Adversarial Stress & Chaos Test Suite for Engine Tier (Universal Bounty V2).

Target Engines:
- IntakeEngine: Currency extraction, Sniper Filter, Banned Platforms, KYC Disqualifiers, Anti-Spam Load Balancer.
- InboxEngine: MIME decoding, Non-UTF8 charsets, quote stripping, alert loop suppression, PR correlation.
- EscortEngine: External deployment gate filtering (Vercel/Netlify/Cloudflare), real CI failure detection, staleness & bump logic.
- SyncEngine: Settlement qualification, conflicting payout resolution, idempotency, coordinator ledger aggregation.
"""

from __future__ import annotations

import email
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from src.core.config import (
    COLLECTION_BOUNTY_LEADS,
    COLLECTION_BOUNTY_MEMORY,
    COLLECTION_BOUNTY_SETTLEMENTS,
    COLLECTION_SWARM_COORDINATOR,
    EVM_PAYOUT_ADDRESS,
)
from src.core.firestore_client import OfflineFirestoreClient
from src.core.safe_io import SafeIO
from src.engines.escort_engine import EscortEngine, is_external_deployment_gate
from src.engines.inbox_engine import (
    InboxEngine,
    clean_reply_quotes,
    decode_mime_header,
    extract_text_from_email_message,
)
from src.engines.intake_engine import (
    IntakeEngine,
    clean_text_for_financials,
    extract_financials,
    generate_canonical_doc_id,
    verify_escrow,
)
from src.engines.sync_engine import SyncEngine, extract_payout_numeric

# =============================================================================
# 1. IntakeEngine: Currency Extraction & Financial Cleaning Stress Tests
# =============================================================================


class TestIntakeEngineCurrencyEdgeCases:
    """Stress tests extract_financials against exotic, malformed, and conflicting currency strings."""

    def test_standard_and_exotic_dollar_formats(self):
        """Verify dollar amounts formatted with prefix, postfix, spaces, decimals, and commas."""
        cases = [
            ("$500", 500.0, "$500.00"),
            ("$ 1,500.50", 1500.50, "$1500.50"),
            ("10,000$", 10000.0, "$10000.00"),
            ("250.00 $", 250.0, "$250.00"),
            ("$1,000,000.00", 1000000.0, "$1000000.00"),
            ("Reward: $42.99 for completion", 42.99, "$42.99"),
        ]
        for text, expected_val, expected_str in cases:
            p_str, p_val = extract_financials(text)
            assert p_val == expected_val, f"Failed on '{text}'"
            assert p_str == expected_str, f"Failed on '{text}'"

    def test_crypto_token_formats_prefix_and_postfix(self):
        """Verify crypto tokens (USDC, XLM, ETH, SOL, ARB, OP, DAI) in prefix and postfix positions."""
        cases = [
            ("500 USDC", 500.0, "500.0 USDC"),
            ("1,200.50 XLM", 1200.50, "1200.5 XLM"),
            ("0.5 ETH", 0.5, "0.5 ETH"),
            ("USDC 2500", 2500.0, "2500.0 USDC"),
            ("ETH 2.0", 2.0, "2.0 ETH"),
            ("10000 MATIC", 10000.0, "10000.0 MATIC"),
            ("150 DAI", 150.0, "150.0 DAI"),
        ]
        for text, expected_val, expected_str in cases:
            p_str, p_val = extract_financials(text)
            assert p_val == expected_val, f"Failed token parsing for '{text}'"
            assert p_str == expected_str, f"Failed token string for '{text}'"

    def test_multiple_amounts_selects_maximum_value(self):
        """Verify that when multiple reward amounts appear, the highest valid value is extracted."""
        text = "Initial bounty was $100. Then funder added $500, and finally total escrow is $1,250.00!"
        p_str, p_val = extract_financials(text)
        assert p_val == 1250.00
        assert p_str == "$1250.00"

        text_token = "Base reward: 50 USDC. Bonus pool: 300 USDC. Total pot: 750 USDC."
        p_str, p_val = extract_financials(text_token)
        assert p_val == 750.0
        assert p_str == "750.0 USDC"

    def test_promotional_footer_and_link_stripping(self):
        """Verify promotional text containing high numbers (e.g. $10,000 gitcoin explorer links) is cleanly ignored."""
        text = (
            "Fix the memory leak in soroban-sdk storage module.\n"
            "Bounty reward: $350.00 for verified fix.\n\n"
            "---\n"
            "More funded OSS work available on Gitcoin!\n"
            "Explore https://gitcoin.co/explorer with over $100,000 in bounties."
        )
        cleaned = clean_text_for_financials(text)
        assert "$100,000" not in cleaned
        assert "$350.00" in cleaned

        p_str, p_val = extract_financials(text)
        assert p_val == 350.0
        assert p_str == "$350.00"

    def test_empty_none_and_non_monetary_text(self):
        """Verify non-monetary text, empty strings, and None safely yield PENDING DISCOVERY."""
        assert extract_financials("") == ("PENDING DISCOVERY", 0.0)
        assert extract_financials(None) == ("PENDING DISCOVERY", 0.0)
        assert extract_financials("Just an open source feature request without funds") == (
            "PENDING DISCOVERY",
            0.0,
        )


# =============================================================================
# 2. IntakeEngine: Sniper Filter, Banned Platforms & Disqualifiers
# =============================================================================


class TestIntakeEngineSniperFilterDisqualifiers:
    """Stress tests verify_escrow against banned platforms, KYC, and cancelled bounties."""

    def test_reject_archived_repositories(self):
        """Archived repositories must be rejected immediately."""
        issue = {
            "repository": {"nameWithOwner": "stellar/archived-project", "isArchived": True},
            "number": 1,
            "title": "$500 Soroban Bounty",
            "body": "Fix contract",
            "labels": [{"name": "bounty"}],
        }
        is_valid, reason, _, _, _, _ = verify_escrow(issue)
        assert not is_valid
        assert reason == "REJECT_ARCHIVED_REPO"

    def test_reject_all_banned_platforms(self):
        """Ensure Algora, Polar, twentyhq/twenty, and Opire are strictly banned across all fields."""
        banned_scenarios = [
            # Algora in author
            {"repo": "org/repo", "number": 1, "author": {"login": "algora-bot"}, "body": "Bounty $100"},
            # Algora URL in body
            {"repo": "org/repo", "number": 2, "body": "Claim on https://algora.io/bounties/123 $500"},
            # Polar in repo name
            {"repo": "polar-sh/polar", "number": 3, "body": "Fix issue $200"},
            # Polar in comments
            {
                "repo": "org/repo",
                "number": 4,
                "body": "Fix issue",
                "comments": [{"body": "Bounty backed by polar.sh with $300 reward"}],
            },
            # TwentyHQ repo
            {"repo": "twentyhq/twenty", "number": 5, "body": "Implement CRM feature $500"},
            # Opire bot in comments
            {
                "repo": "org/repo",
                "number": 6,
                "body": "Bounty $150",
                "comments": [{"author": {"login": "opire-bot"}, "body": "/reward 150"}],
            },
        ]
        for node in banned_scenarios:
            if "repo" in node and "repository" not in node:
                node["repository"] = {"nameWithOwner": node["repo"], "isArchived": False}
            is_valid, reason, _, _, _, _ = verify_escrow(node)
            assert not is_valid, f"Failed to reject banned scenario: {node}"
            assert "REJECT_BANNED_PLATFORM" in reason

    def test_reject_subjective_and_kyc_requirements(self):
        """Reject bounties requiring manual KYC, live video calls, Zoom, Loom, or pure design deliverables."""
        subjective_cases = [
            ("Must pass manual kyc verification before payout", "REJECT_SUBJECTIVE"),
            ("Schedule a 30-min Zoom interview with the founders", "REJECT_SUBJECTIVE"),
            ("Submit a Loom screencast demoing your workflow", "REJECT_SUBJECTIVE"),
            ("Figma only deliverable for UI design", "REJECT_SUBJECTIVE"),
            ("Requires recorded video pitch for judging", "REJECT_SUBJECTIVE"),
            ("Must submit demo video walkthrough of the feature", "REJECT_SUBJECTIVE"),
        ]
        for text, expected_prefix in subjective_cases:
            node = {
                "repository": {"nameWithOwner": "stellar/soroban-app", "isArchived": False},
                "number": 10,
                "title": "Community Bounty $500",
                "body": f"Tasks:\n{text}",
                "labels": [{"name": "bounty"}],
            }
            is_valid, reason, _, _, _, _ = verify_escrow(node)
            assert not is_valid, f"Failed to reject subjective text: {text}"
            assert reason.startswith(expected_prefix)

    def test_reject_cancelled_or_refunded_escrow(self):
        """Reject bounties where maintainers or bots indicated cancellation/refund in comments."""
        cancelled_cases = [
            "Bounty has been cancelled by the author.",
            "The funding has been refunded to the backer.",
            "Bounty reward has been withdrawn due to scope change.",
            "Escrow voided by maintainer.",
        ]
        for comment_text in cancelled_cases:
            node = {
                "repository": {"nameWithOwner": "grantfox/smart-contracts", "isArchived": False},
                "number": 22,
                "title": "GrantFox OSS Bug $400",
                "body": "Fix escrow release",
                "labels": [{"name": "GrantFox OSS"}],
                "comments": [{"body": comment_text}],
            }
            is_valid, reason, _, _, _, _ = verify_escrow(node)
            assert not is_valid, f"Failed to reject cancelled escrow: {comment_text}"
            assert reason == "REJECT_ESCROW_CANCELLED"

    def test_qualify_valid_high_priority_ecosystems(self):
        """Verify legitimate bounties across GrantFox, Stellar, EVM, and Gitcoin qualify as high priority."""
        ecosystem_nodes = [
            (
                {
                    "repository": {"nameWithOwner": "grantfox/core", "isArchived": False},
                    "number": 101,
                    "title": "GrantFox Escrow Integration",
                    "body": "Implement settlement hook. Reward: $1,000.00",
                    "labels": [{"name": "GrantFox OSS"}, {"name": "bounty"}],
                },
                "grantfox",
                "VERIFIED_GRANTFOX_ESCROW",
                1000.0,
            ),
            (
                {
                    "repository": {"nameWithOwner": "stellar/rs-soroban-env", "isArchived": False},
                    "number": 202,
                    "title": "Optimize Soroban Host CPU Meters",
                    "body": "Improve gas metering. Payout: 5000 XLM",
                    "labels": [{"name": "bounty"}, {"name": "stellar"}],
                },
                "stellar",
                "VERIFIED_STELLAR_FUNDING",
                5000.0,
            ),
            (
                {
                    "repository": {"nameWithOwner": "base/contracts", "isArchived": False},
                    "number": 303,
                    "title": "Solidity Bridge Security Patch",
                    "body": "Patch re-entrancy vector on Base L2. Reward: 1.5 ETH",
                    "labels": [{"name": "bounty"}, {"name": "security"}],
                },
                "evm",
                "VERIFIED_EVM_FUNDING",
                1.5,
            ),
        ]
        for node, exp_eco, exp_reason, exp_val in ecosystem_nodes:
            is_valid, reason, p_str, p_val, is_high_prio, eco = verify_escrow(node)
            assert is_valid, f"Failed to qualify valid bounty: {node['title']}"
            assert reason == exp_reason
            assert eco == exp_eco
            assert is_high_prio is True
            assert p_val == exp_val


# =============================================================================
# 3. IntakeEngine: Queue Balancing, Anti-Spam & Concurrency Stress Tests
# =============================================================================


class TestIntakeEngineLoadBalancerAndConcurrency:
    """Stress tests load balancer enforcing max 1 lead per repo and max 4 concurrent active leads."""

    def test_anti_spam_max_one_per_repo(
        self, temp_workspace: Path, offline_db: OfflineFirestoreClient
    ):
        """When 5 queued leads belong to the same repo, only 1 is promoted to active triage."""
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        queue_file = temp_workspace / "intake_queue.jsonl"
        engine = IntakeEngine(
            db=offline_db,
            seen_cache_path=temp_workspace / "seen.json",
            queue_file_path=queue_file,
        )

        for i in range(5):
            doc_id = f"stellar_soroban_sdk_{i+1}"
            col.document(doc_id).set({
                "id": doc_id,
                "repo": "stellar/soroban-sdk",
                "issue_number": i + 1,
                "status": "queued",
                "priority": "high",
                "projected_payout_usd": float(100 * (i + 1)),
            })

        promoted = engine.balance_queue(max_concurrent=4, max_per_repo=1)
        assert len(promoted) == 1
        # The promoted one must be the highest payout ($500)
        assert promoted[0]["id"] == "stellar_soroban_sdk_5"
        assert promoted[0]["status"] == "priority_triage"

        # The other 4 remain in 'queued' status
        remaining_queued = list(col.where("status", "==", "queued").stream())
        assert len(remaining_queued) == 4

    def test_global_concurrency_cap_four_total_active(
        self, temp_workspace: Path, offline_db: OfflineFirestoreClient
    ):
        """When swarm is at capacity (4 active leads), no additional leads are promoted."""
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        queue_file = temp_workspace / "intake_queue.jsonl"
        engine = IntakeEngine(
            db=offline_db,
            seen_cache_path=temp_workspace / "seen.json",
            queue_file_path=queue_file,
        )

        # 4 already active leads in different repos
        active_repos = ["org1/repo1", "org2/repo2", "org3/repo3", "org4/repo4"]
        for idx, r in enumerate(active_repos):
            col.document(f"active_{idx}").set({
                "id": f"active_{idx}",
                "repo": r,
                "status": "running_orbstack" if idx % 2 == 0 else "claimed",
                "priority": "high",
            })

        # 3 new queued leads in separate repos
        for i in range(3):
            col.document(f"new_{i}").set({
                "id": f"new_{i}",
                "repo": f"new_org/new_repo_{i}",
                "status": "queued",
                "priority": "high",
                "projected_payout_usd": 1000.0,
            })

        promoted = engine.balance_queue(max_concurrent=4, max_per_repo=1)
        assert len(promoted) == 0  # Zero promoted because 4 are already active

    def test_partial_capacity_promotion_order(
        self, temp_workspace: Path, offline_db: OfflineFirestoreClient
    ):
        """With 2 active leads, exactly 2 slots are promoted, prioritizing high priority + highest USD."""
        col = offline_db.collection(COLLECTION_BOUNTY_LEADS)
        queue_file = temp_workspace / "intake_queue.jsonl"
        engine = IntakeEngine(
            db=offline_db,
            seen_cache_path=temp_workspace / "seen.json",
            queue_file_path=queue_file,
        )

        # 2 active
        col.document("act_1").set({"repo": "repo_a/a", "status": "pending_triage"})
        col.document("act_2").set({"repo": "repo_b/b", "status": "claimed"})

        # Candidate queue
        candidates = [
            {"id": "q1", "repo": "repo_c/c", "status": "queued", "priority": "standard", "projected_payout_usd": 5000.0},
            {"id": "q2", "repo": "repo_d/d", "status": "queued", "priority": "high", "projected_payout_usd": 200.0},
            {"id": "q3", "repo": "repo_e/e", "status": "queued", "priority": "high", "projected_payout_usd": 800.0},
            {"id": "q4", "repo": "repo_a/a", "status": "queued", "priority": "high", "projected_payout_usd": 9999.0}, # Duplicate repo!
        ]
        for c in candidates:
            col.document(c["id"]).set(c)

        promoted = engine.balance_queue(max_concurrent=4, max_per_repo=1)
        assert len(promoted) == 2
        # Must promote q3 ($800 high) and q2 ($200 high)
        # q4 is skipped because repo_a/a is already active
        # q1 is standard priority so comes after high priority
        promoted_ids = [p["id"] for p in promoted]
        assert promoted_ids == ["q3", "q2"]


# =============================================================================
# 4. InboxEngine: MIME Parsing, Corrupted Payloads & Loop Suppression
# =============================================================================


class TestInboxEngineMIMEDecodingAndLoopSuppression:
    """Stress tests email parsing, corrupted payloads, quote stripping, and loopback alert suppression."""

    def test_multipart_email_with_attachments_and_html(self):
        """Ensure multipart MIME messages with attachments and HTML extract the pure text/plain body."""
        msg = EmailMessage()
        msg["Subject"] = "Issue #42 feedback on stellar/soroban-sdk"
        msg["From"] = "maintainer@stellar.org"
        msg["To"] = "swarm@antigravity.io"
        msg.set_content("Please add comprehensive unit tests for the token host functions.")
        msg.add_alternative(
            "<p>Please add comprehensive unit tests for the <b>token host functions</b>.</p>",
            subtype="html",
        )
        msg.add_attachment(b"%PDF-1.4 binary content mock", maintype="application", subtype="pdf", filename="spec.pdf")

        extracted_text = extract_text_from_email_message(msg)
        assert "Please add comprehensive unit tests" in extracted_text
        assert "<p>" not in extracted_text
        assert "PDF-1.4" not in extracted_text

    def test_non_utf8_charset_decoding_resilience(self):
        """Verify resilient decoding of ISO-8859-1 and Windows-1252 email payloads."""
        raw_iso = (
            b"From: reviewer@example.com\r\n"
            b"Subject: Review on PR #55 in org/repo\r\n"
            b"Content-Type: text/plain; charset=iso-8859-1\r\n"
            b"Content-Transfer-Encoding: 8bit\r\n\r\n"
            b"Le d\xe9veloppeur a valid\xe9 le smart contract.\r\n"
        )
        email_msg = email.message_from_bytes(raw_iso)
        text = extract_text_from_email_message(email_msg)
        assert "Le d\xe9veloppeur a valid\xe9 le smart contract." in text

    def test_rfc2047_encoded_headers(self):
        """Verify RFC 2047 base64 and quoted-printable encoded subject/sender headers."""
        encoded_subject = "=?utf-8?B?U3RlbGxhciBCb3VudHkgUmV3YXJkIPCfmYA=?="
        decoded = decode_mime_header(encoded_subject)
        assert "Stellar Bounty Reward" in decoded

        raw_header = "Plain ASCII Subject"
        assert decode_mime_header(raw_header) == "Plain ASCII Subject"
        assert decode_mime_header(None) == ""

    def test_nested_email_quote_stripping(self):
        """Ensure nested reply quotes (>, >>, On ... wrote) are stripped cleanly while preserving reply text."""
        raw_body = (
            "Thanks for the update. The CI fix looks great!\n"
            "\n"
            "> On Aug 29, 2026, at 10:00 AM, bot wrote:\n"
            "> > Fixes issue #101\n"
            "> > Added automated tests\n"
            "> All checks passing.\n"
            "\n"
            "Please rebase against master and we will merge."
        )
        cleaned = clean_reply_quotes(raw_body)
        assert "Thanks for the update. The CI fix looks great!" in cleaned
        assert "Please rebase against master and we will merge." in cleaned
        assert "> On Aug 29" not in cleaned
        assert "> > Fixes issue" not in cleaned

    def test_all_quoted_email_fallback(self):
        """If an email consists entirely of quoted lines, fallback to returning trimmed content rather than empty string."""
        raw_quoted = "> Entire message was forwarded inside quotes."
        cleaned = clean_reply_quotes(raw_quoted)
        assert cleaned == "> Entire message was forwarded inside quotes."

    def test_alert_loopback_suppression(
        self, temp_workspace: Path, offline_db: OfflineFirestoreClient
    ):
        """Prevent self-generated [Bounty Engine ALERT] emails from triggering infinite processing loops."""
        engine = InboxEngine(db=offline_db)
        alert_subject = "[Bounty Engine ALERT] CI Failure detected on PR #99"
        alert_body = "Automated notification: CI build failed."

        res = engine.process_email_record(
            subject=alert_subject,
            body=alert_body,
            sender="notifications@antigravity.io",
        )
        assert res["suppressed"] is True
        assert res["correlated_prs"] == []

    def test_pr_correlation_and_notification_update(
        self, temp_workspace: Path, offline_db: OfflineFirestoreClient
    ):
        """Correlate maintainer feedback referencing repo and PR number to active record in `bounty_memory`."""
        col = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        col.document("stellar_rs_soroban_env_42").set({
            "repo": "stellar/rs-soroban-env",
            "pr_number": 42,
            "issue_number": 10,
            "has_unread_feedback": False,
            "inbox_notifications": [],
        })

        engine = InboxEngine(db=offline_db)
        email_subj = "Review comments on pull/42 in stellar/rs-soroban-env"
        email_body = "The PR looks solid. Please check lint error in meter.rs."

        res = engine.process_email_record(
            subject=email_subj,
            body=email_body,
            sender="maintainer@stellar.org",
            message_id="<msg_001@stellar.org>",
        )

        assert res["suppressed"] is False
        assert "stellar_rs_soroban_env_42" in res["correlated_prs"]

        # Verify Firestore document was updated with notice and has_unread_feedback=True
        snap = col.document("stellar_rs_soroban_env_42").get()
        assert snap.get("has_unread_feedback") is True
        notices = snap.get("inbox_notifications")
        assert len(notices) == 1
        assert "Please check lint error in meter.rs" in notices[0]["body_snippet"]


# =============================================================================
# 5. EscortEngine: External Deployment Gate Filtering & Staleness Tests
# =============================================================================


class TestEscortEngineDeploymentGatesAndStaleness:
    """Stress tests CI failure filtering (ignoring Vercel/Netlify preview gates) and 14-day inactivity bumps."""

    def test_deployment_gate_detection_heuristics(self):
        """Verify external preview deployment gate name patterns."""
        gates = [
            ("Vercel – Preview Deployment", "Vercel"),
            ("netlify/preview/deploy", "Netlify"),
            ("cloudflare-pages/preview", "Cloudflare"),
            ("Deploy Preview (Cloudflare Pages)", None),
            ("external-deployment-gate", None),
        ]
        for name, app in gates:
            assert is_external_deployment_gate(name, app), f"Failed to detect gate: {name}"

        real_checks = [
            ("build-and-test", "GitHub Actions"),
            ("cargo-clippy", "Rust CI"),
            ("unit-tests (node 20)", "GitHub Actions"),
            ("coverage/coveralls", "Coveralls"),
        ]
        for name, app in real_checks:
            assert not is_external_deployment_gate(name, app), f"False positive gate: {name}"

    def test_pr_health_all_failures_are_preview_gates_classified_as_passing(self):
        """When the only failing check runs are Vercel/Netlify preview gates, PR is classified as SUCCESS_GATES_IGNORED."""
        pr_data = {
            "pr_url": "https://github.com/stellar/soroban-app/pull/15",
            "pr_number": 15,
            "repo": "stellar/soroban-app",
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "statusCheckRollup": {"state": "FAILURE"},
                            "checkSuites": {
                                "nodes": [
                                    {
                                        "app": {"name": "Vercel"},
                                        "checkRuns": {
                                            "nodes": [
                                                {
                                                    "name": "Vercel – Preview Deployment",
                                                    "conclusion": "FAILURE",
                                                }
                                            ]
                                        },
                                    },
                                    {
                                        "app": {"name": "Netlify"},
                                        "checkRuns": {
                                            "nodes": [
                                                {
                                                    "name": "deploy/preview",
                                                    "conclusion": "ACTION_REQUIRED",
                                                }
                                            ]
                                        },
                                    },
                                ]
                            },
                        }
                    }
                ]
            },
        }
        engine = EscortEngine()
        res = engine.inspect_pr_health(pr_data)
        assert res["ci_status"] == "SUCCESS_GATES_IGNORED"
        assert res["needs_ci_fix"] is False
        assert len(res["ignored_gate_failures"]) == 2
        assert len(res["actionable_ci_failures"]) == 0

    def test_pr_health_real_ci_failure_triggers_fix_flag(self):
        """When an actual build or test check fails, PR is flagged with needs_ci_fix=True."""
        pr_data = {
            "pr_url": "https://github.com/stellar/soroban-app/pull/16",
            "pr_number": 16,
            "repo": "stellar/soroban-app",
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "statusCheckRollup": {"state": "FAILURE"},
                            "checkSuites": {
                                "nodes": [
                                    {
                                        "app": {"name": "GitHub Actions"},
                                        "checkRuns": {
                                            "nodes": [
                                                {
                                                    "name": "cargo test --all",
                                                    "conclusion": "FAILURE",
                                                }
                                            ]
                                        },
                                    }
                                ]
                            },
                        }
                    }
                ]
            },
        }
        engine = EscortEngine()
        res = engine.inspect_pr_health(pr_data)
        assert res["ci_status"] == "FAILURE"
        assert res["needs_ci_fix"] is True
        assert res["actionable_ci_failures"] == ["cargo test --all"]

    def test_staleness_and_maintainer_bump_evaluation(self):
        """PRs with >=14 days of inactivity trigger needs_maintainer_bump unless they are drafts."""
        now = datetime.now(timezone.utc)
        # 16 days ago
        old_iso = datetime.fromtimestamp(now.timestamp() - (16 * 86400), tz=timezone.utc).isoformat()

        # Non-draft stalled PR -> needs bump
        pr_ready = {
            "repo": "org/repo",
            "pr_number": 10,
            "updated_at": old_iso,
            "is_draft": False,
        }
        engine = EscortEngine(stale_days_threshold=14)
        res_ready = engine.inspect_pr_health(pr_ready)
        assert res_ready["is_stalled"] is True
        assert res_ready["needs_maintainer_bump"] is True
        assert res_ready["inactivity_days"] >= 16.0

        # Draft stalled PR -> no bump
        pr_draft = {
            "repo": "org/repo",
            "pr_number": 11,
            "updated_at": old_iso,
            "is_draft": True,
        }
        res_draft = engine.inspect_pr_health(pr_draft)
        assert res_draft["is_stalled"] is True
        assert res_draft["needs_maintainer_bump"] is False

        # Active PR (<14 days) -> no bump
        recent_iso = datetime.fromtimestamp(now.timestamp() - (3 * 86400), tz=timezone.utc).isoformat()
        pr_active = {
            "repo": "org/repo",
            "pr_number": 12,
            "updated_at": recent_iso,
            "is_draft": False,
        }
        res_active = engine.inspect_pr_health(pr_active)
        assert res_active["is_stalled"] is False
        assert res_active["needs_maintainer_bump"] is False


# =============================================================================
# 6. SyncEngine: Settlements & Swarm Coordinator Financial Ledger Tests
# =============================================================================


class TestSyncEngineSettlementsAndLedger:
    """Stress tests settlement recording, conflicting transactions, idempotency, and aggregate calculations."""

    def test_payout_extraction_from_various_schemas(self):
        """Verify payout numeric extraction across numeric USD fields, escrow objects, and text."""
        # 1. projected_payout_usd float
        p_str, p_val = extract_payout_numeric({"projected_payout_usd": 450.0})
        assert p_val == 450.0
        assert p_str == "$450.00"

        # 2. escrow dict
        p_str, p_val = extract_payout_numeric({"escrow": {"amount_usd": 1200.0}})
        assert p_val == 1200.0
        assert p_str == "$1200.00"

        # 3. text in reward_tokens
        p_str, p_val = extract_payout_numeric({"reward_tokens": "500 USDC on Base"})
        assert p_val == 500.0
        assert p_str == "500.0 USDC"

    def test_settlement_idempotency_and_recipient_enforcement(
        self, temp_workspace: Path, offline_db: OfflineFirestoreClient
    ):
        """Verify repeated sync passes are idempotent and enforce EVM_PAYOUT_ADDRESS."""
        engine = SyncEngine(db=offline_db)

        # Seed merged PR in bounty_memory
        mem_col = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        mem_col.document("stellar_rs_soroban_env_50").set({
            "repo": "stellar/rs-soroban-env",
            "pr_number": 50,
            "state": "MERGED",
            "projected_payout_usd": 750.0,
            "title": "Soroban Gas Meter Optimization",
        })

        # Run sweep 1
        res1 = engine.sync_settlements()
        assert res1["synced_count"] == 1
        assert res1["total_settled_usd"] == 750.0

        # Verify settlement doc in bounty_settlements
        settle_col = offline_db.collection(COLLECTION_BOUNTY_SETTLEMENTS)
        settle_doc = settle_col.document("settle_stellar_rs_soroban_env_50").get()
        assert settle_doc.exists
        assert settle_doc.get("payout_usd") == 750.0
        assert settle_doc.get("payout_recipient") == EVM_PAYOUT_ADDRESS

        # Run sweep 2 (idempotency check)
        res2 = engine.sync_settlements()
        assert res2["synced_count"] == 1
        # Total settled must remain $750.00 (NOT doubled to $1500)
        assert res2["total_settled_usd"] == 750.0
        assert res2["total_settled_count"] == 1

    def test_multi_lead_settlement_aggregation_and_coordinator_state(
        self, temp_workspace: Path, offline_db: OfflineFirestoreClient
    ):
        """Verify aggregation across multiple merged leads and update to swarm_coordinator/state."""
        engine = SyncEngine(db=offline_db)
        mem_col = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        leads_col = offline_db.collection(COLLECTION_BOUNTY_LEADS)

        # 3 merged items
        mem_col.document("pr_1").set({"repo": "org/r1", "state": "MERGED", "projected_payout_usd": 250.0})
        mem_col.document("pr_2").set({"repo": "org/r2", "status": "completed", "payout_usd": 500.0})
        leads_col.document("lead_3").set({"repo": "org/r3", "status": "completed", "projected_payout_usd": 1250.0})

        # 1 unmerged item (should be ignored)
        mem_col.document("pr_open").set({"repo": "org/r4", "state": "OPEN", "projected_payout_usd": 9999.0})

        sweep_res = engine.sync_settlements()
        assert sweep_res["total_settled_count"] == 3
        assert sweep_res["total_settled_usd"] == 2000.0  # 250 + 500 + 1250

        # Check coordinator state singleton
        coord_doc = offline_db.collection(COLLECTION_SWARM_COORDINATOR).document("state").get()
        assert coord_doc.exists
        assert coord_doc.get("total_settled_usd") == 2000.0
        assert coord_doc.get("total_settled_count") == 3
        assert coord_doc.get("status") == "HEALTHY"
        assert "intake_engine" in coord_doc.get("active_engines")
        assert "escort_engine" in coord_doc.get("active_engines")
        assert "sync_engine" in coord_doc.get("active_engines")


# =============================================================================
# 7. Advanced Engine Tier Chaos & Boundary Harness
# =============================================================================


class TestEngineTierAdvancedChaosAndBoundaries:
    """Stress tests extreme inputs, malformed structures, case sensitivity, and fail-safes across all 4 engines."""

    def test_intake_canonical_doc_id_case_insensitivity(self):
        """Canonical doc IDs must normalize repo names, dots, dashes, and casing identically."""
        id1 = generate_canonical_doc_id("Stellar/RS-Soroban-Env", 42)
        id2 = generate_canonical_doc_id("stellar/rs_soroban_env", "42")
        id3 = generate_canonical_doc_id("stellar/rs.soroban.env", 42)
        assert id1 == "stellar_rs_soroban_env_42"
        assert id2 == "stellar_rs_soroban_env_42"
        assert id3 == "stellar_rs_soroban_env_42"

    def test_intake_engine_full_ingest_and_sync_local_queue(
        self, temp_workspace: Path, offline_db: OfflineFirestoreClient
    ):
        """Verify full ingest pipeline: deduplication via seen cache and sorted local JSONL output."""
        seen_file = temp_workspace / "seen_cache.json"
        queue_file = temp_workspace / "logs" / "intake_queue.jsonl"
        engine = IntakeEngine(
            db=offline_db,
            seen_cache_path=seen_file,
            queue_file_path=queue_file,
        )

        mock_issues = [
            {
                "id": "node_1",
                "repository": {"nameWithOwner": "stellar/soroban-example", "isArchived": False},
                "number": 1,
                "title": "Stellar Example Bounty $300",
                "body": "Fix bug",
                "labels": [{"name": "stellar"}],
            },
            {
                "id": "node_2",
                "repository": {"nameWithOwner": "grantfox/contracts", "isArchived": False},
                "number": 2,
                "title": "GrantFox Escrow $1500",
                "body": "Implement escrow",
                "labels": [{"name": "GrantFox OSS"}],
            },
            {
                "id": "node_1",  # Duplicate node_id
                "repository": {"nameWithOwner": "stellar/soroban-example", "isArchived": False},
                "number": 1,
                "title": "Stellar Example Bounty $300 (Duplicate)",
                "body": "Fix bug",
                "labels": [{"name": "stellar"}],
            },
        ]

        ingested = engine.ingest_bounties(issues=mock_issues)
        assert len(ingested) == 2  # Node 1 and Node 2 only
        assert seen_file.exists()
        assert queue_file.exists()

        # Check local queue file partitioning and sorting
        queue_records = SafeIO.read_jsonl(queue_file)
        assert len(queue_records) == 2
        # $1500 (GrantFox) must be on top of $300 (Stellar)
        assert queue_records[0]["projected_payout_usd"] == 1500.0
        assert queue_records[1]["projected_payout_usd"] == 300.0

    def test_inbox_engine_corrupted_headers_and_payloads(
        self, temp_workspace: Path, offline_db: OfflineFirestoreClient
    ):
        """Verify InboxEngine handles corrupted, non-RFC compliant, or None inputs without crashing."""
        engine = InboxEngine(db=offline_db)

        # 1. None inputs
        res_none = engine.process_email_record(subject="", body="", sender="")
        assert res_none["suppressed"] is False
        assert res_none["clean_body"] == ""

        # 2. Corrupt MIME bytes in extract_text_from_email_message
        corrupt_msg = email.message_from_bytes(b"\x00\xff\xfe\x80\x90")
        extracted = extract_text_from_email_message(corrupt_msg)
        assert isinstance(extracted, str)

        # 3. decode_mime_header with invalid structure
        assert decode_mime_header("=?invalid-encoding?Q?bad?=") == "=?invalid-encoding?Q?bad?="

    def test_escort_engine_malformed_pr_data_resilience(self):
        """Verify EscortEngine handles malformed commits, missing checkSuites, and string dates gracefully."""
        engine = EscortEngine()

        # Malformed commit nodes
        pr_malformed = {
            "pr_url": "https://github.com/org/repo/pull/1",
            "commits": "invalid_not_a_dict_or_list",
            "created_at": "not-a-valid-date",
            "updated_at": None,
        }
        res = engine.inspect_pr_health(pr_malformed)
        assert res["ci_status"] == "UNKNOWN"
        assert res["needs_ci_fix"] is False
        assert res["is_stalled"] is False  # Safe fallback

        # Check run with missing fields
        pr_check_run_none = {
            "pr_url": "https://github.com/org/repo/pull/2",
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "statusCheckRollup": {"state": "FAILURE"},
                            "checkSuites": {
                                "nodes": [
                                    None,
                                    {"app": None, "checkRuns": {"nodes": [None, {"name": None, "conclusion": "FAILURE"}]}},
                                ]
                            },
                        }
                    }
                ]
            },
        }
        res_cr = engine.inspect_pr_health(pr_check_run_none)
        assert res_cr["ci_status"] == "FAILURE"
        assert res_cr["needs_ci_fix"] is True

    def test_sync_engine_cross_collection_settlement_idempotency(
        self, temp_workspace: Path, offline_db: OfflineFirestoreClient
    ):
        """When an issue is present in both bounty_leads and bounty_memory, settlement is recorded exactly once."""
        engine = SyncEngine(db=offline_db)
        mem_col = offline_db.collection(COLLECTION_BOUNTY_MEMORY)
        leads_col = offline_db.collection(COLLECTION_BOUNTY_LEADS)

        # Same canonical doc_id in both collections
        doc_id = "stellar_soroban_app_100"
        mem_col.document(doc_id).set({
            "repo": "stellar/soroban-app",
            "pr_number": 100,
            "state": "MERGED",
            "projected_payout_usd": 600.0,
        })
        leads_col.document(doc_id).set({
            "repo": "stellar/soroban-app",
            "issue_number": 100,
            "status": "completed",
            "projected_payout_usd": 600.0,
        })

        res = engine.sync_settlements()
        assert res["total_settled_count"] == 1  # Only 1 unique settlement ID settle_stellar_soroban_app_100
        assert res["total_settled_usd"] == 600.0

    def test_executor_engine_stipulation_and_payout_routing_integrity(self):
        """Verify ExecutorEngine extracts stipulations and enforces hardcoded Web3 payout routing."""
        from src.engines.executor_engine import (
            ExecutorEngine,
            extract_stipulations_from_text,
        )

        body_text = """
## Requirements
- [ ] Implement atomic token transfer in Soroban Rust contract
- [x] Write property-based tests using proptest
- [ ] Ensure 0 compiler warnings

### Definition of Done:
1. Pass all unit tests
2. Pass security audit
"""
        stips = extract_stipulations_from_text(body_text)
        assert "Implement atomic token transfer in Soroban Rust contract" in stips
        assert "Write property-based tests using proptest" in stips
        assert "Ensure 0 compiler warnings" in stips
        assert "Pass all unit tests" in stips

        # Payout routing block check
        engine = ExecutorEngine()
        payout_block = engine.generate_payout_routing_block()
        assert EVM_PAYOUT_ADDRESS in payout_block
        assert "GCL6OXAMLD75BMTINA6EMRUDWK5THQUSHMYNLSNBCJAPZJHNYJTUNIBC" in payout_block

