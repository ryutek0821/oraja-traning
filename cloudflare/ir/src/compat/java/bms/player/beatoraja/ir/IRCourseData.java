package bms.player.beatoraja.ir;

public final class IRCourseData {
    public final String name; public final IRChartData[] charts; public final int lntype;
    public IRCourseData(String name, IRChartData[] charts, int lntype) {
        this.name = name; this.charts = charts; this.lntype = lntype;
    }
}
