import json

from src.cli import build_parser
from src.cli import main as cli_main
from src.console_server import (
    CANONICAL_STAGES,
    dispatch,
    latest_memory_doc,
    normalize_bounty,
    normalize_status,
    pipeline_payload,
    registry_payload,
)


def test_registry_lists_three_scoped_agents():
    data = registry_payload()
    ids = [agent["id"] for agent in data["agents"]]
    assert ids == ["intake", "executor", "auditor"]
    assert data["policy"]["god_token"] is False
    assert "contents:write" not in data["agents"][2]["tool_scope"]


def test_latest_memory_normalizes_offline_jsonl():
    doc = latest_memory_doc()
    assert doc is not None
    bounty = normalize_bounty(doc)
    assert bounty["source"] == "live"
    assert bounty["pr_url"]
    assert bounty["gcp"]["project"] == "odin-500008"


def test_dispatch_history_and_latest():
    status, ctype, body = dispatch("/api/history")
    assert status == 200
    assert b'"opened": 79' in body
    status, ctype, body = dispatch("/api/bounties/latest")
    assert status == 200
    assert b'"bounty"' in body
    status, _, _ = dispatch("/health")
    assert status == 200


def test_cli_exposes_console_subcommand():
    parser = build_parser()
    args = parser.parse_args(["console", "--port", "8099"])
    assert args.subcommand == "console"
    assert args.port == 8099


def test_cli_console_invokes_server(monkeypatch):
    called = {}

    def fake_serve(host="127.0.0.1", port=8080):
        called["host"] = host
        called["port"] = port

    monkeypatch.setattr("src.console_server.serve_console", fake_serve)
    assert cli_main(["console", "--host", "127.0.0.1", "--port", "8091"]) == 0
    assert called == {"host": "127.0.0.1", "port": 8091}


def test_normalize_status_mapping():
    """Verify all 17 documented statuses map to their canonical 5 stages."""
    # queued
    assert normalize_status("queued") == "queued"
    assert normalize_status("pending_discovery") == "queued"
    assert normalize_status("intake") == "queued"
    # pending_triage
    assert normalize_status("pending_triage") == "pending_triage"
    assert normalize_status("priority_triage") == "pending_triage"
    assert normalize_status("triaged") == "pending_triage"
    # pr_open
    assert normalize_status("pr_open") == "pr_open"
    assert normalize_status("claimed") == "pr_open"
    assert normalize_status("running_orbstack") == "pr_open"
    assert normalize_status("in_progress") == "pr_open"
    assert normalize_status("draft_pr") == "pr_open"
    # completed
    assert normalize_status("completed") == "completed"
    assert normalize_status("merged") == "completed"
    assert normalize_status("paid") == "completed"
    # failed
    assert normalize_status("failed") == "failed"
    assert normalize_status("failed_verification") == "failed"
    assert normalize_status("abandoned") == "failed"
    assert normalize_status("rejected") == "failed"

    # Edge cases: case-insensitivity, whitespace, unknown, empty/None
    assert normalize_status("  PENDING_TRIAGE  ") == "pending_triage"
    assert normalize_status("Draft_PR") == "pr_open"
    assert normalize_status("FAILED_VERIFICATION") == "failed"
    assert normalize_status("unknown_stage_name") == "queued"
    assert normalize_status("") == "queued"
    assert normalize_status(None) == "queued"


def test_pipeline_payload_status_normalization_and_grouping(offline_db):
    """Verify pipeline_payload correctly standardizes statuses and groups leads across all 5 categories."""
    leads_col = offline_db.collection("bounty_leads")

    sample_leads = [
        # Queued category (3)
        {"id": "lead_q1", "repo": "stellar/soroban-sdk", "issue_number": 101, "status": "queued", "projected_payout": "$1500", "projected_payout_usd": 1500.0, "ecosystem": "stellar", "escrow_verified": True},
        {"id": "lead_q2", "repo": "base-org/node", "number": 102, "status": "pending_discovery", "title": "Base node discovery", "ecosystem": "base"},
        {"id": "lead_q3", "repo": "arbitrum/nitro", "issue_number": 103, "status": "intake", "qualification_reason": "Escrow funded"},
        # Pending Triage category (3)
        {"id": "lead_pt1", "repo": "ethereum/go-ethereum", "issue_number": 201, "status": "pending_triage", "payout_usd": 500.0},
        {"id": "lead_pt2", "repo": "optimism/op-geth", "issue_number": 202, "status": "priority_triage"},
        {"id": "lead_pt3", "repo": "polygon/polygon-edge", "issue_number": 203, "status": "triaged"},
        # PR Open category (5)
        {"id": "lead_po1", "repo": "yearn/yearn-vaults", "issue_number": 301, "status": "pr_open", "pr_number": 302},
        {"id": "lead_po2", "repo": "keep3r/contracts", "issue_number": 303, "status": "claimed"},
        {"id": "lead_po3", "repo": "gelato/functions", "issue_number": 304, "status": "running_orbstack"},
        {"id": "lead_po4", "repo": "aave/aave-v3-core", "issue_number": 305, "status": "in_progress"},
        {"id": "lead_po5", "repo": "curvefi/curve-contract", "issue_number": 306, "status": "draft_pr", "pr_url": "https://github.com/curvefi/curve-contract/pull/307"},
        # Completed category (3)
        {"id": "lead_c1", "repo": "uniswap/v3-core", "issue_number": 401, "status": "completed"},
        {"id": "lead_c2", "repo": "sushi/sushiswap", "issue_number": 402, "status": "merged"},
        {"id": "lead_c3", "repo": "balancer/balancer-v2-monorepo", "issue_number": 403, "status": "paid", "payout_usd": 2500.0},
        # Failed category (4)
        {"id": "lead_f1", "repo": "chainlink/contracts", "issue_number": 501, "status": "failed"},
        {"id": "lead_f2", "repo": "compound/compound-protocol", "issue_number": 502, "status": "failed_verification"},
        {"id": "lead_f3", "repo": "makerdao/dss", "issue_number": 503, "status": "abandoned"},
        {"id": "lead_f4", "repo": "synthetixio/synthetix", "issue_number": 504, "status": "rejected"},
    ]

    for item in sample_leads:
        leads_col.document(item["id"]).set(item)

    data = pipeline_payload(db=offline_db)

    assert "leads" in data
    assert "grouped" in data
    assert "counts" in data

    leads = data["leads"]
    assert len(leads) == 18

    counts = data["counts"]
    assert counts["total"] == 18
    assert counts["queued"] == 3
    assert counts["pending_triage"] == 3
    assert counts["pr_open"] == 5
    assert counts["completed"] == 3
    assert counts["failed"] == 4

    grouped = data["grouped"]
    assert len(grouped["queued"]) == 3
    assert len(grouped["pending_triage"]) == 3
    assert len(grouped["pr_open"]) == 5
    assert len(grouped["completed"]) == 3
    assert len(grouped["failed"]) == 4

    # Verify all 13 required fields are present and properly typed on every lead
    for lead in leads:
        assert isinstance(lead["id"], str)
        assert isinstance(lead["repo"], str)
        assert lead["issue_number"] is None or isinstance(lead["issue_number"], int)
        assert isinstance(lead["title"], str)
        assert lead["status"] in CANONICAL_STAGES
        assert isinstance(lead["raw_status"], str)
        assert isinstance(lead["projected_payout"], str)
        assert isinstance(lead["projected_payout_usd"], float)
        assert isinstance(lead["qualification_reason"], str)
        assert isinstance(lead["ecosystem"], str)
        assert isinstance(lead["escrow_verified"], bool)
        assert isinstance(lead["issue_url"], str)
        assert isinstance(lead["pr_url"], str)

    # Spot-check specific lead transformations
    q1 = next(item for item in leads if item["id"] == "lead_q1")
    assert q1["status"] == "queued"
    assert q1["raw_status"] == "queued"
    assert q1["projected_payout_usd"] == 1500.0
    assert q1["projected_payout"] == "$1500"
    assert q1["escrow_verified"] is True
    assert q1["issue_url"] == "https://github.com/stellar/soroban-sdk/issues/101"

    po5 = next(item for item in leads if item["id"] == "lead_po5")
    assert po5["status"] == "pr_open"
    assert po5["raw_status"] == "draft_pr"
    assert po5["pr_url"] == "https://github.com/curvefi/curve-contract/pull/307"


def test_dispatch_api_pipeline(offline_db, monkeypatch):
    """Verify GET /api/pipeline endpoint returns 200, proper Content-Type, and valid JSON structure."""
    monkeypatch.setattr("src.console_server.get_firestore_client", lambda: offline_db)

    offline_db.collection("bounty_leads").document("lead_test_1").set({
        "repo": "stellar/soroban-examples",
        "issue_number": 77,
        "title": "Add timelock contract test",
        "status": "triaged",
        "projected_payout_usd": 750.0,
    })

    status, ctype, body = dispatch("/api/pipeline")
    assert status == 200
    assert ctype == "application/json; charset=utf-8"

    payload = json.loads(body.decode("utf-8"))
    assert "leads" in payload
    assert "grouped" in payload
    assert "counts" in payload

    assert payload["counts"]["total"] == 1
    assert payload["counts"]["pending_triage"] == 1
    assert payload["leads"][0]["repo"] == "stellar/soroban-examples"
    assert payload["leads"][0]["status"] == "pending_triage"
    assert payload["leads"][0]["raw_status"] == "triaged"


def test_pipeline_payload_error_handling(monkeypatch):
    """Verify database exceptions are safely caught and return safe empty defaults."""
    def fail_client():
        raise RuntimeError("Simulated Firestore failure")

    monkeypatch.setattr("src.console_server.get_firestore_client", fail_client)

    payload = pipeline_payload()
    assert payload["leads"] == []
    assert payload["grouped"] == {"queued": [], "pending_triage": [], "pr_open": [], "completed": [], "failed": []}
    assert payload["counts"] == {"queued": 0, "pending_triage": 0, "pr_open": 0, "completed": 0, "failed": 0, "total": 0}

    # Dispatch should also return 200 with empty defaults rather than crashing
    status, ctype, body = dispatch("/api/pipeline")
    assert status == 200
    assert ctype == "application/json; charset=utf-8"
    dispatched_payload = json.loads(body.decode("utf-8"))
    assert dispatched_payload == payload


def test_static_routing_pipeline_and_spa(temp_workspace, monkeypatch):
    """Verify static routing serves pipeline.html, index.html, assets, and handles SPA fallback."""
    console_dir = temp_workspace / "static" / "console"
    console_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = console_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    index_html = "<html><head><title>Fleet Console SPA</title></head><body>Root SPA</body></html>"
    pipeline_html = "<html><head><title>3D Pipeline</title></head><body>Pipeline UI Canvas</body></html>"
    style_css = "body { background: #000; color: #0ff; }"

    (console_dir / "index.html").write_text(index_html)
    (console_dir / "pipeline.html").write_text(pipeline_html)
    (assets_dir / "style.css").write_text(style_css)

    monkeypatch.setattr("src.console_server.CONSOLE_DIR", console_dir)

    # 1. Root /console and /console/
    status, ctype, body = dispatch("/console")
    assert status == 200
    assert "text/html" in ctype
    assert body.decode() == index_html

    status, ctype, body = dispatch("/console/")
    assert status == 200
    assert "text/html" in ctype
    assert body.decode() == index_html

    # 2. Specific pipeline.html file
    status, ctype, body = dispatch("/console/pipeline.html")
    assert status == 200
    assert "text/html" in ctype
    assert body.decode() == pipeline_html

    # 3. Static asset (CSS)
    status, ctype, body = dispatch("/console/assets/style.css")
    assert status == 200
    assert "text/css" in ctype
    assert body.decode() == style_css

    # 4. Unknown route -> SPA fallback to index.html
    status, ctype, body = dispatch("/console/routes/unknown/nested")
    assert status == 200
    assert "text/html" in ctype
    assert body.decode() == index_html


def test_static_routing_security_and_missing_build(temp_workspace, monkeypatch):
    """Verify path traversal attacks are rejected and missing build returns 503."""
    console_dir = temp_workspace / "static" / "console"
    console_dir.mkdir(parents=True, exist_ok=True)

    secret_file = temp_workspace / "secret.key"
    secret_file.write_text("SUPER_SECRET_KEY")

    monkeypatch.setattr("src.console_server.CONSOLE_DIR", console_dir)

    # When index.html is missing, /console returns 503 build hint
    status, ctype, body = dispatch("/console")
    assert status == 503
    assert "text/html" in ctype
    assert b"Build the console first" in body

    # Path traversal attempts outside CONSOLE_DIR return 404
    (console_dir / "index.html").write_text("<html>index</html>")
    status, ctype, body = dispatch("/console/../../secret.key")
    assert status == 404
    assert b"SUPER_SECRET_KEY" not in body

