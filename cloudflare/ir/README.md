# oraja-training beatoraja IR

This public client is licensed **GPL-3.0-only**. The private service and other
repository components remain AGPL-3.0-only; see [PUBLICATION.md](PUBLICATION.md)
for the deny-by-default release boundary.

> **Implementation status:** the source includes the owner-only
> `IRConnection`, durable profile-partitioned write spool, bounded HTTPS transport,
> compatibility stubs, composite adapter, and dependency-free contract smoke
> tests. Release compatibility must still be verified with the pinned real
> beatoraja JAR before publishing.

This directory is the foundation for an `IRConnection` plugin targeting the
official `exch-bms2/beatoraja` API.  The Worker receives the versioned play
event and returns an idempotent ACK; the eventual JAR will own only the
client-side boundary.

## Compatibility and dependency

The compatibility target is the current `master` source of
[`exch-bms2/beatoraja`](https://github.com/exch-bms2/beatoraja), checked on
2026-08-11 at commit `721856fbb431`.  The upstream project is GPL-3.0 and its
README requires a 64-bit Java 17 runtime.  Upstream does not publish a Maven
or Gradle API artifact; its `build.xml` compiles from source and `lib/*.jar`.

The release build takes the user-provided beatoraja runtime JAR as a
compile-only dependency. Use Gradle 8.10.2 directly. The binary wrapper JAR
is deliberately not committed; CI bootstraps exactly Gradle 8.10.2 and the
checked-in wrapper properties document the same version:

```sh
gradle clean releaseArtifacts \
  -PbeatorajaJar=/path/to/beatoraja.jar \
  -PbeatorajaApiVersion=master@721856fbb431
```

`beatorajaJar` is never copied into this plugin JAR.  With no property, the
build uses small source-only API stubs in `src/compat/java` so that the
contract tests can run in a clean checkout.  Those stubs are not packaged and
are not a substitute for the official compatibility build; CI/release must
run once with the actual JAR.  The Java toolchain and Gradle wrapper are
fixed to Java 17 / Gradle 8.10.2.

The generated `build/release/` directory contains the plugin JAR, Maven source
JAR, a reproducible build-complete source ZIP,
SHA-256 checksums, a CycloneDX SBOM, the GPL license/notice, and an unsigned
`release-manifest.json`. CI additionally creates the platform provenance
attestation. No release artifact is committed or published by these tasks.

## Configuration

The stock beatoraja IR screen supplies `IRAccount.id`, `.password`, and
`.name`.  This plugin gives those fields the following deliberately narrow
meaning:

| beatoraja field | meaning | stored/sent where |
|---|---|---|
| ID | server-issued profile UUIDv7 | event `profile_id` and HTTPS query/body |
| Password | opaque device token (`plays:write`) | in-memory Bearer header only |
| Name | local display label | local UI compatibility only; never sent as a password |

The plugin never implements web-password registration, never puts the token
in an event, spool file, URL, log, or exception, and never forwards these
fields to an existing IR adapter.

Sending is fail-closed until the explicit `IR_SEND_ALWAYS=true` setting is
present as either a JVM property or environment variable.  In beatoraja's
own IR settings, choose **Always** (`IRConfig.IR_SEND_ALWAYS == 0`) as well;
the client cannot observe that integer after beatoraja calls the interface.

Optional JVM/environment settings:

| property | environment | default |
|---|---|---|
| `oraja.ir.baseUrl` | `ORAJA_IR_BASE_URL` | `https://api.oraja-training.dev/` |
| `oraja.ir.spoolDir` | `ORAJA_IR_SPOOL_DIR` | `${user.home}/.beatoraja/oraja-training-ir/spool` |
| `IR_SEND_ALWAYS` | `IR_SEND_ALWAYS` | required `true` |

Only HTTPS endpoints are accepted.  An HTTP loopback endpoint is available
to contract tests only with the explicit `-Doraja.ir.allowInsecureLoopback=true`
flag; it must not be used in a release configuration.

## Durable delivery contract

Every normal or course play is mapped to an allowlisted `ir-event` v1 object
before any network request.  The mapper reads only the documented public
fields of `IRChartData` and `IRScoreData`; `IRChartData.values`, key input,
ghost/replay data, BMS content, local paths, titles, and arbitrary fields are
unreachable from the event builder.  The upstream `IRScoreData` interface has
no `random` field, so v1 sends the required neutral value `0`; `option` and
`seed` are retained separately.

The event is written to a UUIDv7-named file using a temporary file, forced
file contents, atomic rename, and a best-effort directory fsync.  A login
drains only the active profile's files in UUIDv7 order.  An event is removed
only after an ACK whose `event_id` matches and whose status is `accepted` or
`duplicate`.  `429`, timeout, connection failure, and `5xx` retain the file
and use bounded jittered exponential backoff.  Corrupt or permanently
rejected files are moved, not deleted, to an inspectable `quarantine/` or
`rejected/` directory.  Disk-full and concurrent-process errors are surfaced
without sending an unpersisted event.

The token is not persisted.  A token/profile change clears the in-memory
session and only drains files whose `profile_id` matches the newly logged-in
profile, preventing an old profile's events from crossing the boundary.

The Worker API contract for the eventual client is:

* Device tokens are provisioned by the authenticated Web endpoint
  `POST /v1/profiles/{profile_id}/devices`; they are not created by the JAR.
* `POST /v1/plays` — validates the Bearer device token, accepts an event, and returns
  `{"event_id":"...","status":"accepted|duplicate"}`.
* `GET /v1/ir/player` — returns only the device token's profile identity.
* `GET /v1/ir/play-data` — returns at most 1,000 best-score projections from
  the token-owned Profile Durable Object, optionally filtered by SHA-256/LN mode.
* `GET /v1/ir/rivals`, `/v1/ir/tables`, and `/v1/ir/course-play-data` — return
  typed empty arrays in v1; they do not imply rival federation or server tables.
* `GET /v1/ir/version` and `/v1/ir/illegal-songs` — return bounded typed metadata.

All read routes require the same profile/device-bound Bearer credential. A
different `player_id` is rejected before a Durable Object request, and the
client also short-circuits cross-player calls without network access. Score
reads never mutate or acknowledge the durable submission spool.

## Offline and existing IR coexistence

Offline play is fail-open for gameplay and durable for delivery: once the
event file is forced to disk, the beatoraja call reports `queued-offline` if
the network is unavailable.  Restarting beatoraja creates a new client,
reuses the same spool/device identity, and retries the pending files after a
successful or offline-tolerated login.

`CompositeIRConnection` is the explicit embedding adapter for installations
that already have another IR connection.  It sends the same score to the
official client and the supplied legacy connection, but it does not call the
legacy `login` with the official device token.  Legacy rivals/tables/URLs stay
available through the legacy side; the official side remains profile-only and
returns empty rivals/tables/course rankings.  Stock beatoraja selects one
connection name, so using the composite requires a host-side integration or
a launcher that constructs it; simply copying a second JAR does not silently
duplicate traffic.  Contract tests cover this boundary and the limitation is
intentional.

## Local verification

```sh
gradle contractTest
gradle check
gradle clean releaseArtifacts -PbeatorajaJar=/path/to/beatoraja.jar
```

The first two commands use compatibility stubs and are development checks.
A publishable result requires the third command with the pinned real
beatoraja JAR; CI first runs behavior tests with the source-only fixtures,
then builds the real JAR from upstream commit `721856fbb431` using a Java 17
distribution with JavaFX, runs `realBeatorajaCompatibility` to compile the
main client ABI against it, and compares two clean-build checksums. Fixture
constructors are intentionally not mistaken for a real-JAR runtime test.

These commands build/test locally only.  They do not deploy the Worker, start
or restart a service, create a GitHub release, or upload a JAR.
