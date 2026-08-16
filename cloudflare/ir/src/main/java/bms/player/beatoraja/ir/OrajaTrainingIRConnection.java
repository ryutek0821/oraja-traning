/* SPDX-License-Identifier: GPL-3.0-only */
package bms.player.beatoraja.ir;

import java.io.IOException;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;

/** Official owner-only beatoraja IR boundary for oraja-training. */
public final class OrajaTrainingIRConnection implements IRConnection {

    public static final String NAME = "oraja-training";
    public static final String HOME = "https://oraja-training.dev/";

    private final IrConfiguration configuration;
    private final DurableSpool spool;
    private final DeviceIdentity device;
    private final IrHttpTransport transport;
    private volatile Session session;

    public OrajaTrainingIRConnection() throws IOException {
        this(IrConfiguration.fromSystem());
    }

    OrajaTrainingIRConnection(IrConfiguration configuration) throws IOException {
        this.configuration = configuration;
        spool = new DurableSpool(configuration.spoolDirectory);
        device = new DeviceIdentity(configuration.spoolDirectory);
        transport = new IrHttpTransport(configuration);
    }

    @Override
    public IRResponse<IRPlayerData> register(IRAccount account) {
        return failure("Registration is available only in the authenticated Web application", null);
    }

    @Override
    public synchronized IRResponse<IRPlayerData> login(IRAccount account) {
        session = null;
        if (!configuration.sendAlways) {
            return failure("IR_SEND_ALWAYS=true is required", null);
        }
        if (account == null || !IrConfiguration.validProfileId(account.id)
                || !IrConfiguration.validToken(account.password)) {
            return failure("Invalid profile ID or device token", null);
        }
        Session candidate = new Session(account.id, account.password,
                account.name == null ? "" : account.name);
        boolean readOffline = false;
        try {
            IrHttpTransport.Response profileResponse = transport.get(candidate.token, "v1/ir/player", Map.of());
            if (profileResponse.status() == 200) {
                IRPlayerData profile = IrReadMapper.player(profileResponse.body(), candidate.profileId);
                candidate = new Session(profile.id, candidate.token, profile.name);
            } else if (profileResponse.status() == 401 || profileResponse.status() == 403) {
                return failure("Invalid or revoked device token", null);
            } else if (profileResponse.status() >= 400 && profileResponse.status() < 500
                    && profileResponse.status() != 429) {
                return failure("IR profile login rejected", null);
            } else {
                readOffline = true;
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return failure("IR login interrupted", null);
        } catch (IOException e) {
            readOffline = true;
        } catch (IllegalArgumentException e) {
            return failure("Invalid IR profile response", null);
        }
        session = candidate;
        try {
            drain(candidate);
        } catch (IOException e) {
            // Offline-tolerated login: the token remains in memory and queued
            // events will be retried on send or the next login.
            return success("Logged in; pending events remain queued offline", player(candidate));
        }
        return success(readOffline ? "Logged in offline; read data is temporarily unavailable" : "Logged in", player(candidate));
    }

    @Override public IRResponse<IRPlayerData[]> getRivals() {
        Session current = session;
        if (current == null) return failure("Not logged in", new IRPlayerData[0]);
        try {
            IrHttpTransport.Response response = transport.get(current.token, "v1/ir/rivals", Map.of());
            if (response.status() != 200) return failure("Rival data unavailable", new IRPlayerData[0]);
            IrReadMapper.requireEmpty(response.body(), "players");
            return success("Rival federation is not available in v1", new IRPlayerData[0]);
        } catch (IOException | IllegalArgumentException e) {
            return failure("Rival data unavailable", new IRPlayerData[0]);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return failure("Rival read interrupted", new IRPlayerData[0]);
        }
    }

    @Override public IRResponse<IRTableData[]> getTableDatas() {
        Session current = session;
        if (current == null) return failure("Not logged in", new IRTableData[0]);
        try {
            IrHttpTransport.Response response = transport.get(current.token, "v1/ir/tables", Map.of());
            if (response.status() != 200) return failure("IR table data unavailable", new IRTableData[0]);
            IrReadMapper.requireEmpty(response.body(), "tables");
            return success("IR tables are not available in v1", new IRTableData[0]);
        } catch (IOException | IllegalArgumentException e) {
            return failure("IR table data unavailable", new IRTableData[0]);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return failure("IR table read interrupted", new IRTableData[0]);
        }
    }

    @Override public IRResponse<IRScoreData[]> getPlayData(IRPlayerData player, IRChartData chart) {
        Session current = session;
        if (current == null) return failure("Not logged in", new IRScoreData[0]);
        if (player != null && !current.profileId.equals(player.id)) {
            return success("Rival federation is not available in v1", new IRScoreData[0]);
        }
        Map<String, String> query = new LinkedHashMap<>();
        query.put("player_id", current.profileId);
        if (chart != null) {
            if (chart.sha256 == null || !chart.sha256.matches("[0-9a-fA-F]{64}")) {
                return failure("Invalid chart hash", new IRScoreData[0]);
            }
            query.put("sha256", chart.sha256.toLowerCase(java.util.Locale.ROOT));
            query.put("ln_mode", Integer.toString(chart.lntype));
        }
        try {
            IrHttpTransport.Response response = transport.get(current.token, "v1/ir/play-data", query);
            if (response.status() != 200) return failure("Score data unavailable", new IRScoreData[0]);
            return success("Owner score data", IrReadMapper.scores(response.body(), current.profileId));
        } catch (IOException | IllegalArgumentException e) {
            return failure("Score data unavailable", new IRScoreData[0]);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return failure("Score read interrupted", new IRScoreData[0]);
        }
    }

    @Override public IRResponse<IRScoreData[]> getCoursePlayData(IRPlayerData player, IRCourseData course) {
        Session current = session;
        if (current == null) return failure("Not logged in", new IRScoreData[0]);
        if (player != null && !current.profileId.equals(player.id)) {
            return success("Rival federation is not available in v1", new IRScoreData[0]);
        }
        try {
            IrHttpTransport.Response response = transport.get(current.token, "v1/ir/course-play-data",
                    Map.of("player_id", current.profileId));
            if (response.status() != 200) return failure("Course ranking unavailable", new IRScoreData[0]);
            IrReadMapper.requireEmpty(response.body(), "scores");
            return success("Course ranking is not available in v1", new IRScoreData[0]);
        } catch (IOException | IllegalArgumentException e) {
            return failure("Course ranking unavailable", new IRScoreData[0]);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return failure("Course read interrupted", new IRScoreData[0]);
        }
    }

    @Override
    public synchronized IRResponse<Object> sendPlayData(IRChartData chart, IRScoreData score) {
        Session current = session;
        if (current == null) return failure("Not logged in", null);
        String eventId = UuidV7.next();
        try {
            IrEvent event = IrEventMapper.mapPlay(eventId, current.profileId, device.id(), chart, score, Instant.now());
            return enqueueAndDrain(current, event);
        } catch (IllegalArgumentException | IOException e) {
            return failure(safeFailure(e), null);
        }
    }

    @Override
    public synchronized IRResponse<Object> sendCoursePlayData(IRCourseData course, IRScoreData score) {
        Session current = session;
        if (current == null) return failure("Not logged in", null);
        String eventId = UuidV7.next();
        try {
            IrEvent event = IrEventMapper.mapCourse(eventId, current.profileId, device.id(), course, score, Instant.now());
            return enqueueAndDrain(current, event);
        } catch (IllegalArgumentException | IOException e) {
            return failure(safeFailure(e), null);
        }
    }

    @Override public String getSongURL(IRChartData chart) { return null; }
    @Override public String getCourseURL(IRCourseData course) { return null; }
    @Override public String getPlayerURL(IRPlayerData player) {
        Session current = session;
        return current != null && (player == null || current.profileId.equals(player.id)) ? HOME : null;
    }

    @Override public IRResponse<IRVersionInfo> getVersionInfo(String currentVersion) {
        Session current = session;
        if (current == null) return failure("Not logged in", null);
        Map<String, String> query = currentVersion == null || currentVersion.isBlank()
                ? Map.of() : Map.of("current_version", currentVersion);
        try {
            IrHttpTransport.Response response = transport.get(current.token, "v1/ir/version", query);
            return response.status() == 200
                    ? success("Version data", IrReadMapper.version(response.body()))
                    : failure("Version data unavailable", null);
        } catch (IOException | IllegalArgumentException e) {
            return failure("Version data unavailable", null);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return failure("Version read interrupted", null);
        }
    }

    @Override public IRResponse<String[]> getIllegalSongs() {
        Session current = session;
        if (current == null) return failure("Not logged in", new String[0]);
        try {
            IrHttpTransport.Response response = transport.get(current.token, "v1/ir/illegal-songs", Map.of());
            if (response.status() != 200) return failure("Illegal-song data unavailable", new String[0]);
            IrReadMapper.requireEmpty(response.body(), "sha256");
            return success("No server-blocked songs", new String[0]);
        } catch (IOException | IllegalArgumentException e) {
            return failure("Illegal-song data unavailable", new String[0]);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return failure("Illegal-song read interrupted", new String[0]);
        }
    }

    private IRResponse<Object> enqueueAndDrain(Session current, IrEvent event) throws IOException {
        // Persistence precedes every send. Disk-full/locking failures are
        // surfaced and no unpersisted event is transmitted.
        spool.persist(event);
        try {
            drain(current);
        } catch (IOException e) {
            return success("queued-offline:" + event.eventId, null);
        }
        return spool.contains(event.eventId)
                ? success("queued-offline:" + event.eventId, null)
                : success("accepted:" + event.eventId, null);
    }

    private void drain(Session current) throws IOException {
        for (DurableSpool.Entry entry : spool.entriesFor(current.profileId)) {
            Delivery delivery = deliverWithRetry(current.token, entry);
            if (delivery == Delivery.ACCEPTED) {
                spool.acknowledge(entry);
            } else if (delivery == Delivery.REJECTED) {
                spool.reject(entry);
            } else {
                return; // UUIDv7 order: do not overtake a retryable event.
            }
        }
    }

    private Delivery deliverWithRetry(String token, DurableSpool.Entry entry) throws IOException {
        IOException lastFailure = null;
        for (int attempt = 0; attempt < 3; attempt++) {
            try {
                IrHttpTransport.Response response = transport.post(token, entry.json());
                if (response.status() == 200 || response.status() == 201 || response.status() == 202) {
                    try {
                        return validateAck(entry.eventId(), response.body())
                                ? Delivery.ACCEPTED : Delivery.RETRY;
                    } catch (IllegalArgumentException e) {
                        return Delivery.RETRY;
                    }
                }
                if (response.status() == 429 || response.status() >= 500) {
                    boundedBackoff(attempt);
                    continue;
                }
                return Delivery.REJECTED;
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IOException("IR delivery interrupted", e);
            } catch (IOException e) {
                lastFailure = e;
                boundedBackoff(attempt);
            } catch (IllegalArgumentException e) {
                return Delivery.RETRY;
            }
        }
        if (lastFailure != null) throw new IOException("IR endpoint unavailable", lastFailure);
        return Delivery.RETRY;
    }

    private static boolean validateAck(String eventId, String json) {
        Map<String, Object> ack = JsonCodec.object(json);
        Object status = ack.get("status");
        return eventId.equals(ack.get("event_id"))
                && ("accepted".equals(status) || "duplicate".equals(status));
    }

    private static void boundedBackoff(int attempt) throws IOException {
        if (attempt >= 2) return;
        long ceiling = Math.min(1_000L, 100L << attempt);
        try {
            Thread.sleep(ThreadLocalRandom.current().nextLong(ceiling + 1));
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IOException("IR retry interrupted", e);
        }
    }

    private static String safeFailure(Exception failure) {
        return failure instanceof IllegalArgumentException
                ? "Rejected local IR event: " + failure.getMessage()
                : "Could not persist IR event";
    }

    private static IRPlayerData player(Session current) {
        return new IRPlayerData(current.profileId, current.displayName, "");
    }

    private static <T> IRResponse<T> success(String message, T data) {
        return new SimpleIRResponse<>(true, message, data);
    }

    private static <T> IRResponse<T> failure(String message, T data) {
        return new SimpleIRResponse<>(false, message, data);
    }

    private record Session(String profileId, String token, String displayName) {}
    private enum Delivery { ACCEPTED, RETRY, REJECTED }
}
