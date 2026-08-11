# oraja-training beatoraja IR

This directory contains the `IRConnection` plugin for the official
`exch-bms2/beatoraja` API.  It is intentionally independent from the
Cloudflare Worker: the Worker receives the versioned `ir-event` envelope and
returns an idempotent ACK; this JAR owns only the client-side boundary.

## Compatibility and dependency

The compatibility target is the current `master` source of
[`exch-bms2/beatoraja`](https://github.com/exch-bms2/beatoraja), checked on
2026-08-11 at commit `721856fbb431`.  The upstream project is GPL-3.0 and its
README requires a 64-bit Java 17 runtime.  Upstream does not publish a Maven
or Gradle API artifact; its `build.xml` compiles from source and `lib/*.jar`.

The real build therefore takes the user-provided beatoraja runtime JAR as a
compile-only dependency:

```sh
./gradlew clean releaseArtifacts \
  -PbeatorajaJar=/path/to/beatoraja.jar \
  -PbeatorajaApiVersion=master@721856fbb431
```

`beatorajaJar` is never copied into this plugin JAR.  With no property, the
build uses small source-only API stubs in `src/compat/java` so that the
contract tests can run in a clean checkout.  Those stubs are not packaged and
are not a substitute for the official compatibility build; CI/release must
run once with the actual JAR.  The Java toolchain and Gradle wrapper are
fixed to Java 17 / Gradle 8.10.2.

The generated `build/release/` directory contains the plugin JAR, source JAR,
SHA-256 checksums, a CycloneDX-style SBOM, and a license notice.  No release
artifact is committed or published by these tasks.

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

The client API used by this module is:

* `POST /v1/ir/devices/session` — validates the device token in the Bearer
  header; body contains only `profile_id` and `device_id`.
* `POST /v1/ir/events` — accepts an event and returns
  `{"event_id":"...","status":"accepted|duplicate"}`.
* `GET /v1/ir/scores?profile_id=...&sha256=...` — returns the authenticated
  profile's aggregate scores only.  The response is an allowlisted score
  array; no rival, table, or course-ranking data is requested.

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
./gradlew contractTest
./gradlew check
./gradlew clean releaseArtifacts -PbeatorajaJar=/path/to/beatoraja.jar
```

These commands build/test locally only.  They do not deploy the Worker, start
or restart a service, create a GitHub release, or upload a JAR.
