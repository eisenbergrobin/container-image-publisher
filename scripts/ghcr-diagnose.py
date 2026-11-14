#!/usr/bin/env python3
"""Report what ghcr.io already knows about a package name, before we blame the workflow.

A `permission_denied: write_package` on a push says the registry refused. It does
not say why, and three different situations look identical: the package name is
owned by another repository in the same account, the account itself is gated, or
the token was never meant to write packages. What does separate them is what the
registry will happily show for reads:

    tags list       a name that already has tags is not a free name
    manifest        content at 'latest' means somebody published it before
    token request   whether the registry will issue a scoped token at all

Read capability is deliberately not a stand-in for write capability: ghcr.io
answers writes by attempting them, and the probe step in the workflow does that
with a 1 KB image under a name the account has never used.

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

# Most images are indexes/manifest lists. Asking only for a single manifest makes
# the registry answer "OCI index found, but Accept header does not support OCI
# indexes" - true, but it hides the fact that the manifest exists.
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


def fetch(url: str, headers: dict[str, str]) -> tuple[int, str, dict[str, str]]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers.items())
    except urllib.error.URLError as exc:
        return 0, str(exc.reason), {}


def main() -> int:
    image = os.environ.get("IMAGE", "")
    user = os.environ.get("GHCR_USER", "")
    secret = os.environ.get("GHCR_TOKEN", "")
    if image.count("/") != 2 or not user or not secret:
        print("ghcr-diagnose: needs IMAGE (ghcr.io/owner/name), GHCR_USER, GHCR_TOKEN")
        return 2

    registry, _, repo = image.partition("/")
    print(f"registry: {registry}   name: {repo}")

    status, body, _ = fetch(
        f"https://{registry}/token?service={registry}&scope=repository:{repo}:pull",
        {"Authorization": basic(user, secret)},
    )
    print(f"token request -> HTTP {status}" + ("" if status == 200 else f": {body[:240]}"))
    if status != 200:
        print("  the registry refused even a pull scope for this name; that is the account or")
        print("  the credential being refused outright, not a name collision.")
        return 0
    token = json.loads(body).get("token", "")
    auth = {"Authorization": f"Bearer {token}"}

    tag_status, body, _ = fetch(f"https://{registry}/v2/{repo}/tags/list", auth)
    print(f"tags -> HTTP {tag_status}: {body[:300]}")
    tags: list[str] = []
    if tag_status == 200:
        try:
            tags = json.loads(body).get("tags") or []
        except json.JSONDecodeError:
            pass

    status, body, headers = fetch(f"https://{registry}/v2/{repo}/manifests/latest",
                                  {**auth, "Accept": ACCEPT})
    digest = headers.get("Docker-Content-Digest") or headers.get("docker-content-digest") or "?"
    media = ""
    if status == 200:
        try:
            media = json.loads(body).get("mediaType", "")
        except json.JSONDecodeError:
            pass
    print(f"manifest 'latest' -> HTTP {status} digest {digest} {media}")

    print()
    if tags:
        print(f"this name is not free: {len(tags)} tag(s) already published - {', '.join(tags[:12])}")
        print("A package is linked to exactly one repository. If this workflow's repository is")
        print("not the one it is linked to, no token from here will ever be allowed to write it.")
    elif tag_status == 200 or status == 200:
        print("the registry answers for this name but shows no tags: it is registered to someone,")
        print("and this repository is not the one allowed to write it.")
    else:
        print("no published tags visible under this name, so the name looks free and the refusal")
        print("is coming from the account or the credential rather than a collision.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
