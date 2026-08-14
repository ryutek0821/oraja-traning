/* SPDX-License-Identifier: GPL-3.0-only */
package bms.player.beatoraja.ir;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;

/** Crash-safe, profile-partitioned queue of immutable event JSON files. */
final class DurableSpool {

    record Entry(Path path, String eventId, String profileId, String json) {}

    private final Path root;
    private final Path pending;
    private final Path quarantine;
    private final Path rejected;
    private final Path lockFile;

    DurableSpool(Path root) throws IOException {
        this.root = root;
        pending = root.resolve("pending");
        quarantine = root.resolve("quarantine");
        rejected = root.resolve("rejected");
        lockFile = root.resolve("spool.lock");
        Files.createDirectories(pending);
        Files.createDirectories(quarantine);
        Files.createDirectories(rejected);
    }

    void persist(IrEvent event) throws IOException {
        withLock(() -> {
            Path destination = pending.resolve(event.eventId + ".json");
            if (Files.exists(destination)) {
                return null;
            }
            Path temporary = Files.createTempFile(root, event.eventId + ".", ".tmp");
            try {
                byte[] bytes = (event.toJson() + "\n").getBytes(StandardCharsets.UTF_8);
                try (FileChannel channel = FileChannel.open(temporary, StandardOpenOption.WRITE)) {
                    ByteBuffer buffer = ByteBuffer.wrap(bytes);
                    while (buffer.hasRemaining()) {
                        channel.write(buffer);
                    }
                    channel.force(true);
                }
                atomicMove(temporary, destination);
                DeviceIdentity.forceDirectory(pending);
            } finally {
                Files.deleteIfExists(temporary);
            }
            return null;
        });
    }

    List<Entry> entriesFor(String profileId) throws IOException {
        return withLock(() -> {
            List<Entry> result = new ArrayList<>();
            try (var paths = Files.list(pending)) {
                for (Path path : paths.filter(p -> p.getFileName().toString().endsWith(".json"))
                        .sorted(Comparator.comparing(p -> p.getFileName().toString())).toList()) {
                    try {
                        String json = Files.readString(path, StandardCharsets.UTF_8);
                        Map<String, Object> object = JsonCodec.object(json);
                        String eventId = string(object, "event_id");
                        String owner = string(object, "profile_id");
                        String expectedName = eventId + ".json";
                        if (!UuidV7.isV7(eventId) || !UuidV7.isV7(owner)
                                || !expectedName.equals(path.getFileName().toString())) {
                            throw new IllegalArgumentException("invalid spool identity");
                        }
                        if (owner.equals(profileId)) {
                            result.add(new Entry(path, eventId, owner, json.strip()));
                        }
                    } catch (IllegalArgumentException | IOException e) {
                        move(path, quarantine);
                    }
                }
            }
            return result;
        });
    }

    void acknowledge(Entry entry) throws IOException {
        withLock(() -> {
            Files.deleteIfExists(entry.path());
            DeviceIdentity.forceDirectory(pending);
            return null;
        });
    }

    void reject(Entry entry) throws IOException {
        withLock(() -> {
            move(entry.path(), rejected);
            return null;
        });
    }

    boolean contains(String eventId) {
        return Files.exists(pending.resolve(eventId + ".json"));
    }

    private void move(Path source, Path destinationDirectory) throws IOException {
        Files.createDirectories(destinationDirectory);
        Path destination = destinationDirectory.resolve(source.getFileName());
        if (Files.exists(destination)) {
            destination = destinationDirectory.resolve(UuidV7.next() + "-" + source.getFileName());
        }
        atomicMove(source, destination);
        DeviceIdentity.forceDirectory(destinationDirectory);
    }

    private static String string(Map<String, Object> object, String key) {
        Object value = object.get(key);
        if (!(value instanceof String text)) {
            throw new IllegalArgumentException("missing " + key);
        }
        return text;
    }

    private static void atomicMove(Path source, Path destination) throws IOException {
        try {
            Files.move(source, destination, StandardCopyOption.ATOMIC_MOVE);
        } catch (AtomicMoveNotSupportedException e) {
            Files.move(source, destination);
        }
    }

    private <T> T withLock(IOOperation<T> operation) throws IOException {
        try (FileChannel channel = FileChannel.open(lockFile, StandardOpenOption.CREATE,
                StandardOpenOption.WRITE); FileLock ignored = channel.tryLock()) {
            if (ignored == null) {
                throw new IOException("IR spool is in use by another process");
            }
            return operation.run();
        } catch (java.nio.channels.OverlappingFileLockException e) {
            throw new IOException("IR spool is in use by another client", e);
        }
    }

    @FunctionalInterface
    private interface IOOperation<T> {
        T run() throws IOException;
    }
}
