/* SPDX-License-Identifier: GPL-3.0-only */
package bms.player.beatoraja.ir;

public interface IRConnection {
    IRResponse<IRPlayerData> register(IRAccount account);
    IRResponse<IRPlayerData> login(IRAccount account);
    IRResponse<IRPlayerData[]> getRivals();
    IRResponse<IRTableData[]> getTableDatas();
    IRResponse<IRScoreData[]> getPlayData(IRPlayerData player, IRChartData chart);
    IRResponse<IRScoreData[]> getCoursePlayData(IRPlayerData player, IRCourseData course);
    IRResponse<Object> sendPlayData(IRChartData chart, IRScoreData score);
    IRResponse<Object> sendCoursePlayData(IRCourseData course, IRScoreData score);
    String getSongURL(IRChartData chart);
    String getCourseURL(IRCourseData course);
    String getPlayerURL(IRPlayerData player);
    default IRResponse<IRVersionInfo> getVersionInfo(String currentVersion) {
        return new SimpleIRResponse<>(false, "Not supported", null);
    }
    default IRResponse<String[]> getIllegalSongs() {
        return new SimpleIRResponse<>(false, "Not supported", new String[0]);
    }
}
