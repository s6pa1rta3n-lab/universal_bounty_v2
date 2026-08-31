"""
Integration Tests for 3D Constellation Pipeline UI and Static Serving.

Verifies:
1. End-to-end static routing of /console/pipeline.html with HTTP 200 and UTF-8 charset.
2. Presence of all essential 3D canvas, HUD, tooltip, and stage navigation DOM anchors.
3. Verification that bundled JS and CSS assets referenced in pipeline.html exist and are served with valid MIME types.
4. Full contract integration between /api/pipeline JSON payload and frontend requirements.
5. Multi-page routing separation between /console (Overview) and /console/pipeline.html (Pipeline).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

import pytest

from src.console_server import (
    CANONICAL_STAGES,
    CONSOLE_DIR,
    dispatch,
    pipeline_payload,
)
from src.core.firestore_client import OfflineFirestoreClient


class TestPipelineUiIntegration:
    """Integration test suite for 3D Pipeline UI and backend serving."""

    def test_pipeline_html_served_and_contains_core_elements(self):
        """Verify GET /console/pipeline.html returns 200, proper content-type, and all required DOM elements."""
        status, ctype, body = dispatch("/console/pipeline.html")
        assert status == 200
        assert "text/html" in ctype
        assert "charset=utf-8" in ctype

        html_text = body.decode("utf-8")

        # 1. 3D WebGL Canvas
        assert '<canvas id="pipeline-canvas"' in html_text or 'id="pipeline-canvas"' in html_text

        # 2. Navigation Header & Routes
        assert '<header class="nav">' in html_text
        assert 'href="/console/pipeline.html"' in html_text
        assert 'class="mark">UB</div>' in html_text
        assert 'id="pipeline-total-badge"' in html_text
        assert 'id="clock"' in html_text
        assert 'id="health-dot"' in html_text

        # 3. Stage Filter Bar & Chips
        assert 'id="stage-chips"' in html_text
        assert 'id="btn-reset-view"' in html_text
        for stage in ["all", "queued", "pending_triage", "pr_open", "completed", "failed"]:
            assert f'data-stage="{stage}"' in html_text
            assert f'id="count-{stage}"' in html_text

        # 4. Floating Glassmorphic HUD Inspector
        assert 'id="pipeline-hud"' in html_text
        assert 'id="hud-close-btn"' in html_text
        assert 'id="hud-content"' in html_text
        assert "CONSTELLATION INSPECTOR" in html_text

        # 5. Floating Hover Tooltip
        assert 'id="pipeline-tooltip"' in html_text
        assert 'id="tooltip-status-badge"' in html_text
        assert 'id="tooltip-payout"' in html_text
        assert 'id="tooltip-repo"' in html_text
        assert 'id="tooltip-title"' in html_text

        # 6. Live Telemetry Footer
        assert 'id="last-sync-time"' in html_text
        assert 'id="total-pipeline-value"' in html_text

    def test_bundled_assets_served_with_correct_mime_and_non_empty(self):
        """Verify that JS and CSS assets referenced in pipeline.html are resolvable and served correctly."""
        status, _, body = dispatch("/console/pipeline.html")
        assert status == 200
        html_text = body.decode("utf-8")

        # Extract script src and stylesheet href attributes
        script_srcs = re.findall(r'<script[^>]+src="([^"]+)"', html_text)
        css_hrefs = re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"', html_text)

        assert len(script_srcs) > 0, "pipeline.html must include at least one bundled script"
        assert len(css_hrefs) > 0, "pipeline.html must include at least one stylesheet link"

        # Verify all referenced scripts are served cleanly
        for script_path in script_srcs:
            s_status, s_ctype, s_body = dispatch(script_path)
            assert s_status == 200, f"Failed to fetch script at {script_path}"
            assert "application/javascript" in s_ctype or "text/javascript" in s_ctype
            assert len(s_body) > 100, f"Script at {script_path} was unexpectedly empty"

        # Verify all referenced stylesheets are served cleanly
        for css_path in css_hrefs:
            c_status, c_ctype, c_body = dispatch(css_path)
            assert c_status == 200, f"Failed to fetch stylesheet at {css_path}"
            assert "text/css" in c_ctype
            assert len(c_body) > 100, f"Stylesheet at {css_path} was unexpectedly empty"

    def test_pipeline_api_and_ui_data_contract_consistency(
        self, offline_db: OfflineFirestoreClient, monkeypatch: pytest.MonkeyPatch
    ):
        """Verify /api/pipeline output perfectly matches the schema requirements expected by pipeline.ts."""
        monkeypatch.setattr("src.console_server.get_firestore_client", lambda: offline_db)

        col = offline_db.collection("bounty_leads")
        test_leads = [
            {
                "id": "lead_intake_1",
                "repo": "stellar/rs-soroban-sdk",
                "issue_number": 105,
                "title": "Add bls12-381 verification tests",
                "status": "queued",
                "projected_payout": "$2,500",
                "projected_payout_usd": 2500.0,
                "ecosystem": "stellar",
                "escrow_verified": True,
                "qualification_reason": "GrantFox smart contract escrow verified",
            },
            {
                "id": "lead_triage_1",
                "repo": "base-org/contracts",
                "issue_number": 210,
                "title": "Optimism portal gas optimization",
                "status": "pending_triage",
                "projected_payout_usd": 1200.0,
                "ecosystem": "base",
                "escrow_verified": True,
            },
            {
                "id": "lead_pr_1",
                "repo": "ethereum-optimism/superchain-ops",
                "issue_number": 315,
                "pr_number": 316,
                "title": "Implement multi-sig verification gate",
                "status": "pr_open",
                "projected_payout_usd": 3000.0,
                "ecosystem": "optimism",
                "escrow_verified": True,
                "pr_url": "https://github.com/ethereum-optimism/superchain-ops/pull/316",
            },
            {
                "id": "lead_comp_1",
                "repo": "uniswap/v4-core",
                "issue_number": 420,
                "title": "Dynamic hook fee calculation fix",
                "status": "completed",
                "projected_payout_usd": 4000.0,
                "ecosystem": "ethereum",
                "escrow_verified": True,
            },
            {
                "id": "lead_fail_1",
                "repo": "arbitrum/nitro-contracts",
                "issue_number": 525,
                "title": "Outdated challenge assertion test",
                "status": "failed",
                "projected_payout_usd": 0.0,
                "ecosystem": "arbitrum",
                "escrow_verified": False,
                "qualification_reason": "Unfunded / educational issue discarded",
            },
        ]

        for item in test_leads:
            col.document(item["id"]).set(item)

        status, ctype, body = dispatch("/api/pipeline")
        assert status == 200
        assert "application/json" in ctype

        payload = json.loads(body.decode("utf-8"))

        # Verify counts
        counts = payload["counts"]
        assert counts["total"] == 5
        assert counts["queued"] == 1
        assert counts["pending_triage"] == 1
        assert counts["pr_open"] == 1
        assert counts["completed"] == 1
        assert counts["failed"] == 1

        # Verify grouped partitioning
        for stage in CANONICAL_STAGES:
            assert len(payload["grouped"][stage]) == 1
            assert payload["grouped"][stage][0]["status"] == stage

        # Verify all leads have complete fields needed for Three.js engine and HUD
        for lead in payload["leads"]:
            assert "id" in lead and isinstance(lead["id"], str)
            assert "repo" in lead and isinstance(lead["repo"], str)
            assert "status" in lead and lead["status"] in CANONICAL_STAGES
            assert "projected_payout_usd" in lead and isinstance(lead["projected_payout_usd"], float)
            assert "escrow_verified" in lead and isinstance(lead["escrow_verified"], bool)
            assert "qualification_reason" in lead and isinstance(lead["qualification_reason"], str)
            assert "ecosystem" in lead and isinstance(lead["ecosystem"], str)
            assert "issue_url" in lead
            assert "pr_url" in lead

        # Verify PR URL synthesis and explicit override
        pr_lead = next(l for l in payload["leads"] if l["id"] == "lead_pr_1")
        assert pr_lead["pr_url"] == "https://github.com/ethereum-optimism/superchain-ops/pull/316"

    def test_multi_page_routing_separation(self):
        """Verify that /console returns index.html and /console/pipeline.html returns pipeline.html."""
        status_root, _, body_root = dispatch("/console")
        status_pipe, _, body_pipe = dispatch("/console/pipeline.html")

        assert status_root == 200
        assert status_pipe == 200

        text_root = body_root.decode("utf-8")
        text_pipe = body_pipe.decode("utf-8")

        # Root console contains Overview / Live App container
        assert '<div id="app"></div>' in text_root
        # Pipeline page contains the 3D pipeline canvas and HUD
        assert 'id="pipeline-canvas"' in text_pipe
        assert 'id="pipeline-hud"' in text_pipe
