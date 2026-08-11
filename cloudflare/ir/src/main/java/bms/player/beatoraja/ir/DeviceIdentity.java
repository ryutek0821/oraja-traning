/* SPDX-License-Identifier: AGPL-3.0-only */
package bms.player.beatoraja.ir;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;

/** Persists only a local UUIDv7 device identifier, never a token. */
public final class DeviceIdentity {

    private final Path file;
    private final String id;

    public DeviceIdentity(Path spoolDirectory) throws IOException {
        Files.createDirectories(spoolDirectory);
        this.file = spoolDirectory.resolve("device.id");
        this.id = loadOrCreate();
    }

    public String id() {
        return id;
    }

    private String loadOrCreate() throws IOException {
        if (Files.exists(file)) {
            String value = Files.readString(file, StandardCharsets.US_ASCII).trim();
            if (!UuidV7.isV7(value)) {
                throw new IOException("invalid local device identity");
            }
            return value;
        }
        String value = UuidV7.next();
        Path temporary = Files.createTempFile(file.getParent(), "device.", ".tmp");
        try {
            byte[] bytes = (value + "\n").getBytes(StandardCharsets.US_ASCII);
            try (FileChannel channel = FileChannel.open(temporary, StandardOpenOption.WRITE)) {
                channel.write(ByteBuffer.wrap(bytes));
                channel.force(true);
            }
            try {
                Files.move(temporary, file, StandardCopyOption.ATOMIC_MOVE);
            } catch (AtomicMoveNotSupportedException e) {
                Files.move(temporary, file);
            }
            forceDirectory(file.getParent());
            return value;
        } catch (java.nio.file.FileAlreadyExistsException e) {
            Files.deleteIfExists(temporary);
            String existing = Files.readString(file, StandardCharsets.US_ASCII).trim();
            if (!UuidV7.isV7(existing)) {
                throw new IOException("invalid concurrent device identity");
            }
            return existing;
        } finally {
            Files.deleteIfExists(temporary);
        }
    }

    static void forceDirectory(Path directory) {
        try (FileChannel channel = FileChannel.open(directory, StandardOpenOption.READ)) {
            channel.force(true);
        } catch (IOException | UnsupportedOperationException ignored) {
            // Directory fsync is unavailable on some Windows filesystems.
        }
    }
}
