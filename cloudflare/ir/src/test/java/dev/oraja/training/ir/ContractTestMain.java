/* SPDX-License-Identifier: GPL-3.0-only */
package dev.oraja.training.ir;

import bms.player.beatoraja.ir.*;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.lang.reflect.*;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.file.*;
import java.time.Instant;
import java.util.List;

/** Dependency-free smoke tests, executed with assertions enabled. */
public final class ContractTestMain {
    public static void main(String[] args) throws Exception {
        mapperAndSpoolContract();
        compositeDoesNotLeakOfficialToken();
        ownerReadContract();
        System.out.println("IR contract smoke tests passed");
    }

    private static void mapperAndSpoolContract() throws Exception {
        String profile = uuid(), device = uuid(), eventId = uuid();
        IRChartData chart = new IRChartData(null, "a".repeat(64), IRChartData.Mode.BEAT_7K, 0);
        IRScoreData score = new IRScoreData(1_700_000_000L, new IRScoreData.Clear(5),
                10, 2, 3, 1, 0, 0, 1, 0, 0, 0, 0, 0,
                12, 17, 17, 1, 2, 123L, 0, 3);
        Class<?> mapper = Class.forName("bms.player.beatoraja.ir.IrEventMapper");
        Object event = mapper.getMethod("mapPlay", String.class, String.class, String.class,
                IRChartData.class, IRScoreData.class, Instant.class)
                .invoke(null, eventId, profile, device, chart, score, Instant.EPOCH);
        String json = (String) event.getClass().getMethod("toJson").invoke(event);
        assert json.contains("\"contract\":\"ir-event\"");
        assert json.contains("\"event_id\":\"" + eventId + "\"");
        assert !json.contains("password") && !json.contains("title");

        Path root = Files.createTempDirectory("oraja-ir-contract-");
        Class<?> spoolType = Class.forName("bms.player.beatoraja.ir.DurableSpool");
        Constructor<?> constructor = spoolType.getDeclaredConstructor(Path.class);
        constructor.setAccessible(true);
        Object spool = constructor.newInstance(root);
        Method persist = spoolType.getDeclaredMethod("persist", event.getClass());
        persist.setAccessible(true);
        persist.invoke(spool, event);
        Method entriesFor = spoolType.getDeclaredMethod("entriesFor", String.class);
        entriesFor.setAccessible(true);
        List<?> entries = (List<?>) entriesFor.invoke(spool, profile);
        assert entries.size() == 1;
        assert ((List<?>) entriesFor.invoke(spool, uuid())).isEmpty();
        Method acknowledge = spoolType.getDeclaredMethod("acknowledge", entries.get(0).getClass());
        acknowledge.setAccessible(true);
        acknowledge.invoke(spool, entries.get(0));
        assert ((List<?>) entriesFor.invoke(spool, profile)).isEmpty();
    }

    private static void compositeDoesNotLeakOfficialToken() {
        CapturingConnection official = new CapturingConnection(), legacy = new CapturingConnection();
        CompositeIRConnection composite = new CompositeIRConnection(official, legacy,
                new IRAccount("legacy-user", "legacy-secret", "legacy"));
        composite.login(new IRAccount(uuidUnchecked(), "official-device-token", "official"));
        assert "official-device-token".equals(official.loginAccount.password);
        assert "legacy-secret".equals(legacy.loginAccount.password);
    }

    private static void ownerReadContract() throws Exception {
        String profile = uuid();
        String token = "ot_" + "x".repeat(43);
        int[] playReads = {0};
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> respond(exchange, profile, token, playReads));
        server.start();
        Path spool = Files.createTempDirectory("oraja-ir-read-");
        try {
            IrConfiguration configuration = new IrConfiguration(
                    URI.create("http://127.0.0.1:" + server.getAddress().getPort() + "/"),
                    spool, true, true, 64 * 1024);
            Constructor<OrajaTrainingIRConnection> constructor = OrajaTrainingIRConnection.class
                    .getDeclaredConstructor(IrConfiguration.class);
            constructor.setAccessible(true);
            OrajaTrainingIRConnection connection = constructor.newInstance(configuration);
            IRResponse<IRPlayerData> login = connection.login(new IRAccount(profile, token, "untrusted-name"));
            assert login.isSucceeded();
            assert profile.equals(login.getData().id);
            assert "Server owner".equals(login.getData().name);

            IRChartData chart = new IRChartData(null, "a".repeat(64), IRChartData.Mode.BEAT_7K, 0);
            IRScoreData[] scores = connection.getPlayData(login.getData(), chart).getData();
            assert scores.length == 1;
            assert scores[0].epg == 10 && scores[0].gauge == 3;
            int ownerReadCount = playReads[0];
            assert connection.getPlayData(new IRPlayerData(uuid(), "rival", ""), chart).getData().length == 0;
            assert playReads[0] == ownerReadCount : "cross-profile read reached the network";

            assert connection.getRivals().getData().length == 0;
            assert connection.getTableDatas().getData().length == 0;
            assert connection.getCoursePlayData(login.getData(), null).getData().length == 0;
            assert connection.getIllegalSongs().getData().length == 0;
            assert "beatoraja-test".equals(connection.getVersionInfo("beatoraja-test").getData().version);
        } finally {
            server.stop(0);
        }
    }

    private static void respond(HttpExchange exchange, String profile, String token, int[] playReads)
            throws IOException {
        assert ("Bearer " + token).equals(exchange.getRequestHeaders().getFirst("Authorization"));
        String path = exchange.getRequestURI().getPath();
        String body;
        if ("/v1/ir/player".equals(path)) {
            body = "{\"contract\":\"ir-read.v1\",\"player\":{\"id\":\"" + profile
                    + "\",\"name\":\"Server owner\",\"rank\":\"\"}}";
        } else if ("/v1/ir/play-data".equals(path)) {
            playReads[0]++;
            assert exchange.getRequestURI().getRawQuery().contains("player_id=" + profile);
            body = scoreResponse(profile);
        } else if ("/v1/ir/rivals".equals(path)) {
            body = "{\"contract\":\"ir-read.v1\",\"players\":[]}";
        } else if ("/v1/ir/tables".equals(path)) {
            body = "{\"contract\":\"ir-read.v1\",\"tables\":[]}";
        } else if ("/v1/ir/course-play-data".equals(path)) {
            body = "{\"contract\":\"ir-read.v1\",\"scores\":[]}";
        } else if ("/v1/ir/illegal-songs".equals(path)) {
            body = "{\"contract\":\"ir-read.v1\",\"sha256\":[]}";
        } else if ("/v1/ir/version".equals(path)) {
            body = "{\"contract\":\"ir-read.v1\",\"version\":{\"version\":\"beatoraja-test\",\"message\":\"\",\"download_url\":null}}";
        } else {
            body = "{\"error\":\"not_found\"}";
            exchange.sendResponseHeaders(404, body.length());
            exchange.getResponseBody().write(body.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            exchange.close();
            return;
        }
        byte[] bytes = body.getBytes(java.nio.charset.StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(200, bytes.length);
        exchange.getResponseBody().write(bytes);
        exchange.close();
    }

    private static String scoreResponse(String profile) {
        return "{\"contract\":\"ir-read.v1\",\"scores\":[{"
                + "\"sha256\":\"" + "a".repeat(64) + "\",\"lntype\":0,\"id\":\"" + profile
                + "\",\"player\":\"\",\"clear\":5,\"date\":1700000000,"
                + "\"epg\":10,\"lpg\":2,\"egr\":3,\"lgr\":1,\"egd\":0,\"lgd\":0,"
                + "\"ebd\":1,\"lbd\":0,\"epr\":0,\"lpr\":0,\"ems\":0,\"lms\":0,"
                + "\"avgjudge\":0,\"maxcombo\":12,\"notes\":17,\"passnotes\":17,"
                + "\"minbp\":1,\"option\":2,\"seed\":123,\"assist\":0,\"gauge\":3,\"skin\":\"\"}],"
                + "\"truncated\":false}";
    }

    private static String uuid() throws Exception {
        return (String) Class.forName("bms.player.beatoraja.ir.UuidV7").getMethod("next").invoke(null);
    }
    private static String uuidUnchecked() { try { return uuid(); } catch (Exception e) { throw new AssertionError(e); } }

    private static final class CapturingConnection implements IRConnection {
        IRAccount loginAccount;
        public IRResponse<IRPlayerData> register(IRAccount a) { return response(null); }
        public IRResponse<IRPlayerData> login(IRAccount a) { loginAccount = a; return response(null); }
        public IRResponse<IRPlayerData[]> getRivals() { return response(new IRPlayerData[0]); }
        public IRResponse<IRTableData[]> getTableDatas() { return response(new IRTableData[0]); }
        public IRResponse<IRScoreData[]> getPlayData(IRPlayerData p, IRChartData c) { return response(new IRScoreData[0]); }
        public IRResponse<IRScoreData[]> getCoursePlayData(IRPlayerData p, IRCourseData c) { return response(new IRScoreData[0]); }
        public IRResponse<Object> sendPlayData(IRChartData c, IRScoreData s) { return response(null); }
        public IRResponse<Object> sendCoursePlayData(IRCourseData c, IRScoreData s) { return response(null); }
        public String getSongURL(IRChartData c) { return "legacy-song"; }
        public String getCourseURL(IRCourseData c) { return "legacy-course"; }
        public String getPlayerURL(IRPlayerData p) { return "legacy-player"; }
        public IRResponse<IRVersionInfo> getVersionInfo(String v) { return response(null); }
        public IRResponse<String[]> getIllegalSongs() { return response(new String[0]); }
    }

    private static <T> IRResponse<T> response(T data) {
        return new IRResponse<>() {
            public boolean isSucceeded() { return true; }
            public String getMessage() { return "ok"; }
            public T getData() { return data; }
        };
    }
}
