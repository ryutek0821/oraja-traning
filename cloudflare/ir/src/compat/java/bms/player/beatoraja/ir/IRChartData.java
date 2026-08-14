package bms.player.beatoraja.ir;

public final class IRChartData {
    public enum Mode { BEAT_7K, OTHER }
    public final String md5; public final String sha256; public final Mode mode; public final int lntype;
    public IRChartData(String md5, String sha256, Mode mode, int lntype) {
        this.md5 = md5; this.sha256 = sha256; this.mode = mode; this.lntype = lntype;
    }
}
