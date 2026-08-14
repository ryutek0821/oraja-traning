/* SPDX-License-Identifier: AGPL-3.0-only */
package bms.player.beatoraja.ir;

import java.net.URI;
import java.nio.file.Path;

/** Immutable, secret-free client configuration. */
public final class IrConfiguration {

    public static final URI DEFAULT_BASE_URL = URI.create("https://api.oraja-training.dev/");

    public final URI baseUrl;
    public final Path spoolDirectory;
    public final boolean sendAlways;
    public final boolean allowInsecureLoopback;
    public final int maxResponseBytes;

    public IrConfiguration(URI baseUrl, Path spoolDirectory, boolean sendAlways,
            boolean allowInsecureLoopback, int maxResponseBytes) {
        this.baseUrl = normalizeBaseUrl(baseUrl);
        this.spoolDirectory = spoolDirectory.toAbsolutePath().normalize();
        this.sendAlways = sendAlways;
        this.allowInsecureLoopback = allowInsecureLoopback;
        if (maxResponseBytes < 1024 || maxResponseBytes > 16 * 1024 * 1024) {
            throw new IllegalArgumentException("invalid response limit");
        }
        this.maxResponseBytes = maxResponseBytes;
    }

    public IrConfiguration(URI baseUrl, Path spoolDirectory, boolean sendAlways) {
        this(baseUrl, spoolDirectory, sendAlways, false, 1_048_576);
    }

    public static IrConfiguration fromSystem() {
        String base = setting("oraja.ir.baseUrl", "ORAJA_IR_BASE_URL");
        String spool = setting("oraja.ir.spoolDir", "ORAJA_IR_SPOOL_DIR");
        String send = setting("IR_SEND_ALWAYS", "IR_SEND_ALWAYS");
        Path defaultSpool = Path.of(System.getProperty("user.home", "."),
                ".beatoraja", "oraja-training-ir", "spool");
        return new IrConfiguration(
                base == null || base.isBlank() ? DEFAULT_BASE_URL : URI.create(base),
                spool == null || spool.isBlank() ? defaultSpool : Path.of(spool),
                Boolean.parseBoolean(send),
                Boolean.parseBoolean(System.getProperty("oraja.ir.allowInsecureLoopback", "false")),
                Integer.getInteger("oraja.ir.maxResponseBytes", 1_048_576));
    }

    public static boolean validProfileId(String profileId) {
        return UuidV7.isV7(profileId);
    }

    public static boolean validToken(String token) {
        if (token == null || token.length() < 16 || token.length() > 512) {
            return false;
        }
        for (int i = 0; i < token.length(); i++) {
            if (Character.isISOControl(token.charAt(i)) || Character.isWhitespace(token.charAt(i))) {
                return false;
            }
        }
        return true;
    }

    private static String setting(String property, String environment) {
        String value = System.getProperty(property);
        if (value != null) {
            return value;
        }
        return System.getenv(environment);
    }

    private static URI normalizeBaseUrl(URI value) {
        if (value == null || value.getScheme() == null || value.getHost() == null) {
            throw new IllegalArgumentException("IR base URL must have scheme and host");
        }
        String text = value.toString();
        return URI.create(text.endsWith("/") ? text : text + "/");
    }
}
