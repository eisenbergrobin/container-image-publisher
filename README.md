# docker-image-publisher

Turns upstream repos that ship a `Dockerfile` but publish **no container image** into
ordinary registry images on ghcr.io.

A weekly GitHub Actions job builds the upstream Dockerfile and publishes
`ghcr.io/<you>/<app>:latest`. Anything that updates by comparing a registry tag - a NAS
container UI, Portainer, compose plus a watcher, plain `docker pull` - cannot tell the
result apart from an official image.

No forks. No vendored Dockerfiles. The workflow checks out the upstream project at an exact
commit and builds the Dockerfile that project already maintains.

## How a run works

```
weekly (Mon 05:17 UTC) or manual
  plan    validate containers.yml, verify each upstream Dockerfile still exists, build a matrix
  build   resolve the upstream ref to one exact commit
          skip if that version tag is already in ghcr.io
          probe ghcr.io with a 1 KB throwaway tag: a registry permission block
          should cost five seconds, not 25 minutes of compile
          check out upstream AT THAT COMMIT
          build and push only the immutable version tag (v0.3.3)
          run the app's smoke test inside the image, no GPU needed
          only then move latest / v0.3 / v0 onto it
  report  one table; the run goes red if any app failed
```

Two ideas carry the design:

- **Immutable version tag first, rolling tags second.** `latest` moves only after the smoke
  test passes. Upstream breaking their own build shows up as a red workflow, not as a
  dead container.
- **Idempotence by tag existence.** "Have we built this already?" is "does the version tag
  exist?", so a weekly run against an unchanged upstream costs a few seconds and no
  build minutes.

## Start here: two things about basalt

**1. The image only runs on a Blackwell GPU.** basalt's `CMakeLists.txt` hard-fails unless
`CMAKE_CUDA_ARCHITECTURES` is `120`, and its README requires an RTX Blackwell-class card
(compute capability 12.0) on a 580-branch driver or newer. On anything else the build
succeeds and the container will not run. Check `nvidia-smi` on the host that will run it
before you spend an hour building.

**2. Upstream publishes release archives, not images.** Each basalt release carries
`basalt-vX.Y.Z-linux-x64.tar.gz` (binaries + server + tools + profiles, no CUDA libs, no
venv). This repo tracks **releases**, not `main`, so you get upstream's tested versions
with real version tags and changelogs, and rolling back means pulling an older tag.
If the from-source build ever feels too slow, the archive is the fast path: the upstream
Dockerfile's final stage assembles exactly that tree plus a CUDA runtime base and a venv,
so a Dockerfile that downloads the tarball instead of compiling would build in minutes.
That is a deliberate trade of build speed for one Dockerfile you now own; it is not worth
it until the source build actually hurts.

## Setup

1. Create this as a **public or private** GitHub repo and push it:

   ```sh
   git init -b main && git add -A && git commit -m "image publisher"
   git remote add origin https://github.com/YOU/docker-image-publisher.git
   git push -u origin main
   ```

2. Nothing to configure. Packages are pushed with the run's built-in `GITHUB_TOKEN`
   (`packages: write`) and land in your account's Container registry, linked to this repo.
   First push of `ghcr.io/YOU/basalt:v0.3.3` creates the package.

3. Run **Build images → Run workflow** once to build now instead of waiting for Monday.

Private vs public, and what it costs:

| | Actions minutes | Package visibility |
|---|---|---|
| Public repo | unlimited Linux runner minutes | package must be public to be free of storage metering |
| Private repo | 2 000 min/month on GitHub Free | private packages count against plan quota |

GitHub's billing page currently notes that Container registry storage and bandwidth are
free, and this image is large (a CUDA runtime base plus the engine: several GB). If that
note ever changes, a private multi-GB image is the first thing that would start costing
money. The basalt from-source build is a CUDA compile: expect roughly 30-90 minutes cold,
so a private repo's 2 000 free minutes/month is still comfortable for weekly builds of a
handful of apps, but not for dozens.

## Pulling the images

- **Private package**: authenticate the pulling side with a GitHub PAT scoped
  `read:packages`, username = your GitHub username (`docker login ghcr.io`). Without it the
  pull fails *and* the version check fails quietly, which looks like "upstream never
  updates". A public package needs no credentials.
- **Update**: pull the tag, recreate the container. Same config, same volumes.
- **Roll back**: pull an older version tag (`v0.3.2`) or a series tag.
- **Which build is running?**: the image labels carry `org.opencontainers.image.revision`
  (the exact upstream commit) and `.version`.

`latest` means "newest upstream release that built and passed the smoke test", not "newest
upstream release". If you would rather not take minor-version jumps without asking, pin the
series tag (`v0.3`) and you receive patch releases only. For an app like basalt, whose config
format has moved before (upstream ships `tools/migrations/config_v1_to_v2.py` for exactly
that), a series tag is the calmer choice.

## Adding an app

Add an entry to `containers.yml`, open a PR, and **validate-config** previews the next
weekly run's versions in its job summary. That check is the whole point of keeping the list
in one file: it catches an upstream that moved or renamed its Dockerfile before the silent
gap opens.

| Key | Default | Meaning |
|---|---|---|
| `name` | - | package name, so `ghcr.io/YOU/<name>` |
| `upstream` | - | `owner/repo` of the project. Never a fork |
| `track` | `release` | `release` = newest published release tag; `branch` = tip of a branch |
| `branch` | required if `track: branch` | branch to follow |
| `dockerfile` | `Dockerfile` | path inside the upstream repo |
| `context` | `.` | build context inside the upstream repo |
| `platforms` | `[linux/amd64]` | keep amd64-only unless you have other hardware |
| `build_args` | `{}` | `ARG`s of the upstream Dockerfile; no `=` or `,` in values |
| `series_tags` | unset | from `v0.3.3` also move `v0.3` and `v0` |
| `publish_latest` | `true` | also move `latest` |
| `labels` | `{}` | OCI labels to stamp on the image |
| `smoke_test` | `[]` | shell commands run inside the built image; all must exit 0 |

A few notes on the parts that are easy to get wrong:

- **Smoke tests must not need a GPU, a network, or a model file.** They run on a plain
  runner. Good checks: expected binaries exist, `ldd` finds no `not found` libraries, the
  venv interpreter starts, config directories are present. Those catch the failure that
  actually happens, which is an upstream base-image change dropping a library.
- **`smoke_test: []` disables the gate** and the run summary records that the app skipped it.
- **More than one platform** disables `--load`, so the smoke test runs against the pushed
  image instead of a local copy; the tag promotion still happens only after it passes.
- Actions and the two docker actions are pinned to commit SHAs, with Dependabot bumping the
  pins monthly. This repo holds a package-push token, so it should not be pulling build
  steps by floating tag.

### What adding one actually takes

The YAML itself is two minutes. The real work is three questions, usually ten minutes more:

1. **Does it already publish an image?** Check Docker Hub and the project's own ghcr.io
   packages first. If an image exists, no entry is needed.
2. **Does it have a `Dockerfile`, and where?** Anything not at the root needs `dockerfile:`
   and usually a matching `context:`. `validate-config` fails loudly if the file is missing
   or upstream moved it.
3. **Does it publish releases?** Yes -> `track: release`. No -> `track: branch`, and you
   accept `<branch>-<sha>` tags with no changelog.

Then two optional minutes that pay for themselves: `build_args` if the Dockerfile declares
`ARG`s, and two or four `smoke_test` lines. A release-tagged app with a root Dockerfile and no
args is a five-minute copy-paste. An unusual context layout, required args, or a build that
needs a specific base pushes it to half an hour.

The one case this repo cannot handle yet: an upstream with **no Dockerfile at all**. Every
entry builds a Dockerfile that upstream maintains, so that project needs one written first -
ideally upstream, otherwise in a fork or in this repo. Building a Dockerfile from here is a
small extension to `plan.py` and one `if` in the workflow (checkout our repo instead of
theirs); everything else - versioning, skipping, smoke gating, promotion - stays identical.

## Many images from one repo

That is the intended shape: one repo, one schedule, N independent packages. Each entry becomes
its own matrix leg and its own ghcr.io package (`ghcr.io/YOU/<name>`), with its own build
cache tag (`<package>:buildcache`), its own version scheme, and its own promotion gate. The
cron is shared; nothing else is.

`fail-fast: false` is what makes that safe - a broken upstream stops that app's updates and
nothing else, and the report job lists each app's outcome separately.

The limits are shared quotas, not structure:

| Limit | What it means as the list grows |
|---|---|
| Actions minutes | One plan-wide allowance. Tag-existence skipping means unchanged apps cost seconds, so the bill follows how many upstreams actually changed, not how many entries exist |
| Concurrent jobs | Matrix legs queue past the plan's concurrency limit, so the run's wall time stretches as you add slow apps |
| Runner disk | Not shared: each leg gets a fresh runner, so big images compete for minutes rather than disk |
| GHCR packages | One package per app, each with its own visibility, token and cleanup policy |

Housekeeping worth knowing: `latest` is per package, so adding an app never disturbs another.
Container packages have no automatic retention - superseded versions pile up until you delete
them in the package UI or run a scheduled cleanup workflow. With multi-GB images that is worth
a monthly minute.

| Symptom | Cause |
|---|---|
| No new image, workflow green | upstream has not published a release (`track: release` is deliberate); or the version tag already exists, which is normal |
| Workflow red in the build job | upstream's Dockerfile broke. The app keeps running on the last good tag; check the log, then either fix the entry or file upstream |
| Build job dies with "no space left on device" | CUDA base images are big. The run already prunes images and unused toolchains; if it still fails, raise `timeout-minutes` and check you are not building many GB-scale apps on one runner |
| Nothing sees a new image | for private packages, the pulling side has no `read:packages` token, so the pull and the version check both fail |
| Container starts then exits, no logs | wrong GPU: basalt is compiled for sm_120 (Blackwell) only |
| `denied: permission_denied: write_package` | an account-level block, not a workflow one: unaccepted Docker terms of service, a package name already owned by another repo in the account, or a blocked billing category. The probe step fails in seconds so no compile is wasted, and its run summary quotes what GHCR will grant this credential - scopes granted with push present means the account is gated, not the token |

## Alternatives, and why this one

- **Docker Hub automated builds**: fewer YAML files, but they only build a repo you can push
  to, which drags forks and vendored Dockerfiles back in. Private automated builds are also
  capped on the free plan.
- **Building on the host that runs the container, pushing to a local `registry:2`**: no
  GitHub dependency, but you then own cron, git, and build breakage on that box, and a
  watcher against a local registry is the least-travelled path.
- **Asking upstream to publish**: worth doing alongside, permanently free, and then the entry
  gets deleted. basalt publishes release archives but no image, and its Dockerfile is
  maintained, so it is a good candidate either way.
