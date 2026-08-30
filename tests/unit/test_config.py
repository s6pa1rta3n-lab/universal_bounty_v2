"""
Unit Tests for Configuration & Security Rules (Milestone 1).
"""

import pytest

from src.core.config import (
    BANNED_PLATFORMS,
    DEFAULT_GCP_PROJECT_ID,
    DEFAULT_IGNORE_LIST,
    EVM_PAYOUT_ADDRESS,
    PAYOUT_ROUTING,
    SOLANA_PAYOUT_ADDRESS,
    STELLAR_PAYOUT_ADDRESS,
    SwarmConfig,
    contains_disqualify_keywords,
    get_config,
    is_banned_platform,
)


class TestConfigSecurityRules:
    """Verifies strict security constraints, banned platforms, and payout addresses."""

    def test_banned_platforms_list(self):
        expected_banned = ["algora", "polar", "twentyhq/twenty", "twentyhq", "opire"]
        for p in expected_banned:
            assert p in BANNED_PLATFORMS

    def test_is_banned_platform_detection(self):
        # Exact names
        assert is_banned_platform("algora") is True
        assert is_banned_platform("polar") is True
        assert is_banned_platform("twentyhq/twenty") is True
        assert is_banned_platform("twentyhq") is True
        assert is_banned_platform("opire") is True

        # Variations and domains
        assert is_banned_platform("https://algora.io/bounties/123") is True
        assert is_banned_platform("polar.sh/issue/45") is True
        assert is_banned_platform("opire.dev/reward") is True
        assert is_banned_platform("github.com/twentyhq/twenty/issues/1") is True
        assert is_banned_platform("Polar-Bot rewarded $50") is True

        # Valid approved platforms
        assert is_banned_platform("grantfox") is False
        assert is_banned_platform("https://github.com/stellar/soroban-example") is False
        assert is_banned_platform("gitcoin") is False
        assert is_banned_platform("bounties-network") is False
        assert is_banned_platform(None) is False
        assert is_banned_platform("") is False

    def test_default_ignore_list_paths(self):
        assert "~/teamwork_projects/keeper_daemon" in DEFAULT_IGNORE_LIST
        assert "~/teamwork_projects/odin" in DEFAULT_IGNORE_LIST
        assert "~/teamwork_projects/matt-berserker" in DEFAULT_IGNORE_LIST

    def test_payout_routing_addresses(self):
        assert EVM_PAYOUT_ADDRESS == "0xF46C9F6d70C50BF81ef3588AB523a90a594a2F89"
        assert STELLAR_PAYOUT_ADDRESS == "GCL6OXAMLD75BMTINA6EMRUDWK5THQUSHMYNLSNBCJAPZJHNYJTUNIBC"
        assert SOLANA_PAYOUT_ADDRESS == "MSwhtUP1XaRfMf5ectKq9LnGvQxcQxxMHJo4QNaJ6Av"

        assert PAYOUT_ROUTING["EVM"] == EVM_PAYOUT_ADDRESS
        assert PAYOUT_ROUTING["STELLAR"] == STELLAR_PAYOUT_ADDRESS
        assert PAYOUT_ROUTING["SOLANA"] == SOLANA_PAYOUT_ADDRESS

    def test_disqualify_keywords_filter(self):
        # Disqualified texts
        disq1, kw1 = contains_disqualify_keywords("Please record a video pitch explaining your solution.")
        assert disq1 is True
        assert kw1 in ["video pitch", "record a video"]

        disq2, kw2 = contains_disqualify_keywords("Submit a loom walkthrough of your demo.")
        assert disq2 is True
        assert kw2 in ["loom", "loom.com"]

        disq3, kw3 = contains_disqualify_keywords("Requires live Zoom interview with the founders.")
        assert disq3 is True
        assert kw3 in ["interview", "zoom interview", "zoom call"]

        disq4, kw4 = contains_disqualify_keywords("Manual KYC required before payout.")
        assert disq4 is True
        assert kw4 in ["manual kyc", "kyc required"]

        # Valid technical descriptions
        disq_ok, kw_ok = contains_disqualify_keywords("Implement Soroban smart contract unit tests and Rust CLI.")
        assert disq_ok is False
        assert kw_ok is None

    def test_swarm_config_singleton_and_immutability(self):
        cfg = get_config()
        assert isinstance(cfg, SwarmConfig)
        assert cfg.evm_payout_address == EVM_PAYOUT_ADDRESS
        assert cfg.stellar_payout_address == STELLAR_PAYOUT_ADDRESS
        assert cfg.gcp_project_id == DEFAULT_GCP_PROJECT_ID

        # Verify frozen immutability
        with pytest.raises(Exception):
            cfg.evm_payout_address = "0x0000000000000000000000000000000000000000"

    def test_swarm_config_environment_overrides(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT_ID", "custom-gcp-project")
        monkeypatch.setenv("SWARM_CONTAINER_CPUS", "4")
        monkeypatch.setenv("SWARM_CONTAINER_MEMORY", "4g")

        cfg = get_config(force_reload=True)
        assert cfg.gcp_project_id == "custom-gcp-project"
        assert cfg.container_cpus == "4"
        assert cfg.container_memory == "4g"

        # Reset config
        monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
        monkeypatch.delenv("SWARM_CONTAINER_CPUS", raising=False)
        monkeypatch.delenv("SWARM_CONTAINER_MEMORY", raising=False)
        get_config(force_reload=True)
