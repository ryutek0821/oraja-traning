# Public IR / private service boundary

The authoritative service repository remains private. The only Java client
tree approved for a public GPL-3.0-only source release is this `cloudflare/ir/`
directory. A release must be produced from a clean export, never by changing
the visibility of the service repository or by publishing its Git history.

## Public export allowlist

- `src/main/**`, `src/compat/**`, and `src/test/**`
- `build.gradle.kts`, `settings.gradle.kts`, and `gradle.properties`
- `gradle/wrapper/gradle-wrapper.properties` (configuration only; no binary
  wrapper JAR is committed)
- `README.md`, `LICENSE`, `NOTICE`, and this document

Everything else is denied by default. In particular, exclude Worker,
Container, migrations, service runbooks, environment configuration, fixtures
derived from real users, logs, databases, tokens, host paths, and repository
history. The corresponding-source archive is the Gradle `sourceBundle` ZIP,
not a root repository archive. The Maven source JAR alone is not a complete
standalone build because it intentionally omits the build scripts.

## Required evidence

Before any authorized publication, retain all of the following under the
release record:

1. clean source revision and the approving issue;
2. CI success against the pinned real beatoraja commit/JAR;
3. two-build checksum equality for the runtime and source JARs;
4. `release-manifest.json`, CycloneDX SBOM, SHA-256 files, and CI provenance
   attestation;
5. proof that the runtime JAR contains `META-INF/LICENSE` and
   `META-INF/NOTICE`, and does not contain compatibility DTO classes;
6. public-tree secret/PII/path scan with zero findings.

Publishing the private repository, a Worker source archive, or a release that
omits corresponding IR source is a hard stop. Publication and repository
visibility changes remain separate, explicitly approved external operations.
