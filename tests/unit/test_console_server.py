"""Unit tests for the Fleet Console server wired into V2."""

from src.cli import build_parser, main as cli_main
from src.console_server import dispatch, latest_memory_doc, normalize_bounty, registry_payload


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
