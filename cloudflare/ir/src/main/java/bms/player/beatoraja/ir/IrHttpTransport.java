/* SPDX-License-Identifier: AGPL-3.0-only */
package bms.player.beatoraja.ir;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/** Minimal HTTPS transport. Secrets are restricted to the Authorization header. */
final class IrHttpTransport {

    record Response(int status, String body) {}

    private final URI playsEndpoint;
    private final HttpClient client;
    private final int maxResponseBytes;

    IrHttpTransport(IrConfiguration configuration) {
        URI base = configuration.baseUrl;
        boolean https = "https".equalsIgnoreCase(base.getScheme());
        boolean loopback = "http".equalsIgnoreCase(base.getScheme())
                && ("localhost".equalsIgnoreCase(base.getHost())
                    || "127.0.0.1".equals(base.getHost())
                    || "::1".equals(base.getHost()))
                && configuration.allowInsecureLoopback;
        if (!https && !loopback) {
            throw new IllegalArgumentException("IR transport requires HTTPS");
        }
        playsEndpoint = base.resolve("v1/plays");
        maxResponseBytes = configuration.maxResponseBytes;
        client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .followRedirects(HttpClient.Redirect.NEVER)
                .build();
    }

    Response post(String token, String eventJson) throws IOException, InterruptedException {
        HttpRequest request = HttpRequest.newBuilder(playsEndpoint)
                .timeout(Duration.ofSeconds(10))
                .header("Authorization", "Bearer " + token)
                .header("Content-Type", "application/json")
                .header("Accept", "application/json")
                .header("Idempotency-Key", eventId(eventJson))
                .POST(HttpRequest.BodyPublishers.ofString(eventJson))
                .build();
        HttpResponse<java.io.InputStream> response = client.send(request, HttpResponse.BodyHandlers.ofInputStream());
        byte[] body;
        try (java.io.InputStream input = response.body()) {
            body = input.readNBytes(maxResponseBytes + 1);
        }
        if (body.length > maxResponseBytes) {
            throw new IOException("IR response exceeds configured limit");
        }
        return new Response(response.statusCode(), new String(body, java.nio.charset.StandardCharsets.UTF_8));
    }

    private static String eventId(String json) {
        Object value = JsonCodec.object(json).get("event_id");
        if (!(value instanceof String id) || !UuidV7.isV7(id)) {
            throw new IllegalArgumentException("event_id missing from request");
        }
        return id;
    }
}
