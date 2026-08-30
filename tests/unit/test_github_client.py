"""
Unit Tests for Shared GitHub Client Module (Milestone 1).
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.core.exceptions import GitHubAPIError
from src.core.github_client import GitHubClient, get_github_client


class TestGitHubClient:
    """Verifies GitHub GraphQL, issue, and PR operations with retry and error handling."""

    def test_singleton_getter(self):
        client1 = get_github_client()
        client2 = get_github_client()
        assert client1 is client2
        assert isinstance(client1, GitHubClient)

    def test_query_graphql_success(self):
        client = GitHubClient(token="dummy_token")
        fake_response = {
            "data": {
                "search": {
                    "issueCount": 1,
                    "nodes": [{"id": "I_123", "number": 1, "title": "Bounty Issue"}],
                }
            }
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps(fake_response)

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            res = client.query_graphql("query { viewer { login } }", variables={"var1": "val1"})
            assert res == fake_response
            mock_run.assert_called_once()
            cmd_args = mock_run.call_args[0][0]
            assert "gh" in cmd_args
            assert "graphql" in cmd_args

    def test_query_graphql_retry_on_rate_limit(self):
        client = GitHubClient()

        mock_fail = MagicMock()
        mock_fail.returncode = 1
        mock_fail.stderr = "API rate limit exceeded"

        mock_success = MagicMock()
        mock_success.returncode = 0
        mock_success.stdout = json.dumps({"data": {"status": "ok"}})

        with patch("subprocess.run", side_effect=[mock_fail, mock_success]) as mock_run:
            with patch("time.sleep") as mock_sleep:
                res = client.query_graphql("query { test }", max_retries=2, backoff_sec=0.1)
                assert res == {"data": {"status": "ok"}}
                assert mock_run.call_count == 2
                mock_sleep.assert_called_once_with(0.1)

    def test_query_graphql_failure_raises_github_api_error(self):
        client = GitHubClient()
        mock_fail = MagicMock()
        mock_fail.returncode = 1
        mock_fail.stderr = "Could not resolve to a Repository"

        with patch("subprocess.run", return_value=mock_fail):
            with pytest.raises(GitHubAPIError) as exc_info:
                client.query_graphql("query { badQuery }", max_retries=1)
            assert "GitHub GraphQL query failed" in str(exc_info.value)

    def test_search_bounties(self):
        client = GitHubClient()
        fake_search_resp = {
            "data": {
                "search": {
                    "issueCount": 5,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"id": "I_1", "number": 1, "title": "Lead 1"}],
                }
            }
        }

        with patch.object(client, "query_graphql", return_value=fake_search_resp) as mock_query:
            res = client.search_bounties("is:issue grantfox", page_size=10)
            assert res == fake_search_resp
            mock_query.assert_called_once()
            args, kwargs = mock_query.call_args
            assert kwargs == {} or kwargs.get("variables") is not None
            assert args[1]["q"] == "is:issue grantfox"
            assert args[1]["first"] == 10

    def test_check_repo_archived(self):
        client = GitHubClient()

        # Archived repo
        mock_archived = MagicMock()
        mock_archived.returncode = 0
        mock_archived.stdout = json.dumps({"isArchived": True})

        with patch("subprocess.run", return_value=mock_archived):
            assert client.check_repo_archived("owner/archived-repo") is True

        # Active repo
        mock_active = MagicMock()
        mock_active.returncode = 0
        mock_active.stdout = json.dumps({"isArchived": False})

        with patch("subprocess.run", return_value=mock_active):
            assert client.check_repo_archived("owner/active-repo") is False

    def test_get_issue_and_create_pr(self):
        client = GitHubClient()

        # get_issue
        mock_issue = MagicMock()
        mock_issue.returncode = 0
        mock_issue.stdout = json.dumps({"id": "I_42", "number": 42, "title": "Bounty"})

        with patch("subprocess.run", return_value=mock_issue):
            issue = client.get_issue("owner/repo", 42)
            assert issue["number"] == 42

        # create_pull_request
        mock_pr = MagicMock()
        mock_pr.returncode = 0
        mock_pr.stdout = "https://github.com/owner/repo/pull/1"

        with patch("subprocess.run", return_value=mock_pr):
            pr_res = client.create_pull_request(
                repo="owner/repo",
                title="Fix issue 42",
                body="Draft PR",
                head="fix-42",
                draft=True,
            )
            assert pr_res["status"] == "success"
            assert pr_res["pr_url"] == "https://github.com/owner/repo/pull/1"
