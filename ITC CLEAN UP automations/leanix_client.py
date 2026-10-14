"""
Shared LeanIX auth and GraphQL helpers.

Configure via environment variables (or a .env file):
  LEANIX_API_TOKEN        — API key / personal access token
  LEANIX_WORKSPACE_HOST   — hostname only, e.g. demo-us.leanix.net
  LEANIX_WORKSPACE        — workspace slug, e.g. HarclerodeDemo
"""

import os
import requests

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

API_TOKEN = os.getenv("LEANIX_API_TOKEN", "")
HOST      = os.getenv("LEANIX_WORKSPACE_HOST", "yourworkspace.leanix.net")
WORKSPACE = os.getenv("LEANIX_WORKSPACE", "Your workspace details")


def get_access_token() -> str:
    resp = requests.post(
        f"https://{HOST}/services/mtm/v1/oauth2/token",
        auth=("apitoken", API_TOKEN),
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def graphql(token: str, query: str, variables: dict | None = None, url: str | None = None) -> dict:
    resp = requests.post(
        url or f"https://{HOST}/{WORKSPACE}/graphql",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL error: {data['errors']}")
    return data["data"]
