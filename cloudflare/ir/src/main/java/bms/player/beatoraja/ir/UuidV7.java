/* SPDX-License-Identifier: GPL-3.0-only */
package bms.player.beatoraja.ir;

import java.security.SecureRandom;
import java.util.UUID;

/** UUIDv7 generator with monotonic ordering within one JVM. */
public final class UuidV7 {

    private static final SecureRandom RANDOM = new SecureRandom();
    private static long lastTimestamp = -1L;
    private static long lastRandomA;
    private static long lastRandomB;

    private UuidV7() {
    }

    public static synchronized String next() {
        long timestamp = System.currentTimeMillis() & 0xffffffffffffL;
        long randomA;
        long randomB;
        if (timestamp > lastTimestamp) {
            randomA = RANDOM.nextInt(1 << 12);
            randomB = RANDOM.nextLong() & 0x3fffffffffffffffL;
        } else {
            timestamp = Math.max(timestamp, lastTimestamp);
            randomA = lastRandomA;
            randomB = (lastRandomB + 1) & 0x3fffffffffffffffL;
            if (randomB == 0) {
                randomA = (randomA + 1) & 0xfff;
            }
        }
        lastTimestamp = timestamp;
        lastRandomA = randomA;
        lastRandomB = randomB;

        long most = (timestamp << 16) | 0x7000L | randomA;
        long least = 0x8000000000000000L | randomB;
        return new UUID(most, least).toString();
    }

    public static boolean isV7(String value) {
        if (value == null) {
            return false;
        }
        try {
            UUID uuid = UUID.fromString(value);
            return uuid.toString().equalsIgnoreCase(value)
                    && uuid.version() == 7
                    && uuid.variant() == 2;
        } catch (IllegalArgumentException e) {
            return false;
        }
    }
}
