"""
GitHub API Client Module (GraphQL & REST).

Provides shared access to GitHub GraphQL and REST endpoints via the `gh` CLI
binary and/or direct HTTPS requests, with automatic pagination, rate-limit backoff,
and error handling.
"""

import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, Optional

from src.core.exceptions import GitHubAPIError

logger = logging.getLogger("UniversalBountyV2.GitHubClient")

# GraphQL search query template for issue intake
GRAPHQL_SEARCH_QUERY = """
query SearchBounties($q: String!, $cursor: String, $first: Int = 25) {
  search(query: $q, type: ISSUE, first: $first, after: $cursor) {
    issueCount
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on Issue {
        id
        number
        title
        url
        body
        state
        createdAt
        updatedAt
        author {
          login
        }
        repository {
          nameWithOwner
          isArchived
          isFork
          stargazerCount
          primaryLanguage {
            name
          }
        }
        labels(first: 15) {
          nodes {
            name
          }
        }
        comments(first: 10) {
          totalCount
          nodes {
            author {
              login
            }
            body
            createdAt
          }
        }
      }
    }
  }
}
"""

GRAPHQL_PR_ROLLUP_QUERY = """
query GetPRRollup($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      id
      number
      title
      state
      isDraft
      mergeable
      url
      createdAt
      updatedAt
      comments(last: 20) {
        nodes {
          author {
            login
          }
          body
          createdAt
        }
      }
      reviews(last: 10) {
        nodes {
          author {
            login
          }
          state
          body
          submittedAt
        }
      }
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              state
              contexts(first: 50) {
                nodes {
                  ... on CheckRun {
                    name
                    conclusion
                    status
                    detailsUrl
                  }
                  ... on StatusContext {
                    context
                    state
                    targetUrl
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


class GitHubClient:
    """
    Client for GitHub GraphQL and REST operations.
    Defaults to invoking `gh` CLI (which leverages existing authenticated credentials),
    falling back to direct HTTPS requests if needed.
    """

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("GITHUB_TOKEN")

    def query_graphql(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        backoff_sec: float = 2.0,
    ) -> Dict[str, Any]:
        """
        Executes a GraphQL query via the `gh api graphql` command.
        """
        cmd = ["gh", "api", "graphql"]
        if variables:
            for k, v in variables.items():
                if v is not None:
                    if isinstance(v, (int, float, bool)):
                        cmd.extend(["-F", f"{k}={v}"])
                    else:
                        cmd.extend(["-f", f"{k}={str(v)}"])
        cmd.extend(["-f", f"query={query}"])

        for attempt in range(1, max_retries + 1):
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                if res.returncode == 0:
                    try:
                        return json.loads(res.stdout)
                    except json.JSONDecodeError as e:
                        raise GitHubAPIError(f"Failed to parse GraphQL JSON response: {e}")

                stderr_lower = res.stderr.lower()
                if "rate limit" in stderr_lower or "secondary rate" in stderr_lower:
                    wait_time = backoff_sec * (2 ** (attempt - 1))
                    logger.warning(
                        f"GitHub GraphQL rate limited (attempt {attempt}/{max_retries}); backing off {wait_time:.1f}s"
                    )
                    time.sleep(wait_time)
                    continue

                logger.error(f"GitHub GraphQL query error ({res.returncode}): {res.stderr.strip()}")
                if attempt == max_retries:
                    raise GitHubAPIError(
                        f"GitHub GraphQL query failed: {res.stderr.strip()}",
                        status_code=res.returncode,
                        response_body=res.stderr,
                    )
            except subprocess.TimeoutExpired:
                logger.warning(f"GitHub GraphQL query timed out (attempt {attempt}/{max_retries})")
                if attempt == max_retries:
                    raise GitHubAPIError("GitHub GraphQL query timed out after multiple attempts")
                time.sleep(backoff_sec)
            except Exception as e:
                if attempt == max_retries:
                    raise GitHubAPIError(f"Unexpected error in query_graphql: {e}") from e
                time.sleep(backoff_sec)

        return {}

    def search_bounties(
        self,
        query_str: str,
        cursor: Optional[str] = None,
        page_size: int = 25,
    ) -> Dict[str, Any]:
        """
        Executes a bounty search using the standard GraphQL query.
        """
        variables = {"q": query_str, "first": page_size}
        if cursor:
            variables["cursor"] = cursor
        return self.query_graphql(GRAPHQL_SEARCH_QUERY, variables)

    def get_pr_rollup(self, repo: str, pr_number: int) -> Dict[str, Any]:
        """
        Fetches full PR status check rollup, reviews, and comments.
        """
        if "/" not in repo:
            raise ValueError(f"Invalid repo format (expected owner/repo): {repo}")
        owner, repo_name = repo.split("/", 1)
        variables = {"owner": owner, "repo": repo_name, "number": pr_number}
        return self.query_graphql(GRAPHQL_PR_ROLLUP_QUERY, variables)

    def check_repo_archived(self, repo: str) -> bool:
        """
        Checks whether a repository is archived using `gh repo view`.
        """
        cmd = ["gh", "repo", "view", repo, "--json", "isArchived"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                return bool(data.get("isArchived", False))
        except Exception as e:
            logger.warning(f"Could not check archive status for {repo}: {e}")
        return False

    def get_issue(self, repo: str, issue_number: int) -> Dict[str, Any]:
        """
        Fetches issue details via `gh issue view`.
        """
        cmd = [
            "gh",
            "issue",
            "view",
            f"{repo}#{issue_number}",
            "--json",
            "id,number,title,body,state,labels,comments,url,author",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if res.returncode == 0:
                return json.loads(res.stdout)
            raise GitHubAPIError(f"Failed to view issue {repo}#{issue_number}: {res.stderr.strip()}")
        except Exception as e:
            if isinstance(e, GitHubAPIError):
                raise
            raise GitHubAPIError(f"Error fetching issue {repo}#{issue_number}: {e}") from e

    def add_comment(self, repo: str, issue_or_pr_number: int, body: str) -> Dict[str, Any]:
        """
        Adds a comment to an issue or pull request.
        """
        cmd = [
            "gh",
            "issue",
            "comment",
            f"{repo}#{issue_or_pr_number}",
            "--body",
            body,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if res.returncode == 0:
                return {"status": "success", "output": res.stdout.strip()}
            raise GitHubAPIError(f"Failed to add comment to {repo}#{issue_or_pr_number}: {res.stderr.strip()}")
        except Exception as e:
            if isinstance(e, GitHubAPIError):
                raise
            raise GitHubAPIError(f"Error adding comment to {repo}#{issue_or_pr_number}: {e}") from e

    def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool = True,
        cwd: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Creates a new pull request (defaulting to Draft).
        """
        cmd = [
            "gh",
            "pr",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--body",
            body,
            "--head",
            head,
            "--base",
            base,
        ]
        if draft:
            cmd.append("--draft")

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=cwd)
            if res.returncode == 0:
                pr_url = res.stdout.strip()
                return {"status": "success", "pr_url": pr_url}
            raise GitHubAPIError(f"Failed to create PR for {repo}: {res.stderr.strip()}")
        except Exception as e:
            if isinstance(e, GitHubAPIError):
                raise
            raise GitHubAPIError(f"Error creating PR for {repo}: {e}") from e


# Global singleton instance
_GITHUB_CLIENT_INSTANCE: Optional[GitHubClient] = None


def get_github_client() -> GitHubClient:
    """Returns the shared GitHubClient singleton."""
    global _GITHUB_CLIENT_INSTANCE
    if _GITHUB_CLIENT_INSTANCE is None:
        _GITHUB_CLIENT_INSTANCE = GitHubClient()
    return _GITHUB_CLIENT_INSTANCE
