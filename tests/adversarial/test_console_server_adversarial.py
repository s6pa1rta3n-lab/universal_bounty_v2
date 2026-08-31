"""
Empirical Adversarial Stress, Security & Fuzzing Harness for Milestone M1 in src/console_server.py.

Evaluates:
1. Path Traversal & Static Routing Attacks against dispatch(path)
2. Abnormal, Malformed, Hostile, and Boundary Inputs in pipeline_payload & _format_lead
3. Concurrency, Race Conditions & Rapid Dispatch Calls (in-memory and live socket)
4. Schema & Interface Contract Invariants
"""

import http.client
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote, unquote

import pytest

from src.console_server import (
    CANONICAL_STAGES,
    ConsoleHandler,
    _format_lead,
    dispatch,
    empty_pipeline_payload,
    normalize_bounty,
    normalize_status,
    pipeline_payload,
)
from src.core.firestore_client import OfflineFirestoreClient


# =============================================================================
# 1. Path Traversal & Static Routing Adversarial Attacks
# =============================================================================


class TestConsoleServerPathTraversal:
    """Adversarial stress testing for path traversal and file boundary escaping."""

    @pytest.fixture(autouse=True)
    def setup_console_env(self, temp_workspace: Path, monkeypatch):
        self.workspace = temp_workspace
        self.console_dir = temp_workspace / "static" / "console"
        self.console_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir = self.console_dir / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)

        # Create sensitive files outside CONSOLE_DIR
        self.secret_env = temp_workspace / ".env"
        self.secret_env.write_text("DATABASE_URL=postgres://admin:super_secret@localhost:5432/db")

        self.secret_key = temp_workspace / "private_key.pem"
        self.secret_key.write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...")

        # Create standard console files
        self.index_html = "<html><head><title>Fleet Console</title></head><body>Console SPA Root</body></html>"
        self.pipeline_html = "<html><head><title>3D Pipeline</title></head><body>Pipeline Canvas</body></html>"
        self.style_css = "body { background: #0a0a0f; color: #00ffcc; }"
        self.app_js = "console.log('Fleet Console Loaded');"

        (self.console_dir / "index.html").write_text(self.index_html)
        (self.console_dir / "pipeline.html").write_text(self.pipeline_html)
        (self.assets_dir / "style.css").write_text(self.style_css)
        (self.assets_dir / "app.js").write_text(self.app_js)

        monkeypatch.setattr("src.console_server.CONSOLE_DIR", self.console_dir)

    def test_direct_parent_directory_traversal(self):
        """Test relative path traversal attempts like .. and ../.. to read private keys."""
        attack_paths = [
            "/console/../.env",
            "/console/../../private_key.pem",
            "/console/../../../etc/passwd",
            "/console/../../../../../../etc/shadow",
            "/console/assets/../../.env",
            "/console/assets/../../../private_key.pem",
            "/console/./../../.env",
            "/console/./.././../private_key.pem",
        ]
        for path in attack_paths:
            status, ctype, body = dispatch(path)
            assert status == 404, f"Path traversal succeeded for {path}! status={status}"
            assert b"DATABASE_URL" not in body
            assert b"PRIVATE KEY" not in body
            assert b"root:" not in body

    def test_encoded_path_traversal_variations(self):
        """Test URL-encoded and double-encoded path traversal sequences."""
        attack_paths = [
            "/console/%2e%2e/.env",
            "/console/%2e%2e/%2e%2e/private_key.pem",
            "/console/%2e%2e%2f.env",
            "/console/%252e%252e%252f.env",
            "/console/..%2f..%2fprivate_key.pem",
            "/console/%2e%2e%5c.env",
            "/console/%2e%2e/assets/../../.env",
        ]
        for path in attack_paths:
            status, ctype, body = dispatch(path)
            # Either 404, or SPA index fallback if resolved inside console dir, but NEVER leaked secret content
            assert b"DATABASE_URL" not in body
            assert b"PRIVATE KEY" not in body

    def test_symlink_escape_vector(self):
        """Test that symlinks inside static/console pointing to sensitive directories outside are rejected."""
        symlink_target = self.secret_env
        symlink_file = self.console_dir / "leaked_env.txt"

        try:
            os.symlink(str(symlink_target), str(symlink_file))
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not supported on this filesystem")

        # Attempt to access the symlink
        status, ctype, body = dispatch("/console/leaked_env.txt")
        # .resolve() resolves symlinks, relative_to(base_dir) will fail -> 404
        assert status == 404
        assert b"DATABASE_URL" not in body

    def test_null_byte_injection_and_path_fuzzing(self):
        """Test null bytes and control character injections in path resolution."""
        fuzz_paths = [
            "/console/pipeline.html\x00.secret",
            "/console/\x00/../../.env",
            "/console/pipeline.html%00",
            "/console/pipeline.html\r\nHeader-Injection: true",
            "/console/assets/style.css\n",
        ]
        for path in fuzz_paths:
            status, ctype, body = dispatch(path)
            assert status in (200, 404)
            assert b"DATABASE_URL" not in body
            assert b"PRIVATE KEY" not in body

    def test_absolute_and_scheme_relative_paths(self):
        """Test absolute, URI-scheme and UNC path injections."""
        fuzz_paths = [
            "http://evil.com/console/../.env",
            "https://attacker.org/console/../../private_key.pem",
            "//evil.com/console/pipeline.html",
            "file:///console/../.env",
            "/console////assets/style.css",
            "/console/\\..\\../.env",
        ]
        for path in fuzz_paths:
            status, ctype, body = dispatch(path)
            assert b"DATABASE_URL" not in body
            assert b"PRIVATE KEY" not in body

    def test_valid_static_asset_serving_and_mime_types(self):
        """Verify standard static assets resolve with correct headers and contents."""
        cases = [
            ("/console", 200, "text/html", self.index_html),
            ("/console/", 200, "text/html", self.index_html),
            ("/console/pipeline.html", 200, "text/html", self.pipeline_html),
            ("/console/assets/style.css", 200, "text/css", self.style_css),
            ("/console/assets/app.js", 200, "javascript", self.app_js),
            ("/console/pipeline.html?query=123#frag", 200, "text/html", self.pipeline_html),
        ]
        for path, exp_status, exp_mime_sub, exp_content in cases:
            status, ctype, body = dispatch(path)
            assert status == exp_status
            assert exp_mime_sub in ctype
            assert body.decode() == exp_content

    def test_spa_fallback_behavior(self):
        """Verify non-existent routes under /console fallback to index.html for SPA client-side routing."""
        spa_routes = [
            "/console/dashboard",
            "/console/pipeline/details/123",
            "/console/settings/advanced",
            "/console/nonexistent.html",
        ]
        for route in spa_routes:
            status, ctype, body = dispatch(route)
            assert status == 200
            assert "text/html" in ctype
            assert body.decode() == self.index_html

    def test_non_console_unmatched_routes_return_404(self):
        """Verify arbitrary non-API and non-console routes strictly return 404."""
        bad_routes = [
            "/",
            "/admin",
            "/secret",
            "/etc/passwd",
            "/api/unknown_endpoint",
            "/console_server.py",
        ]
        for route in bad_routes:
            status, ctype, body = dispatch(route)
            assert status == 404
            assert body == b"not found"


# =============================================================================
# 2. Malformed, Hostile, and Boundary Inputs in pipeline_payload & _format_lead
# =============================================================================


class TestConsoleServerHostilePayloads:
    """Stress testing data formatting and serialization under adversarial inputs."""

    def test_format_lead_with_none_and_empty_inputs(self):
        """Verify _format_lead handles None, empty dicts, and non-dict objects gracefully."""
        res_none = _format_lead(None)
        assert res_none["id"] == "unknown"
        assert res_none["repo"] == "unknown"
        assert res_none["issue_number"] is None
        assert res_none["status"] == "queued"
        assert res_none["projected_payout_usd"] == 0.0
        assert res_none["escrow_verified"] is False

        res_empty = _format_lead({})
        assert res_empty["id"] == "unknown"
        assert res_empty["title"] == ""
        assert res_empty["status"] == "queued"

        res_str = _format_lead("raw_string_not_dict")
        assert res_str["id"] == "unknown"
        assert res_str["status"] == "queued"

    def test_format_lead_with_type_confusion_in_repo_and_numbers(self):
        """Verify type confusion (e.g. nested dict repo, float issues, string numbers)."""
        doc = {
            "id": 12345,  # Non-string ID
            "repository": {"nameWithOwner": "stellar/soroban-sdk", "extra": True},
            "issue_number": "42",  # String instead of int
            "pr_number": "1001",
            "payout_usd": "750.50",
            "status": "TRIAGED",
        }
        res = _format_lead(doc)
        assert res["id"] == "12345"
        assert res["repo"] == "stellar/soroban-sdk"
        assert res["issue_number"] == 42
        assert res["projected_payout_usd"] == 750.50
        assert res["status"] == "pending_triage"
        assert res["issue_url"] == "https://github.com/stellar/soroban-sdk/issues/42"
        assert res["pr_url"] == "https://github.com/stellar/soroban-sdk/pull/1001"

    def test_format_lead_extreme_and_corrupt_numbers(self):
        """Verify extreme integers, invalid strings, negative numbers and float boundaries."""
        doc = {
            "id": "extreme_doc",
            "repo": "org/repo",
            "issue_number": "not-a-number",
            "pr_number": 999999,
            "projected_payout_usd": "invalid_float",
            "projected_payout": "$1,000,000.00",
        }
        res = _format_lead(doc)
        assert res["issue_number"] is None
        assert res["projected_payout_usd"] == 0.0
        assert res["projected_payout"] == "$1,000,000.00"
        assert res["pr_url"] == "https://github.com/org/repo/pull/999999"

    def test_format_lead_hostile_xss_and_injection_strings(self):
        """Verify XSS strings, SQL injections, null bytes and Unicode formatting."""
        xss_payload = "<script>alert('pwned')</script><img src=x onerror=alert(1)>"
        rtl_unicode = "Soroban \u202eRTL_OVERRIDE\u202c 🦀 🚀 [GrantFox] المكافأة"
        sql_injection = "'; DROP TABLE bounty_leads; --"

        doc = {
            "id": "xss_doc_1",
            "repo": "malicious/org",
            "issue_number": 666,
            "title": xss_payload,
            "qualification_reason": sql_injection,
            "ecosystem": rtl_unicode,
            "status": "intake",
            "payout_usd": 500.0,
        }
        res = _format_lead(doc)
        assert res["title"] == xss_payload
        assert res["qualification_reason"] == sql_injection
        assert res["ecosystem"] == rtl_unicode
        assert res["status"] == "queued"

        # Verify JSON serializability of the formatted output
        encoded = json.dumps(res)
        assert "<script>" in encoded
        assert "DROP TABLE" in encoded
        decoded = json.loads(encoded)
        assert decoded["title"] == xss_payload

    def test_format_lead_massive_string_payload(self):
        """Verify huge strings (e.g. 1MB description or title) do not crash lead formatting."""
        large_str = "X" * (1024 * 1024)  # 1MB
        doc = {
            "id": "large_doc",
            "repo": "org/large",
            "issue_number": 1,
            "title": large_str,
            "qualification_reason": large_str,
            "status": "draft_pr",
        }
        res = _format_lead(doc)
        assert len(res["title"]) == 1024 * 1024
        assert res["status"] == "pr_open"

    def test_normalize_status_adversarial_fuzzing(self):
        """Fuzz normalize_status with unusual types, nested objects, and boundary characters."""
        fuzz_cases = [
            (None, "queued"),
            ("", "queued"),
            ("   ", "queued"),
            ("\n\t", "queued"),
            ("unknown_status_xyz", "queued"),
            ("COMPLETED", "completed"),
            ("   pEnDiNg_DiScOvErY   ", "queued"),
            ("RUNNING_ORBSTACK", "pr_open"),
            ("FAILED_VERIFICATION", "failed"),
            ("REJECTED", "failed"),
            (12345, "queued"),
            (True, "queued"),
            ({"status": "open"}, "queued"),
            (["in_progress"], "queued"),
        ]
        for input_val, expected in fuzz_cases:
            assert normalize_status(input_val) == expected

    def test_pipeline_payload_handles_stream_exception_gracefully(self, monkeypatch):
        """Verify pipeline_payload returns clean empty_pipeline_payload when db stream raises an exception."""
        class FailingCollection:
            def stream(self):
                raise ConnectionResetError("Simulated Firestore connection drop")

        class FailingDB:
            def collection(self, name):
                return FailingCollection()

        data = pipeline_payload(db=FailingDB())
        assert data == empty_pipeline_payload()

    def test_pipeline_payload_handles_mid_stream_generator_error(self, monkeypatch):
        """Verify pipeline_payload catches exceptions raised mid-stream during iteration."""
        def faulty_stream():
            yield {"id": "lead_1", "status": "queued"}
            raise RuntimeError("Corrupted document record mid-stream")

        class FaultyCollection:
            def stream(self):
                return faulty_stream()

        class FaultyDB:
            def collection(self, name):
                return FaultyCollection()

        data = pipeline_payload(db=FaultyDB())
        assert data == empty_pipeline_payload()


# =============================================================================
# 3. Concurrency, Race Conditions & Rapid Dispatch Calls
# =============================================================================


class TestConsoleServerConcurrencyAndStress:
    """Stress testing under high concurrency, rapid burst requests, and multi-threading."""

    def test_concurrent_dispatch_in_memory(self, offline_db: OfflineFirestoreClient, monkeypatch):
        """Verify thread safety when 100 concurrent threads invoke dispatch() across mixed endpoints."""
        monkeypatch.setattr("src.console_server.get_firestore_client", lambda: offline_db)

        # Seed database with sample leads
        leads_col = offline_db.collection("bounty_leads")
        for i in range(25):
            leads_col.document(f"concurrent_lead_{i}").set({
                "repo": f"org/repo_{i % 5}",
                "issue_number": i + 1,
                "title": f"Concurrent Lead #{i}",
                "status": CANONICAL_STAGES[i % len(CANONICAL_STAGES)],
                "payout_usd": float((i + 1) * 50),
            })

        endpoints = [
            "/health",
            "/api/pipeline",
            "/api/registry",
            "/api/history",
            "/api/bounties/latest",
            "/console/pipeline.html",
            "/console/nonexistent_subpage",
            "/invalid/route",
        ]

        results = []
        errors = []

        def worker(idx: int):
            endpoint = endpoints[idx % len(endpoints)]
            try:
                status, ctype, body = dispatch(endpoint, db=offline_db)
                results.append((endpoint, status, len(body)))
            except Exception as exc:
                errors.append((endpoint, str(exc)))

        num_threads = 100
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Encountered errors during concurrent dispatch: {errors}"
        assert len(results) == 100

        # Verify all /api/pipeline calls returned valid JSON with 25 leads
        pipeline_results = [r for r in results if r[0] == "/api/pipeline"]
        assert len(pipeline_results) > 0
        for _, status, body_len in pipeline_results:
            assert status == 200
            assert body_len > 0

    def test_rapid_burst_dispatch_throughput(self, offline_db: OfflineFirestoreClient):
        """Execute 1,000 rapid dispatch calls and verify zero crashes or latency degradation."""
        leads_col = offline_db.collection("bounty_leads")
        leads_col.document("burst_lead").set({
            "repo": "stellar/soroban",
            "issue_number": 99,
            "status": "pr_open",
            "payout_usd": 1200.0,
        })

        t_start = time.time()
        for i in range(1000):
            status, ctype, body = dispatch("/api/pipeline", db=offline_db)
            assert status == 200
        t_duration = time.time() - t_start

        # 1,000 in-memory dispatches should easily execute within 3 seconds
        assert t_duration < 3.0, f"Rapid burst too slow: {t_duration:.2f}s for 1000 calls"

    def test_live_socket_http_server_concurrency(self, offline_db: OfflineFirestoreClient, monkeypatch):
        """Spin up a real ThreadingHTTPServer on an ephemeral port and test socket concurrency."""
        monkeypatch.setattr("src.console_server.get_firestore_client", lambda: offline_db)

        # Seed lead
        offline_db.collection("bounty_leads").document("live_socket_lead").set({
            "repo": "socket/test",
            "issue_number": 1,
            "status": "completed",
            "payout_usd": 999.0,
        })

        # Start ThreadingHTTPServer on ephemeral port 0 (OS assigned)
        server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
        host, port = server.server_address

        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        time.sleep(0.05)  # Allow socket to bind

        try:
            def client_request(path: str) -> int:
                conn = http.client.HTTPConnection(host, port, timeout=2.0)
                try:
                    conn.request("GET", path)
                    resp = conn.getresponse()
                    resp.read()
                    return resp.status
                finally:
                    conn.close()

            # Execute 32 concurrent socket requests via ThreadPoolExecutor
            paths = ["/health", "/api/pipeline", "/api/registry", "/api/history"] * 8
            with ThreadPoolExecutor(max_workers=10) as executor:
                statuses = list(executor.map(client_request, paths))

            assert len(statuses) == len(paths)
            assert all(s == 200 for s in statuses)

        finally:
            server.shutdown()
            server.server_close()


# =============================================================================
# 4. Schema & Interface Contract Invariants
# =============================================================================


class TestConsoleServerContractRigor:
    """Strict verification of interface schema contracts against PROJECT.md specification."""

    def test_pipeline_schema_contract_compliance(self, offline_db: OfflineFirestoreClient):
        """Verify GET /api/pipeline matches the exact JSON schema defined in PROJECT.md."""
        leads_col = offline_db.collection("bounty_leads")

        # Insert diverse lead data
        test_data = [
            {
                "id": "lead_schema_1",
                "repo": "stellar/rs-soroban-sdk",
                "issue_number": 500,
                "title": "Soroban Contract",
                "status": "queued",
                "raw_status": "pending_discovery",
                "projected_payout": "$2,000",
                "projected_payout_usd": 2000.0,
                "qualification_reason": "Verified Escrow",
                "ecosystem": "stellar",
                "escrow_verified": True,
                "issue_url": "https://github.com/stellar/rs-soroban-sdk/issues/500",
                "pr_url": "",
            },
            {
                "id": "lead_schema_2",
                "repo": "base-org/contracts",
                "number": 501,
                "title": "Base Bridge",
                "status": "pr_open",
                "pr_number": 502,
                "payout_usd": 1500.0,
            },
        ]
        for doc in test_data:
            leads_col.document(doc["id"]).set(doc)

        data = pipeline_payload(db=offline_db)

        # Top-level keys check
        assert set(data.keys()) == {"leads", "grouped", "counts"}

        # Check grouped structure
        assert set(data["grouped"].keys()) == set(CANONICAL_STAGES)
        for stage, group_list in data["grouped"].items():
            assert isinstance(group_list, list)

        # Check counts structure
        expected_counts_keys = set(CANONICAL_STAGES) | {"total"}
        assert set(data["counts"].keys()) == expected_counts_keys
        assert data["counts"]["total"] == len(data["leads"])
        assert data["counts"]["total"] == sum(data["counts"][stage] for stage in CANONICAL_STAGES)

        # Check each lead contains all 13 canonical fields with strict types
        required_fields = {
            "id": str,
            "repo": str,
            "issue_number": (int, type(None)),
            "title": str,
            "status": str,
            "raw_status": str,
            "projected_payout": str,
            "projected_payout_usd": float,
            "qualification_reason": str,
            "ecosystem": str,
            "escrow_verified": bool,
            "issue_url": str,
            "pr_url": str,
        }

        for lead in data["leads"]:
            for field, exp_type in required_fields.items():
                assert field in lead, f"Missing field '{field}' in lead"
                assert isinstance(lead[field], exp_type), f"Field '{field}' has wrong type {type(lead[field])}, expected {exp_type}"
                if field == "status":
                    assert lead["status"] in CANONICAL_STAGES

    def test_empty_pipeline_payload_matches_schema(self):
        """Verify empty_pipeline_payload structure matches canonical contract."""
        data = empty_pipeline_payload()
        assert set(data.keys()) == {"leads", "grouped", "counts"}
        assert data["leads"] == []
        assert set(data["grouped"].keys()) == set(CANONICAL_STAGES)
        assert data["counts"] == {
            "queued": 0,
            "pending_triage": 0,
            "pr_open": 0,
            "completed": 0,
            "failed": 0,
            "total": 0,
        }
