# Release AgentKit

After CI passes for the commit you want to release, push a version tag:

```sh
git tag v0.1.0
git push origin v0.1.0
```

The release workflow builds and publishes these images for `linux/amd64` and
`linux/arm64` under `ghcr.io/sozercan/agentkit`:

- `agentkit`, the BuildKit frontend.
- `serve-pydantic-ai`, the Pydantic AI runtime.
- `serve-maf`, the Microsoft Agent Framework runtime.
- `serve-langgraph`, the LangGraph runtime.

Each image gets the version tag, such as `v0.1.0`. After all images publish,
a stable release updates `latest` unless a newer stable release already exists.
Prerelease tags such as `v0.2.0-rc.1` publish versioned images without
changing `latest` and create a GitHub prerelease.
Build metadata such as `+build.1` is rejected before publishing to prevent
collisions between versioned Docker tags.

A GitHub Release with generated notes is created only after all four images
publish. Publishing uses the repository's `GITHUB_TOKEN`; no separate registry
secret is needed. Builds include SBOM and provenance attestations.

Release runs are serialized. Updates across the four `latest` tags are not
atomic; use version tags when you need a fixed release, and rerun a failed
workflow to finish an interrupted publication.

On first publication, GHCR packages default to private. After the first release,
open each of the four packages' settings and change its visibility to **Public**
before announcing it. For existing packages published manually, grant this
repository Actions access if needed. The workflow does not change package access.
