/* SPDX-License-Identifier: GPL-3.0-only */
package bms.player.beatoraja.ir;

import java.lang.reflect.Constructor;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** Strict, bounded mapping from the owner-only Worker read contract to the upstream IR ABI. */
final class IrReadMapper {

    private static final int MAX_SCORES = 1000;

    private IrReadMapper() {
    }

    static IRPlayerData player(String json, String expectedId) {
        Map<String, Object> root = contract(json);
        Map<String, Object> player = object(root.get("player"), "player");
        String id = string(player, "id", 64);
        if (!id.equals(expectedId)) throw new IllegalArgumentException("IR player owner mismatch");
        return new IRPlayerData(id, string(player, "name", 120), string(player, "rank", 120, true));
    }

    static IRScoreData[] scores(String json, String expectedId) {
        Object raw = contract(json).get("scores");
        if (!(raw instanceof List<?> values) || values.size() > MAX_SCORES) {
            throw new IllegalArgumentException("invalid bounded IR score response");
        }
        List<IRScoreData> result = new ArrayList<>(values.size());
        for (Object value : values) {
            Map<String, Object> score = object(value, "score");
            if (!expectedId.equals(string(score, "id", 64))) {
                throw new IllegalArgumentException("IR score owner mismatch");
            }
            result.add(newScore(score));
        }
        return result.toArray(IRScoreData[]::new);
    }

    static void requireEmpty(String json, String field) {
        Object value = contract(json).get(field);
        if (!(value instanceof List<?> list) || !list.isEmpty()) {
            throw new IllegalArgumentException("unsupported IR federation response");
        }
    }

    static IRVersionInfo version(String json) {
        Map<String, Object> value = object(contract(json).get("version"), "version");
        return new IRVersionInfo(string(value, "version", 120, true), string(value, "message", 512, true),
                nullableString(value.get("download_url"), 2048));
    }

    private static IRScoreData newScore(Map<String, Object> score) {
        Constructor<?> official = null;
        Constructor<?> compatibility = null;
        for (Constructor<?> candidate : IRScoreData.class.getConstructors()) {
            if (candidate.getParameterCount() == 31) official = candidate;
            if (candidate.getParameterCount() == 22) compatibility = candidate;
        }
        try {
            if (official != null) {
                Class<?> clearType = official.getParameterTypes()[4];
                Object[] arguments = {
                    hash(score, "sha256"), integer(score, "lntype", 0, 2),
                    string(score, "id", 64), string(score, "player", 120, true),
                    clear(clearType, integer(score, "clear", 0, 10)), number(score, "date", 0, Long.MAX_VALUE),
                    integer(score, "epg", 0, Integer.MAX_VALUE), integer(score, "lpg", 0, Integer.MAX_VALUE),
                    integer(score, "egr", 0, Integer.MAX_VALUE), integer(score, "lgr", 0, Integer.MAX_VALUE),
                    integer(score, "egd", 0, Integer.MAX_VALUE), integer(score, "lgd", 0, Integer.MAX_VALUE),
                    integer(score, "ebd", 0, Integer.MAX_VALUE), integer(score, "lbd", 0, Integer.MAX_VALUE),
                    integer(score, "epr", 0, Integer.MAX_VALUE), integer(score, "lpr", 0, Integer.MAX_VALUE),
                    integer(score, "ems", 0, Integer.MAX_VALUE), integer(score, "lms", 0, Integer.MAX_VALUE),
                    number(score, "avgjudge", Long.MIN_VALUE, Long.MAX_VALUE),
                    integer(score, "maxcombo", 0, Integer.MAX_VALUE), integer(score, "notes", 0, Integer.MAX_VALUE),
                    integer(score, "passnotes", 0, Integer.MAX_VALUE), integer(score, "minbp", 0, Integer.MAX_VALUE),
                    integer(score, "option", 0, 399), number(score, "seed", 0, Long.MAX_VALUE),
                    integer(score, "assist", 0, 2), integer(score, "gauge", 0, 5),
                    null, null, null, string(score, "skin", 120, true),
                };
                return (IRScoreData) official.newInstance(arguments);
            }
            if (compatibility != null) {
                Class<?> clearType = compatibility.getParameterTypes()[1];
                Object[] arguments = {
                    number(score, "date", 0, Long.MAX_VALUE), clear(clearType, integer(score, "clear", 0, 10)),
                    integer(score, "epg", 0, Integer.MAX_VALUE), integer(score, "lpg", 0, Integer.MAX_VALUE),
                    integer(score, "egr", 0, Integer.MAX_VALUE), integer(score, "lgr", 0, Integer.MAX_VALUE),
                    integer(score, "egd", 0, Integer.MAX_VALUE), integer(score, "lgd", 0, Integer.MAX_VALUE),
                    integer(score, "ebd", 0, Integer.MAX_VALUE), integer(score, "lbd", 0, Integer.MAX_VALUE),
                    integer(score, "epr", 0, Integer.MAX_VALUE), integer(score, "lpr", 0, Integer.MAX_VALUE),
                    integer(score, "ems", 0, Integer.MAX_VALUE), integer(score, "lms", 0, Integer.MAX_VALUE),
                    integer(score, "maxcombo", 0, Integer.MAX_VALUE), integer(score, "notes", 0, Integer.MAX_VALUE),
                    integer(score, "passnotes", 0, Integer.MAX_VALUE), integer(score, "minbp", 0, Integer.MAX_VALUE),
                    integer(score, "option", 0, 399), number(score, "seed", 0, Long.MAX_VALUE),
                    integer(score, "assist", 0, 2), integer(score, "gauge", 0, 5),
                };
                return (IRScoreData) compatibility.newInstance(arguments);
            }
            throw new IllegalArgumentException("unsupported pinned IRScoreData ABI");
        } catch (InstantiationException | IllegalAccessException | InvocationTargetException failure) {
            throw new IllegalArgumentException("invalid IR score response", failure);
        }
    }

    private static Object clear(Class<?> type, int id) {
        try {
            Method factory = type.getMethod("getClearTypeByID", int.class);
            return factory.invoke(null, id);
        } catch (NoSuchMethodException ignored) {
            try {
                return type.getConstructor(int.class).newInstance(id);
            } catch (ReflectiveOperationException failure) {
                throw new IllegalArgumentException("unsupported clear type ABI", failure);
            }
        } catch (IllegalAccessException | InvocationTargetException failure) {
            throw new IllegalArgumentException("invalid clear type", failure);
        }
    }

    private static Map<String, Object> contract(String json) {
        Map<String, Object> root = JsonCodec.object(json);
        if (!"ir-read.v1".equals(root.get("contract"))) throw new IllegalArgumentException("invalid IR read contract");
        return root;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> object(Object value, String field) {
        if (!(value instanceof Map<?, ?>)) throw new IllegalArgumentException("invalid " + field);
        return (Map<String, Object>) value;
    }

    private static String hash(Map<String, Object> value, String field) {
        String result = string(value, field, 64);
        if (!result.matches("[0-9a-f]{64}")) throw new IllegalArgumentException("invalid " + field);
        return result;
    }

    private static int integer(Map<String, Object> value, String field, int minimum, int maximum) {
        long parsed = number(value, field, minimum, maximum);
        return (int) parsed;
    }

    private static long number(Map<String, Object> value, String field, long minimum, long maximum) {
        Object raw = value.get(field);
        if (!(raw instanceof Number numeric)) throw new IllegalArgumentException("invalid " + field);
        long parsed = numeric.longValue();
        if (parsed < minimum || parsed > maximum || numeric.doubleValue() != (double) parsed) {
            throw new IllegalArgumentException("invalid " + field);
        }
        return parsed;
    }

    private static String string(Map<String, Object> value, String field, int maximum) {
        return string(value, field, maximum, false);
    }

    private static String string(Map<String, Object> value, String field, int maximum, boolean emptyAllowed) {
        Object raw = value.get(field);
        if (!(raw instanceof String result) || (!emptyAllowed && result.isEmpty()) || result.length() > maximum
                || result.chars().anyMatch(character -> Character.isISOControl((char) character))) {
            throw new IllegalArgumentException("invalid " + field);
        }
        return result;
    }

    private static String nullableString(Object raw, int maximum) {
        if (raw == null) return null;
        if (!(raw instanceof String value) || value.length() > maximum
                || value.chars().anyMatch(character -> Character.isISOControl((char) character))) {
            throw new IllegalArgumentException("invalid URL");
        }
        return value;
    }
}
