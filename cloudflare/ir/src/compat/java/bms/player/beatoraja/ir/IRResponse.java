/* SPDX-License-Identifier: GPL-3.0-only */
package bms.player.beatoraja.ir;

public interface IRResponse<T> {
    boolean isSucceeded();
    String getMessage();
    T getData();
}
