/* SPDX-License-Identifier: GPL-3.0-only */
package bms.player.beatoraja.ir;

/**
 * Explicit host-side adapter for coexistence with one legacy IR.
 * The oraja device token is never supplied to the legacy connection.
 */
public final class CompositeIRConnection implements IRConnection {

    private final IRConnection official;
    private final IRConnection legacy;
    private final IRAccount legacyAccount;

    public CompositeIRConnection(IRConnection official, IRConnection legacy, IRAccount legacyAccount) {
        if (official == null || legacy == null) throw new IllegalArgumentException("both IR clients are required");
        this.official = official;
        this.legacy = legacy;
        this.legacyAccount = legacyAccount;
    }

    @Override public IRResponse<IRPlayerData> register(IRAccount account) { return official.register(account); }

    @Override
    public IRResponse<IRPlayerData> login(IRAccount account) {
        IRResponse<IRPlayerData> result = official.login(account);
        if (legacyAccount != null) legacy.login(legacyAccount);
        return result;
    }

    @Override public IRResponse<IRPlayerData[]> getRivals() { return legacy.getRivals(); }
    @Override public IRResponse<IRTableData[]> getTableDatas() { return legacy.getTableDatas(); }
    @Override public IRResponse<IRScoreData[]> getPlayData(IRPlayerData player, IRChartData chart) {
        return legacy.getPlayData(player, chart);
    }
    @Override public IRResponse<IRScoreData[]> getCoursePlayData(IRPlayerData player, IRCourseData course) {
        return legacy.getCoursePlayData(player, course);
    }

    @Override
    public IRResponse<Object> sendPlayData(IRChartData chart, IRScoreData score) {
        IRResponse<Object> officialResult = official.sendPlayData(chart, score);
        IRResponse<Object> legacyResult = legacy.sendPlayData(chart, score);
        return combine(officialResult, legacyResult);
    }

    @Override
    public IRResponse<Object> sendCoursePlayData(IRCourseData course, IRScoreData score) {
        IRResponse<Object> officialResult = official.sendCoursePlayData(course, score);
        IRResponse<Object> legacyResult = legacy.sendCoursePlayData(course, score);
        return combine(officialResult, legacyResult);
    }

    @Override public String getSongURL(IRChartData chart) { return legacy.getSongURL(chart); }
    @Override public String getCourseURL(IRCourseData course) { return legacy.getCourseURL(course); }
    @Override public String getPlayerURL(IRPlayerData player) { return legacy.getPlayerURL(player); }
    @Override public IRResponse<IRVersionInfo> getVersionInfo(String currentVersion) {
        return legacy.getVersionInfo(currentVersion);
    }
    @Override public IRResponse<String[]> getIllegalSongs() { return legacy.getIllegalSongs(); }

    private static IRResponse<Object> combine(IRResponse<Object> official, IRResponse<Object> legacy) {
        boolean succeeded = official.isSucceeded() && legacy.isSucceeded();
        String message = "oraja=" + official.getMessage() + "; legacy=" + legacy.getMessage();
        return new SimpleIRResponse<>(succeeded, message, null);
    }
}
