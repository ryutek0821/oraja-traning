/* SPDX-License-Identifier: GPL-3.0-only */
package bms.player.beatoraja.ir;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/** Maps only the allowlisted beatoraja IR DTO fields into ir-event v1. */
public final class IrEventMapper {

    private static final String[] OPTION_NAMES = {
        "NORMAL", "MIRROR", "RANDOM", "R-RANDOM", "S-RANDOM", "H-RANDOM",
        "SPIRAL", "ALL-SCR", "EX-RANDOM", "EX-S-RANDOM"
    };

    private IrEventMapper() {
    }

    public static IrEvent mapPlay(String eventId, String profileId, String deviceId,
            IRChartData chart, IRScoreData score, Instant now) {
        validateIdentity(eventId, profileId, deviceId);
        if (chart == null || score == null || now == null) {
            throw new IllegalArgumentException("chart, score, and time are required");
        }
        return new IrEvent(eventId, profileId, deviceId, eventTime(score.date, now),
                mapChart(chart), mapPlay(score), false, null);
    }

    public static IrEvent mapCourse(String eventId, String profileId, String deviceId,
            IRCourseData course, IRScoreData score, Instant now) {
        validateIdentity(eventId, profileId, deviceId);
        if (course == null || score == null || now == null || course.charts == null
                || course.charts.length == 0) {
            throw new IllegalArgumentException("course with at least one chart is required");
        }
        // IRCourseData has no server course UUID.  The v1 Worker only needs a
        // UUIDv7 partition key and excludes is_course=true from model input;
        // binding it to this event keeps retries deterministic without hashing
        // the user-visible course name.
        return new IrEvent(eventId, profileId, deviceId, eventTime(score.date, now),
                mapChart(course.charts[0]), mapPlay(score), true, eventId);
    }

    private static IrEvent.Chart mapChart(IRChartData chart) {
        String sha256 = requireHash(chart.sha256, 64, "sha256");
        String md5 = chart.md5 == null || chart.md5.isBlank()
                ? null : requireHash(chart.md5, 32, "md5");
        int lnMode = chart.lntype;
        if (lnMode < 0 || lnMode > 2) {
            throw new IllegalArgumentException("unsupported LN mode");
        }
        if (chart.mode == null || !"BEAT_7K".equals(chart.mode.name())) {
            throw new IllegalArgumentException("only beatoraja SP7 charts are accepted");
        }
        return new IrEvent.Chart(sha256, md5, lnMode);
    }

    private static IrEvent.Play mapPlay(IRScoreData score) {
        int notes = nonNegative(score.notes, "notes");
        int passnotes = nonNegative(score.passnotes, "passnotes");
        if (passnotes > notes) {
            throw new IllegalArgumentException("passnotes exceeds notes");
        }
        int minbp = score.minbp < 0 || score.minbp == Integer.MAX_VALUE ? 0 : score.minbp;
        int combo = nonNegative(score.maxcombo, "combo");
        List<String> options = mapOptions(score.option);
        IrEvent.Judgements judgements = new IrEvent.Judgements(
                nonNegative(score.epg, "epg"), nonNegative(score.lpg, "lpg"),
                nonNegative(score.egr, "egr"), nonNegative(score.lgr, "lgr"),
                nonNegative(score.egd, "egd"), nonNegative(score.lgd, "lgd"),
                nonNegative(score.ebd, "ebd"), nonNegative(score.lbd, "lbd"),
                nonNegative(score.epr, "epr"), nonNegative(score.lpr, "lpr"),
                nonNegative(score.ems, "ems"), nonNegative(score.lms, "lms"));
        int clear = score.clear == null ? -1 : score.clear.id;
        if (clear < 0 || clear > 10) {
            throw new IllegalArgumentException("invalid clear type");
        }
        int gauge = score.gauge;
        String gaugeKind = switch (gauge) {
            case 0 -> "ASSIST_EASY";
            case 1 -> "EASY";
            case 2 -> "NORMAL";
            case 3 -> "HARD";
            case 4 -> "EXHARD";
            case 5 -> "HAZARD";
            default -> "NORMAL"; // -1 means beatoraja changed gauge type mid-play.
        };
        String assistKind = switch (score.assist) {
            case 0 -> "NONE";
            case 1 -> "LIGHT_ASSIST_EASY";
            default -> "ASSIST_EASY";
        };
        long seed = Math.max(0L, score.seed);
        int exScore = safeExScore(judgements);
        return new IrEvent.Play(clear, gaugeKind, assistKind, notes, passnotes, minbp,
                exScore, combo, options, seed, 0, judgements);
    }

    private static List<String> mapOptions(int encoded) {
        if (encoded < 0 || encoded > 399) {
            throw new IllegalArgumentException("unsupported option encoding");
        }
        Set<String> result = new LinkedHashSet<>();
        int primary = encoded % 10;
        result.add(OPTION_NAMES[primary]);
        int secondary = (encoded / 10) % 10;
        if (encoded >= 10 && encoded < 100) {
            result.add(OPTION_NAMES[secondary]);
        }
        int battle = encoded / 100;
        if (battle == 2) {
            result.add("BATTLE");
        } else if (battle == 3) {
            result.add("BATTLE-ASSIST");
        } else if (battle != 0) {
            throw new IllegalArgumentException("unsupported battle option encoding");
        }
        return new ArrayList<>(result).subList(0, Math.min(4, result.size()));
    }

    private static int safeExScore(IrEvent.Judgements j) {
        long value = ((long) j.epg + j.lpg) * 2L + j.egr + j.lgr;
        if (value > Integer.MAX_VALUE) {
            throw new IllegalArgumentException("ex score overflow");
        }
        return (int) value;
    }

    private static Instant eventTime(long scoreDate, Instant now) {
        return scoreDate > 0 ? Instant.ofEpochSecond(scoreDate) : now;
    }

    private static int nonNegative(int value, String name) {
        if (value < 0) {
            throw new IllegalArgumentException(name + " must be non-negative");
        }
        return value;
    }

    private static String requireHash(String value, int length, String name) {
        if (value == null || value.length() != length) {
            throw new IllegalArgumentException("invalid " + name);
        }
        for (int i = 0; i < value.length(); i++) {
            if (Character.digit(value.charAt(i), 16) < 0) {
                throw new IllegalArgumentException("invalid " + name);
            }
        }
        return value.toLowerCase(Locale.ROOT);
    }

    private static void validateIdentity(String eventId, String profileId, String deviceId) {
        if (!UuidV7.isV7(eventId) || !UuidV7.isV7(profileId) || !UuidV7.isV7(deviceId)) {
            throw new IllegalArgumentException("event, profile, and device IDs must be UUIDv7");
        }
    }
}
