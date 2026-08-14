/* SPDX-License-Identifier: AGPL-3.0-only */
package bms.player.beatoraja.ir;

import java.io.IOException;
import java.time.Instant;
import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;

/** Official, write-only beatoraja IR boundary for oraja-training. */
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
        session = new Session(account.id, account.password,
                account.name == null ? "" : account.name);
        try {
            drain(session);
        } catch (IOException e) {
            // Offline-tolerated login: the token remains in memory and queued
            // events will be retried on send or the next login.
            return success("Logged in; pending events remain queued offline", player(session));
        }
        return success("Logged in", player(session));
    }

    @Override public IRResponse<IRPlayerData[]> getRivals() { return success("Unsupported", new IRPlayerData[0]); }
    @Override public IRResponse<IRTableData[]> getTableDatas() { return success("Unsupported", new IRTableData[0]); }
    @Override public IRResponse<IRScoreData[]> getPlayData(IRPlayerData player, IRChartData chart) {
        return success("Read access is disabled", new IRScoreData[0]);
    }
    @Override public IRResponse<IRScoreData[]> getCoursePlayData(IRPlayerData player, IRCourseData course) {
        return success("Read access is disabled", new IRScoreData[0]);
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
    @Override public String getPlayerURL(IRPlayerData player) { return HOME; }

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
