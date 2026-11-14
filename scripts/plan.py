#!/usr/bin/env python3
"""Turn containers.yml entries into exact upstream commits and image tags.

Modes
  --validate        check containers.yml, its schema, and that each upstream Dockerfile exists
  --list            print the enabled app names, one per line (no network)
  --resolve APP     resolve APP to an exact ref/sha and emit key=value lines for GITHUB_OUTPUT
  --smoke APP       write APP's smoke-test script to --out FILE (no network)

Exit non-zero on any problem: a malformed entry should break the weekly run loudly
rather than quietly drop an app from the matrix.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
REGISTRY = os.environ.get("REGISTRY", "ghcr.io")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
UPSTREAM_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
SEMVER_RE = re.compile(r"^(v?)(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
TRACKS = ("release", "branch")

KNOWN_KEYS = {
    "name", "upstream", "enabled", "track", "branch", "dockerfile", "context",
    "platforms", "build_args", "series_tags", "publish_latest", "labels", "smoke_test",
}


def fail(msg: str) -> None:
    print(f"plan: {msg}", file=sys.stderr)
    raise SystemExit(1)


def load_apps(path: str) -> list[dict]:
    try:
        import yaml
    except ImportError:
        fail("PyYAML is required: pip install pyyaml")

    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except OSError as exc:
        fail(f"cannot read {path}: {exc}")
    except yaml.YAMLError as exc:
        fail(f"{path} is not valid YAML: {exc}")

    apps = (doc or {}).get("apps")
    if not isinstance(apps, list) or not apps:
        fail(f"{path} needs a non-empty 'apps:' list")

    seen: set[str] = set()
    for app in apps:
        if not isinstance(app, dict):
            fail("every app entry must be a mapping")
        name = app.get("name")
        where = f"app {name!r}"
        if not isinstance(name, str) or not NAME_RE.match(name):
            fail(f"bad name {name!r}: lowercase letters, digits, dot, dash, underscore")
        if name in seen:
            fail(f"duplicate app name {name!r}")
        seen.add(name)

        unknown = set(app) - KNOWN_KEYS
        if unknown:
            fail(f"{where}: unknown key(s) {sorted(unknown)}")
        if not isinstance(app.get("upstream"), str) or not UPSTREAM_RE.match(str(app.get("upstream"))):
            fail(f"{where}: 'upstream' must look like owner/repo")
        track = app.get("track", "release")
        if track not in TRACKS:
            fail(f"{where}: 'track' must be one of {TRACKS}")
        if track == "branch" and not isinstance(app.get("branch"), str):
            fail(f"{where}: track 'branch' needs a 'branch:' name")
        for key in ("dockerfile", "context"):
            if key in app and not isinstance(app[key], str):
                fail(f"{where}: '{key}' must be a string")
        if "enabled" in app and not isinstance(app["enabled"], bool):
            fail(f"{where}: 'enabled' must be true or false")
        for key in ("series_tags", "publish_latest"):
            if key in app and not isinstance(app[key], bool):
                fail(f"{where}: '{key}' must be true or false")
        plats = app.get("platforms", ["linux/amd64"])
        if (not isinstance(plats, list) or not plats
                or not all(isinstance(p, str) and "/" in p for p in plats)):
            fail(f"{where}: 'platforms' must be a non-empty list like [linux/amd64]")
        args = app.get("build_args", {})
        if not isinstance(args, dict) or not all(
                isinstance(k, str) and isinstance(v, (str, int, float, bool)) for k, v in args.items()):
            fail(f"{where}: 'build_args' must be a mapping of name to scalar")
        for value in args.values():
            if "," in str(value) or "=" in str(value):
                fail(f"{where}: build arg {value!r} may not contain '=' or ','")
        labels = app.get("labels", {})
        if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, (str, int, float, bool))
                                                   for k, v in labels.items()):
            fail(f"{where}: 'labels' must be a mapping")
        smoke = app.get("smoke_test", [])
        if not isinstance(smoke, list) or not all(isinstance(s, str) and s.strip() for s in smoke):
            fail(f"{where}: 'smoke_test' must be a list of shell commands")
    return apps


def find_app(apps: list[dict], name: str) -> dict:
    for app in apps:
        if app["name"] == name:
            return app
    fail(f"no app named {name!r} in containers.yml (enabled: {', '.join(a['name'] for a in apps)})")
    raise AssertionError


def api_get(path: str, token: str | None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "docker-image-publisher",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(API + path, headers=headers), timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        fail(f"GET {path} -> HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
    except urllib.error.URLError as exc:
        fail(f"GET {path} -> {exc.reason}")
    raise AssertionError


def api_get_or_none(path: str, token: str | None) -> dict | None:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "docker-image-publisher",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(API + path, headers=headers), timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        fail(f"GET {path} -> HTTP {exc.code}")
    except urllib.error.URLError as exc:
        fail(f"GET {path} -> {exc.reason}")
    raise AssertionError


def image_name(app: dict, owner: str) -> str:
    return f"{REGISTRY}/{owner.lower()}/{app['name']}".lower()


def resolve_ref(app: dict, token: str | None) -> dict:
    """Pin the app to one exact upstream commit, and name the version we will tag with."""
    upstream = app["upstream"]
    if app.get("track", "release") == "release":
        rel = api_get(f"/repos/{upstream}/releases/latest", token)
        if not rel.get("tag_name"):
            fail(f"{upstream} has no published release to track")
        ref = rel["tag_name"]
        commit = api_get(f"/repos/{upstream}/commits/{urllib.parse.quote(ref, safe='')}", token)
        info = {
            "ref": ref,
            "sha": commit["sha"],
            "version": ref,
            "released": rel.get("published_at", ""),
            "release_url": rel.get("html_url", ""),
        }
    else:
        branch = app["branch"]
        commit = api_get(f"/repos/{upstream}/commits/{urllib.parse.quote(branch, safe='')}", token)
        info = {
            "ref": branch,
            "sha": commit["sha"],
            "version": f"{branch.replace('/', '-')}-{commit['sha'][:7]}",
            "released": commit["commit"]["committer"]["date"],
            "release_url": commit.get("html_url", ""),
        }

    tags = [info["version"]]
    series = SEMVER_RE.match(info["version"]) if app.get("series_tags") else None
    if series:
        prefix, major, minor = series.group(1), series.group(2), series.group(3)
        tags += [f"{prefix}{major}.{minor}", f"{prefix}{major}"]
    if app.get("publish_latest", True):
        tags.append("latest")
    info["tags"] = list(dict.fromkeys(tags))
    return info


def check_dockerfile(app: dict, ref: str, token: str | None) -> None:
    path = app.get("dockerfile", "Dockerfile").strip("/")
    here = api_get_or_none(f"/repos/{app['upstream']}/contents/{path}?ref={ref}", token)
    if here is None:
        fail(f"{app['name']}: {app['upstream']} has no {path} at {ref}; "
             f"the project may have moved it - update containers.yml")


def joined(mapping: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in mapping.items())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="containers.yml")
    ap.add_argument("--validate", action="store_true", help="schema-check plus check upstream Dockerfiles exist")
    ap.add_argument("--list", action="store_true", help="print enabled app names")
    ap.add_argument("--resolve", metavar="NAME", help="resolve one app and emit key=value lines")
    ap.add_argument("--smoke", metavar="NAME", help="write one app's smoke script")
    ap.add_argument("--out", default="-", help="destination for --smoke")
    args = ap.parse_args()

    apps = load_apps(args.config)
    enabled = [a for a in apps if a.get("enabled", True)]
    token = os.environ.get("GITHUB_TOKEN")

    if args.list:
        for app in enabled:
            print(app["name"])
        return

    if args.smoke:
        app = find_app(apps, args.smoke)
        body = ["set -euo pipefail", *(app.get("smoke_test") or [])]
        text = "\n".join(body) + "\n"
        if args.out == "-":
            sys.stdout.write(text)
        else:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
        return

    if args.validate:
        for app in enabled:
            ref = app["branch"] if app.get("track", "release") == "branch" else "HEAD"
            check_dockerfile(app, ref, token)
            print(f"{app['name']}: ok ({app['upstream']}, track {app.get('track', 'release')})")
        if not enabled:
            print("plan: no enabled apps", file=sys.stderr)
            raise SystemExit(1)
        return

    if args.resolve:
        app = find_app(apps, args.resolve)
        owner = os.environ.get("IMAGE_OWNER")
        if not owner:
            fail("--resolve needs IMAGE_OWNER (the ghcr.io namespace owner)")
        info = resolve_ref(app, token)
        check_dockerfile(app, info["ref"], token)
        platforms = app.get("platforms", ["linux/amd64"])
        out = {
            "ref": info["ref"],
            "sha": info["sha"],
            "version": info["version"],
            "released": info["released"],
            "release_url": info["release_url"],
            "upstream": app["upstream"],
            "image": image_name(app, owner),
            "tags": json.dumps(info["tags"]),
            "dockerfile": app.get("dockerfile", "Dockerfile"),
            "context": app.get("context", "."),
            "platforms": ",".join(platforms),
            "multiarch": str(len(platforms) > 1).lower(),
            "build_args": joined(app.get("build_args", {})),
            "labels": joined({
                "org.opencontainers.image.created": info["released"],
                "org.opencontainers.image.revision": info["sha"],
                "org.opencontainers.image.version": info["version"],
                **app.get("labels", {}),
            }),
            "has_smoke": str(bool(app.get("smoke_test"))).lower(),
        }
        for key, value in out.items():
            print(f"{key}={value}")
        return

    ap.error("choose one of --validate, --list, --resolve, --smoke")


if __name__ == "__main__":
    main()
