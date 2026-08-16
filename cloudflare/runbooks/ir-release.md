# GPL IR release runbook

This procedure prepares evidence for the public GPL-3.0-only IR client. It does
not authorize a GitHub Release, repository creation, visibility change, upload,
or deployment. Those external writes require the separately approved release
issue.

1. Confirm the private service tree is clean and the release issue names the
   exact source revision. Read `cloudflare/ir/PUBLICATION.md`; export only its
   allowlist and never publish the private repository or its history.
2. Require the `ir` CI job to compile against beatoraja commit
   `721856fbb431`, pass the contract tests, and compare two clean-build JAR
   checksums. A compatibility-stub-only build is not releasable.
3. Download the CI artifact and provenance attestation. Verify the attestation
   subject, source revision, workflow identity, runtime/source JAR SHA-256,
   `release-manifest.json`, SBOM, build-complete source ZIP, `LICENSE`, and
   `NOTICE`.
4. Inspect the runtime JAR. It must contain the license and notice and must not
   contain beatoraja runtime classes or compatibility DTOs. Run the public
   secret/PII/path scan against the clean export.
5. Prepare one draft release containing the runtime JAR, Maven source JAR,
   build-complete source ZIP, checksum
   files, SBOM, manifest, license, and notice together. Record the upstream API
   commit and state that beatoraja itself is not bundled.
6. Stop for explicit publication approval. After approval, upload all artifacts
   atomically where possible, verify the public checksums and corresponding
   source link, then record the immutable release URL. Never retry an uncertain
   publish blindly; inspect the remote release first.

If real-JAR CI, two-build equality, provenance, source archive, or GPL metadata
is missing, the release remains a candidate and must not be published.
