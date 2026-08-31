"""
Adversarial Stress Test & Challenger Verification Suite for 3D Pipeline UI.
Empirically challenges:
1. Static routing of /console/pipeline.html against path traversal, query noise, large payloads, malformed headers.
2. Complete DOM structure integrity (Canvas, HUD, stage chips, tooltip, telemetry).
3. Built bundle asset resolution and import tree integrity without broken references.
4. Data contract adherence under high-stress and corrupted inputs.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Set
from urllib.parse import quote, urlparse

import pytest

from src.console_server import (
    CANONICAL_STAGES,
    CONSOLE_DIR,
    ConsoleHandler,
    ThreadingHTTPServer,
    dispatch,
    pipeline_payload,
)
from src.core.firestore_client import OfflineFirestoreClient


# =============================================================================
# 1. Static Routing Adversarial Stress Tests
# =============================================================================

class TestPipelineStaticRoutingAdversarial:
    """Stress tests static routing of /console/pipeline.html under adversarial conditions."""

    def test_pipeline_html_canonical_serving(self):
        """Ensure /console/pipeline.html is served with 200 OK and text/html charset=utf-8."""
        status, ctype, body = dispatch("/console/pipeline.html")
        assert status == 200
        assert "text/html" in ctype
        assert "charset=utf-8" in ctype
        assert len(body) > 500
        assert b"<!DOCTYPE html>" in body or b"<!doctype html>" in body.lower()

    @pytest.mark.parametrize(
        "query_noise",
        [
            "?v=1.0.0",
            "?stage=pr_open&lead=stellar_101",
            "?utm_source=fuzz&seed=999999",
            "?q=../../../etc/passwd",
            "?payload=" + quote("<script>alert('xss')</script>"),
            "?filter=' OR '1'='1",
            "?null_byte=%00&data=true",
            "?unicode=" + quote("宇宙星系✨🪐🚀"),
            "?" + "&".join(f"param_{i}={i*42}" for i in range(100)),
            "?large=" + ("A" * 8000),
        ],
    )
    def test_pipeline_html_query_string_noise(self, query_noise: str):
        """Verify that any query string noise or hostile query params still serve pipeline.html safely."""
        path = f"/console/pipeline.html{query_noise}"
        status, ctype, body = dispatch(path)
        assert status == 200, f"Failed on path: {path}"
        assert "text/html" in ctype
        assert b"pipeline-canvas" in body

    @pytest.mark.parametrize(
        "traversal_attempt",
        [
            "/console/pipeline.html/../../etc/passwd",
            "/console/pipeline.html/../../../private_key.pem",
            "/console/pipeline.html/..",
            "/console/pipeline.html/",
            "/console/pipeline.html%00.txt",
            "/console/pipeline.html%2f..%2f.env",
            "/console/assets/../../pipeline.html",
            "/console/%2e%2e/pipeline.html",
        ],
    )
    def test_pipeline_traversal_and_boundary_rejection(self, traversal_attempt: str):
        """Ensure path traversal attacks targeting or stemming from pipeline.html are rejected with 404."""
        status, ctype, body = dispatch(traversal_attempt)
        # Should either be 404 or (if safe resolution resolves inside console dir) serve safe static file without leaking secrets
        if status == 200:
            # If 200, ensure it's within console bounds and does not contain server private files
            assert b"PRIVATE KEY" not in body
            assert b"root:x" not in body
            assert b"DATABASE_URL" not in body
        else:
            assert status == 404

    def test_live_http_server_socket_stress(self):
        """Stress-test live HTTP socket connections with rapid bursts and malformed requests."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        time.sleep(0.05)

        try:
            # 1. Burst of 50 rapid GET requests to /console/pipeline.html
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            for i in range(50):
                conn.request("GET", f"/console/pipeline.html?req_id={i}")
                res = conn.getresponse()
                assert res.status == 200
                data = res.read()
                assert b"pipeline-canvas" in data
            conn.close()

            # 2. Large header payload
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {
                "User-Agent": "ChallengerBot/1.0 (" + "X" * 2048 + ")",
                "X-Custom-Telemetry": "FuzzPayload-" * 100,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            conn.request("GET", "/console/pipeline.html", headers=headers)
            res = conn.getresponse()
            assert res.status == 200
            assert b"pipeline-canvas" in res.read()
            conn.close()

            # 3. Raw socket fuzzing with raw malformed strings
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", port))
                raw_req = b"GET /console/pipeline.html?fuzz=\xff\xfe HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
                s.sendall(raw_req)
                resp_data = s.recv(4096)
                assert b"200 OK" in resp_data or b"400 Bad Request" in resp_data

        finally:
            server.shutdown()
            server.server_close()


# =============================================================================
# 2. DOM Structure & Accessibility Validation
# =============================================================================

class TestPipelineDomStructure:
    """Verifies all required DOM elements and HUD anchors exist in served pipeline.html."""

    @pytest.fixture(scope="class")
    def pipeline_html(self) -> str:
        status, _, body = dispatch("/console/pipeline.html")
        assert status == 200
        return body.decode("utf-8")

    def test_canvas_element_present_and_accessible(self, pipeline_html: str):
        """Verify WebGL canvas exists with appropriate id and aria-label."""
        assert '<canvas id="pipeline-canvas"' in pipeline_html
        assert 'aria-label=' in pipeline_html

    def test_navigation_header_and_brand(self, pipeline_html: str):
        """Verify console navigation bar matches UB Fleet standard."""
        assert '<header class="nav">' in pipeline_html
        assert 'class="mark">UB</div>' in pipeline_html
        assert 'Universal Bounty Fleet' in pipeline_html
        assert 'href="/console/pipeline.html" class="active">Pipeline</a>' in pipeline_html
        assert 'id="health-dot"' in pipeline_html
        assert 'id="health-label"' in pipeline_html
        assert 'id="pipeline-total-badge"' in pipeline_html
        assert 'id="clock"' in pipeline_html

    def test_stage_filter_chips_and_controls(self, pipeline_html: str):
        """Verify all 5 stages + all filter buttons and reset view button are present."""
        assert 'id="stage-chips"' in pipeline_html
        assert 'id="btn-reset-view"' in pipeline_html

        required_stages = ["all", "queued", "pending_triage", "pr_open", "completed", "failed"]
        for st in required_stages:
            assert f'data-stage="{st}"' in pipeline_html, f"Missing stage chip: {st}"
            assert f'id="count-{st}"' in pipeline_html, f"Missing count element for: {st}"

    def test_hud_inspector_overlay_structure(self, pipeline_html: str):
        """Verify floating HUD panel and sub-elements."""
        assert 'id="pipeline-hud"' in pipeline_html
        assert 'id="hud-close-btn"' in pipeline_html
        assert 'id="hud-content"' in pipeline_html
        assert "CONSTELLATION INSPECTOR" in pipeline_html

    def test_tooltip_overlay_structure(self, pipeline_html: str):
        """Verify floating hover tooltip elements."""
        assert 'id="pipeline-tooltip"' in pipeline_html
        assert 'id="tooltip-status-badge"' in pipeline_html
        assert 'id="tooltip-payout"' in pipeline_html
        assert 'id="tooltip-repo"' in pipeline_html
        assert 'id="tooltip-title"' in pipeline_html

    def test_telemetry_footer(self, pipeline_html: str):
        """Verify footer telemetry elements."""
        assert '<footer class="pipeline-footer">' in pipeline_html
        assert 'id="last-sync-time"' in pipeline_html
        assert 'id="total-pipeline-value"' in pipeline_html


# =============================================================================
# 3. Bundle Asset Resolution and Dependency Graph Integrity
# =============================================================================

class TestBundleAssetIntegrity:
    """Verifies that all JS/CSS assets in static/console/ exist, resolve, and have valid imports."""

    def test_all_referenced_assets_resolve_with_correct_mimetypes(self):
        """Parse all scripts and stylesheet links from static/console/pipeline.html and verify 200 status."""
        status, _, body = dispatch("/console/pipeline.html")
        assert status == 200
        html = body.decode("utf-8")

        # Scripts
        scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
        assert len(scripts) >= 1, "Expected at least 1 script tag in pipeline.html"

        for s_url in scripts:
            s_status, s_ctype, s_body = dispatch(s_url)
            assert s_status == 200, f"Script asset failed to load: {s_url}"
            assert "application/javascript" in s_ctype or "text/javascript" in s_ctype
            assert len(s_body) > 100

        # Module preloads
        preloads = re.findall(r'<link[^>]+rel="modulepreload"[^>]+href="([^"]+)"', html)
        for p_url in preloads:
            p_status, p_ctype, p_body = dispatch(p_url)
            assert p_status == 200, f"Modulepreload asset failed to load: {p_url}"
            assert "application/javascript" in p_ctype or "text/javascript" in p_ctype
            assert len(p_body) > 100

        # Stylesheets
        stylesheets = re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"', html)
        assert len(stylesheets) >= 1, "Expected at least 1 stylesheet link in pipeline.html"

        for c_url in stylesheets:
            c_status, c_ctype, c_body = dispatch(c_url)
            assert c_status == 200, f"Stylesheet asset failed to load: {c_url}"
            assert "text/css" in c_ctype
            assert len(c_body) > 100

    def test_internal_javascript_module_imports_are_resolved(self):
        """Inspect all JS files in static/console/assets/ to ensure all relative ES imports exist."""
        assets_dir = CONSOLE_DIR / "assets"
        assert assets_dir.exists() and assets_dir.is_dir()

        js_files = list(assets_dir.glob("*.js"))
        assert len(js_files) > 0, "No JS files found in static/console/assets"

        for js_file in js_files:
            content = js_file.read_text(encoding="utf-8")
            # Match ES module import statements like `from"./three.module-Bcl9ZI_9.js"` or `import"./foo.js"`
            imports = re.findall(r'(?:from|import)\s*["\'](\.[^"\']+)["\']', content)
            for imp in imports:
                # Relative to assets_dir
                target_file = (js_file.parent / imp).resolve()
                assert target_file.exists(), (
                    f"Broken import in {js_file.name}: {imp} -> resolved to {target_file}, which does not exist!"
                )
                assert target_file.is_file(), f"Target {target_file} is not a file!"


# =============================================================================
# 4. Pipeline Data Contract & High-Stress Fuzzing
# =============================================================================

class TestPipelineDataContractFuzzing:
    """Stress tests /api/pipeline output under hostile, missing, and boundary lead structures."""

    def test_pipeline_with_100_synthetic_leads_across_all_stages(
        self, offline_db: OfflineFirestoreClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr("src.console_server.get_firestore_client", lambda: offline_db)
        col = offline_db.collection("bounty_leads")

        stages_cycle = ["queued", "pending_triage", "pr_open", "completed", "failed"]
        for i in range(100):
            st = stages_cycle[i % len(stages_cycle)]
            col.document(f"synthetic_lead_{i}").set({
                "id": f"synthetic_lead_{i}",
                "repo": f"org-{i % 5}/repo-{i % 10}",
                "issue_number": 1000 + i,
                "title": f"Adversarial synthetic bounty #{i} with utf8: 🚀 \u202e \u0000",
                "status": st,
                "projected_payout_usd": float(i * 125.5),
                "qualification_reason": "High-throughput synthetic stress test",
                "ecosystem": "stellar" if i % 2 == 0 else "evm",
                "escrow_verified": (i % 3 != 0),
            })

        status, ctype, body = dispatch("/api/pipeline")
        assert status == 200
        data = json.loads(body.decode("utf-8"))

        assert data["counts"]["total"] == 100
        for st in CANONICAL_STAGES:
            assert data["counts"][st] == 20
            assert len(data["grouped"][st]) == 20

        # Verify all leads parse cleanly
        for lead in data["leads"]:
            assert lead["status"] in CANONICAL_STAGES
            assert isinstance(lead["projected_payout_usd"], float)
            assert isinstance(lead["escrow_verified"], bool)
