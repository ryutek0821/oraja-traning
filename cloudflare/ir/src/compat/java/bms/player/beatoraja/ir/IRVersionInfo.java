/* SPDX-License-Identifier: GPL-3.0-only */
package bms.player.beatoraja.ir;

public final class IRVersionInfo {
    public final String version; public final String message; public final String downloadURL;
    public IRVersionInfo(String version, String message, String downloadURL) {
        this.version = version; this.message = message; this.downloadURL = downloadURL;
    }
}
