#!/usr/bin/env python3
"""Explain why ghcr.io refused a push, without ever printing a credential.

The runner's GITHUB_TOKEN had `Packages: write` and GHCR still answered
`permission_denied: write_package`, so the block is somewhere between the
credential and the account. These two facts separate the causes:

  - what scopes GHCR is willing to grant this credential for this repository
    (the JWT it hands out says so; we decode it and print only the access list)
  - what a plain read of the same repository answers (404 = not created yet and
    reads are fine, 401/403 = the account itself is gated)

Never prints the workflow token or the registry token it receives.

Env: IMAGE (ghcr.io/owner/name), GHCR_USER, GHCR_TOKEN
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def access_claims(registry_token: str) -> object:
    """The `access` list from the JWT payload. The signature is not verified and is not needed."""
    try:
        payload = registry_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("access")
    except Exception as exc:                                    # noqa: BLE001
        return f"undecodable ({exc})"


def main() -> int:
    image = os.environ.get("IMAGE", "")
    user = os.environ.get("GHCR_USER", "")
    token = os.environ.get("GHCR_TOKEN", "")
    if not image.count("/") == 2 or not user or not token:
        print("ghcr-diagnose: needs IMAGE (ghcr.io/owner/name), GHCR_USER, GHCR_TOKEN")
        return 2

    registry, _, repo = image.partition("/")
    print(f"registry: {registry}   repository: {repo}")

    status, body = fetch(f"https://{registry}/v2/", {})
    print(f"GET /v2/ -> {status} {body[:120]}")

    status, body = fetch(
        f"https://{registry}/token?service={registry}&scope=repository:{repo}:push,pull",
        {"Authorization": basic(user, token)},
    )
    print(f"token endpoint (push,pull) -> HTTP {status}")
    registry_token = ""
    if status == 200:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
            print(f"  body is not JSON: {body[:200]}")
        registry_token = payload.get("token", "")
        issued = payload.get("issued_at", "")
        print(f"  issued_at: {issued}")
        print(f"  granted access: {json.dumps(access_claims(registry_token))}")
        granted = access_claims(registry_token)
        actions = set()
        if isinstance(granted, list):
            for entry in granted:
                actions.update(entry.get("actions", []))
        if "push" not in actions:
            print("  -> GHCR will not grant push to this credential for this repository.")
            print("     The workflow token itself is not the problem; the registry is refusing")
            print("     the capability. That is the account-gated case (terms of service,")
            print("     billing category, or a package name owned elsewhere).")
    else:
        print(f"  error body: {body[:400]}")
        print("  -> GHCR refused to issue a scoped token; the body above is the reason it gives.")
        return 0

    if registry_token:
        auth = {"Authorization": f"Bearer {registry_token}"}
        for ref in ("latest", "push-probe"):
            status, body = fetch(f"https://{registry}/v2/{repo}/manifests/{ref}",
                                 {**auth, "Accept": "application/vnd.oci.image.manifest.v1+json"})
            print(f"HEAD-equivalent manifests/{ref} -> HTTP {status} {body[:160]}")
        print("  -> 401/403 above with push granted means the account, not the token, is gated.")
        print("     404 means reads are fine and the package simply does not exist yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
