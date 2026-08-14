/* SPDX-License-Identifier: GPL-3.0-only */
package bms.player.beatoraja.ir;

public final class IRAccount {
    public final String id; public final String password; public final String name;
    public IRAccount(String id, String password, String name) {
        this.id = id; this.password = password; this.name = name;
    }
}
