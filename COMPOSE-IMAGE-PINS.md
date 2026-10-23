# Review-only immutable image digest audit

The existing private Compose configuration may point to mutable `latest`
tags. Tags are NOT immutable. To inspect whether both configured images use
fully qualified registry digests without revealing private .env credentials:

```sh
python3 scripts/check_compose_image_pins.py
```

The command invokes **only** `docker compose ... config --images`, with the
explicit private project directory and private `.env` file, the crawler
Compose file and the optional SearXNG override. It never
executes `up`, `pull`, `inspect`, `down`, or writes configuration.
The private Compose project must contain a regular `.env` file; linked or
missing `.env` files are refused. As with any Docker Compose invocation,
**exported shell environment variables take precedence over `.env` values**.
Run the audit under the same exported environment as the intended deployment
(or unset unintended image overrides). The audit checks the configured
image references, not the images of currently running containers.

Results report the number of images and whether each is digest pinned,
without printing image strings or secret variables. The inspection fails
closed if Compose does not list the expected one crawler image and, when the
SearXNG overlay is present, its second image. Exit code 1 means an
unpinned image; 2 means inspection unavailable or invalid.

The checked-in SearXNG overlay supports an optional `SEARXNG_IMAGE`
variable. If absent, it retains the previous default tag, for backwards
compatibility. The operator can put a reviewed image digest reference
(e.g. `docker.io/searxng/searxng@sha256:FULL_64_HEX_DIGEST`) in the
private `.env` file. Likewise, replace `CRAWL4AI_IMAGE` with a verified
digest (not a Docker-local image ID). Do not reveal `.env` contents.

For an existing **local** image, `docker image inspect --format
'{{json .RepoDigests}}' IMAGE_NAME` may list registry digests. A missing
or unrelated repo digest is not sufficient: independently verify image
provenance, architecture, trust and compatibility before manually selecting
the digest. This check intentionally does not fetch or verify upstream
release metadata, signatures, image hashes or vulnerabilities.

After changing a digest, operator review and backup/rollback preparation
are required before any explicitly requested Compose recreation. Never
auto-pull or auto-upgrade images. Local-only checks do not change any
currently running container.
