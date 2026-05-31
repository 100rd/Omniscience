"""GitHub source discovery connector."""

from __future__ import annotations

from typing import Any
import httpx
import structlog
from omniscience_connectors.discovery import DiscoveredSource

log = structlog.get_logger(__name__)

class GitHubDiscoveryConnector:
    """Discovers repositories within a GitHub organization."""

    async def discover(self, config: dict[str, Any], secrets: dict[str, str]) -> list[DiscoveredSource]:
        org_name = config.get("org")
        token = secrets.get("github_token")
        
        if not org_name or not token:
            log.error("github_discovery_missing_config", org=org_name, has_token=bool(token))
            return []

        discovered = []
        include_pattern = config.get("include_pattern")
        exclude_repos = set(config.get("exclude_repos", []))
        
        import re

        async with httpx.AsyncClient() as client:
            # GitHub API: List organization repositories
            url = f"https://api.github.com/orgs/{org_name}/repos"
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json"
            }
            
            params = {"per_page": 100, "type": config.get("repo_type", "all")}
            
            try:
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()
                repos = response.json()
                
                for repo in repos:
                    name = repo["name"]
                    
                    # 1. Denylist check (exact name)
                    if name in exclude_repos:
                        continue
                    
                    # 2. Archive check
                    if repo.get("archived"):
                        continue
                    
                    # 3. Allowlist check (regex pattern)
                    if include_pattern and not re.search(include_pattern, name):
                        continue
                        
                    discovered.append(DiscoveredSource(
                        name=name,
                        type="git",
                        uri=repo["clone_url"],
                        external_id=str(repo["id"]),
                        metadata={
                            "description": repo.get("description"),
                            "language": repo.get("language"),
                            "stars": repo.get("stargazers_count"),
                            "github_full_name": repo["full_name"]
                        }
                    ))
                
                log.info("github_discovery_success", org=org_name, count=len(discovered))
            except Exception as exc:
                log.error("github_discovery_failed", org=org_name, error=str(exc))
                
        return discovered
