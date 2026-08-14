/* SPDX-License-Identifier: GPL-3.0-only */
package bms.player.beatoraja.ir;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Immutable allowlisted ir-event v1 envelope. */
public final class IrEvent {

    public final String eventId;
    public final String profileId;
    public final String deviceId;
    public final String occurredAt;
    public final Chart chart;
    public final Play play;
    public final boolean course;
    public final String courseId;

    IrEvent(String eventId, String profileId, String deviceId, Instant occurredAt,
            Chart chart, Play play, boolean course, String courseId) {
        this.eventId = eventId;
        this.profileId = profileId;
        this.deviceId = deviceId;
        this.occurredAt = occurredAt.toString();
        this.chart = chart;
        this.play = play;
        this.course = course;
        this.courseId = courseId;
    }

    public String toJson() {
        return JsonCodec.stringify(toMap());
    }

    Map<String, Object> toMap() {
        Map<String, Object> root = new LinkedHashMap<>();
        root.put("contract", "ir-event");
        root.put("schema_version", "1");
        root.put("event_id", eventId);
        root.put("profile_id", profileId);
        root.put("device_id", deviceId);
        root.put("occurred_at", occurredAt);
        root.put("game_mode", "SP7");
        root.put("rule", "BEATORAJA-SP7");
        root.put("chart", chart.toMap());
        root.put("play", play.toMap());
        root.put("is_course", course);
        if (course) {
            root.put("course_id", courseId);
        }
        Map<String, Object> provenance = new LinkedHashMap<>();
        provenance.put("trust_domain", "official");
        provenance.put("source", "official_ir");
        provenance.put("aggregate_eligible", false);
        root.put("provenance", provenance);
        return root;
    }

    public static final class Chart {
        public final String sha256;
        public final String md5;
        public final int lnMode;

        Chart(String sha256, String md5, int lnMode) {
            this.sha256 = sha256;
            this.md5 = md5;
            this.lnMode = lnMode;
        }

        Map<String, Object> toMap() {
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("sha256", sha256);
            if (md5 != null) {
                result.put("md5", md5);
            }
            result.put("ln_mode", lnMode);
            return result;
        }
    }

    public static final class Play {
        public final int clear;
        public final String gaugeKind;
        public final String assistKind;
        public final int notes;
        public final int passnotes;
        public final int minbp;
        public final int exScore;
        public final int combo;
        public final List<String> options;
        public final long seed;
        public final int random;
        public final Judgements judgements;

        Play(int clear, String gaugeKind, String assistKind, int notes, int passnotes,
                int minbp, int exScore, int combo, List<String> options, long seed,
                int random, Judgements judgements) {
            this.clear = clear;
            this.gaugeKind = gaugeKind;
            this.assistKind = assistKind;
            this.notes = notes;
            this.passnotes = passnotes;
            this.minbp = minbp;
            this.exScore = exScore;
            this.combo = combo;
            this.options = List.copyOf(options);
            this.seed = seed;
            this.random = random;
            this.judgements = judgements;
        }

        Map<String, Object> toMap() {
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("clear", clear);
            result.put("gauge_kind", gaugeKind);
            result.put("assist_kind", assistKind);
            result.put("notes", notes);
            result.put("passnotes", passnotes);
            result.put("minbp", minbp);
            result.put("ex_score", exScore);
            result.put("combo", combo);
            result.put("options", new ArrayList<>(options));
            result.put("seed", seed);
            result.put("random", random);
            result.put("judgements", judgements.toMap());
            return result;
        }
    }

    public static final class Judgements {
        public final int epg;
        public final int lpg;
        public final int egr;
        public final int lgr;
        public final int egd;
        public final int lgd;
        public final int ebd;
        public final int lbd;
        public final int epr;
        public final int lpr;
        public final int ems;
        public final int lms;

        Judgements(int epg, int lpg, int egr, int lgr, int egd, int lgd,
                int ebd, int lbd, int epr, int lpr, int ems, int lms) {
            this.epg = epg;
            this.lpg = lpg;
            this.egr = egr;
            this.lgr = lgr;
            this.egd = egd;
            this.lgd = lgd;
            this.ebd = ebd;
            this.lbd = lbd;
            this.epr = epr;
            this.lpr = lpr;
            this.ems = ems;
            this.lms = lms;
        }

        Map<String, Object> toMap() {
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("epg", epg);
            result.put("lpg", lpg);
            result.put("egr", egr);
            result.put("lgr", lgr);
            result.put("egd", egd);
            result.put("lgd", lgd);
            result.put("ebd", ebd);
            result.put("lbd", lbd);
            result.put("epr", epr);
            result.put("lpr", lpr);
            result.put("ems", ems);
            result.put("lms", lms);
            return result;
        }
    }
}
