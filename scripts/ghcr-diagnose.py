#!/usr/bin/env python3
"""Ask ghcr.io, in the open, whether this repository may write a package.

Behavioural, cheap, and leaves nothing behind: the registry answers the same
question docker/buildx is about to ask, one HTTP call at a time.

    pull check   GET  /v2/<name>/manifests/<tag>
    push check   POST /v2/<name>/blobs/uploads/     202 = may write
                 the upload is never committed, so it creates no package,
                 no blob and no tag

The point is to tell three otherwise identical-looking denials apart:

    package name already owned by another repository  real name refused,
                                                      a fresh name accepted
    account gated (terms of service, billing)         both refused
    transient/registry fault                          both accepted, build
                                                      still fails

Neither the workflow token nor the registry token it receives is ever printed.

Env: IMAGE (ghcr.io/owner/name), GHCR_USER, GHCR_TOKEN
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

# A manifest list/index is what most images actually are; asking for only a
# manifest makes the registry answer "OCI index found, but Accept header..."
# which is a clue in itself but hides the status we want.
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def basic(user: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


def fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def registry_token(registry: str, repo: str, user: str, secret: str) -> tuple[str, str, int]:
    """(token, raw body, status). The token is returned for use, never printed."""
    scope = f"repository:{repo}:push,pull"
    status, body = fetch(
        f"https://{registry}/token?service={registry}&scope={scope}",
        {"Authorization": basic(user, secret)},
    )
    if status != 200:
        return "", body, status
    try:
        return json.loads(body).get("token", ""), "", status
    except json.JSONDecodeError:
        return "", body, status


def probe(name: str, registry: str, user: str, secret: str) -> bool:
    """Return True if the registry would let this credential write `name`."""
    token, body, status = registry_token(registry, name, user, secret)
    print(f"\npackage: {name}")
    if status != 200:
        print(f"  token request -> HTTP {status}: {body[:300]}")
        return False
    if not token:
        print("  token request returned 200 but no token")
        return False

    status, body = fetch(f"https://{registry}/v2/{name}/manifests/latest",
                         {"Authorization": f"Bearer {token}", "Accept": ACCEPT})
    read = {200: "exists and is readable", 404: "no manifest at latest",
            401: "read refused", 403: "read refused"}.get(status, f"HTTP {status}")
    print(f"  read  latest -> {status} ({read}) {body[:160] if status >= 400 else ''}")

    status, body = fetch(f"https://{registry}/v2/{name}/blobs/uploads/",
                         {"Authorization": f"Bearer {token}"})
    may_write = status in (201, 202, 204)
    verdict = "MAY WRITE" if may_write else "refused"
    print(f"  write probe -> {status} ({verdict}) {body[:200] if not may_write else ''}")
    return may_write


def main() -> int:
    image = os.environ.get("IMAGE", "")
    user = os.environ.get("GHCR_USER", "")
    secret = os.environ.get("GHCR_TOKEN", "")
    if image.count("/") != 2 or not user or not secret:
        print("ghcr-diagnose: needs IMAGE (ghcr.io/owner/name), GHCR_USER, GHCR_TOKEN")
        return 2

    registry, _, repo = image.partition("/")
    print(f"registry: {registry}")

    configured = probe(repo, registry, user, secret)
    # A name this account has certainly never used. If this one is writable and
    # the configured one is not, the block is the package name, not the account.
    throwaway = f"{repo}-pushcheck"
    scratch = probe(throwaway, registry, user, secret)

    print("\nverdict:")
    if configured and scratch:
        print("  both writable: the denial is not permission-shaped; look again at the build")
        return 0
    if not configured and scratch:
        print(f"  {repo} is refused while {throwaway} would be accepted.")
        print("  That package name is already owned by another repository in this account -")
        print("  ghcr.io links a package to exactly one repository and will not let a second")
        print("  one write it. Delete the old package or use a different name in containers.yml.")
        return 3
    if configured and not scratch:
        print("  the configured name is writable but a fresh name is not: unexpected, and")
        print("  most likely a stale package-link for the new name. Not a blocker here.")
        return 0
    print("  no package name is writable with this credential: the account or the token is")
    print("  gated (terms of service, billing category, or a token without package write).")
    return 4


if __name__ == "__main__":
    sys.exit(main())
