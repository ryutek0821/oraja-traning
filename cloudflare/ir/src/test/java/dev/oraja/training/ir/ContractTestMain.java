/* SPDX-License-Identifier: AGPL-3.0-only */
package dev.oraja.training.ir;

import bms.player.beatoraja.ir.*;
import java.lang.reflect.*;
import java.nio.file.*;
import java.time.Instant;
import java.util.List;

/** Dependency-free smoke tests, executed with assertions enabled. */
public final class ContractTestMain {
    public static void main(String[] args) throws Exception {
        mapperAndSpoolContract();
        compositeDoesNotLeakOfficialToken();
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
