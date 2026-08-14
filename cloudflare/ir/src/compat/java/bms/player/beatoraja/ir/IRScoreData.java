package bms.player.beatoraja.ir;

public final class IRScoreData {
    public static final class Clear { public final int id; public Clear(int id) { this.id = id; } }
    public final long date; public final int epg, lpg, egr, lgr, egd, lgd, ebd, lbd, epr, lpr, ems, lms;
    public final int maxcombo, notes, passnotes, minbp, option, assist, gauge; public final long seed;
    public final Clear clear;
    public IRScoreData(long date, Clear clear, int epg, int lpg, int egr, int lgr,
            int egd, int lgd, int ebd, int lbd, int epr, int lpr, int ems, int lms,
            int maxcombo, int notes, int passnotes, int minbp, int option, long seed,
            int assist, int gauge) {
        this.date = date; this.clear = clear; this.epg = epg; this.lpg = lpg;
        this.egr = egr; this.lgr = lgr; this.egd = egd; this.lgd = lgd;
        this.ebd = ebd; this.lbd = lbd; this.epr = epr; this.lpr = lpr;
        this.ems = ems; this.lms = lms; this.maxcombo = maxcombo; this.notes = notes;
        this.passnotes = passnotes; this.minbp = minbp; this.option = option;
        this.seed = seed; this.assist = assist; this.gauge = gauge;
    }
}
