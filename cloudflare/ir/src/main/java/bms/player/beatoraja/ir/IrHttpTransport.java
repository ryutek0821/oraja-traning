/* SPDX-License-Identifier: GPL-3.0-only */
package bms.player.beatoraja.ir;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.StringJoiner;

/** Minimal HTTPS transport. Secrets are restricted to the Authorization header. */
final class IrHttpTransport {

    record Response(int status, String body) {}

    private final URI baseEndpoint;
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
        baseEndpoint = base;
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
        return send(request);
    }

    Response get(String token, String relativePath, Map<String, String> query)
            throws IOException, InterruptedException {
        if (relativePath == null || !relativePath.startsWith("v1/ir/")
                || relativePath.contains("..") || relativePath.indexOf('?') >= 0) {
            throw new IllegalArgumentException("invalid IR read path");
        }
        StringJoiner encoded = new StringJoiner("&");
        for (Map.Entry<String, String> entry : query.entrySet()) {
            if (entry.getKey() == null || entry.getValue() == null) {
                throw new IllegalArgumentException("invalid IR read query");
            }
            encoded.add(URLEncoder.encode(entry.getKey(), StandardCharsets.UTF_8)
                    + "=" + URLEncoder.encode(entry.getValue(), StandardCharsets.UTF_8));
        }
        URI endpoint = baseEndpoint.resolve(relativePath + (encoded.length() == 0 ? "" : "?" + encoded));
        if (!baseEndpoint.getScheme().equalsIgnoreCase(endpoint.getScheme())
                || !baseEndpoint.getHost().equalsIgnoreCase(endpoint.getHost())
                || effectivePort(baseEndpoint) != effectivePort(endpoint)) {
            throw new IllegalArgumentException("IR read endpoint changed origin");
        }
        HttpRequest request = HttpRequest.newBuilder(endpoint)
                .timeout(Duration.ofSeconds(10))
                .header("Authorization", "Bearer " + token)
                .header("Accept", "application/json")
                .GET()
                .build();
        return send(request);
    }

    private Response send(HttpRequest request) throws IOException, InterruptedException {
        HttpResponse<java.io.InputStream> response = client.send(request, HttpResponse.BodyHandlers.ofInputStream());
        byte[] body;
        try (java.io.InputStream input = response.body()) {
            body = input.readNBytes(maxResponseBytes + 1);
        }
        if (body.length > maxResponseBytes) {
            throw new IOException("IR response exceeds configured limit");
        }
        return new Response(response.statusCode(), new String(body, StandardCharsets.UTF_8));
    }

    private static int effectivePort(URI value) {
        if (value.getPort() >= 0) return value.getPort();
        return "https".equalsIgnoreCase(value.getScheme()) ? 443 : 80;
    }

    private static String eventId(String json) {
        Object value = JsonCodec.object(json).get("event_id");
        if (!(value instanceof String id) || !UuidV7.isV7(id)) {
            throw new IllegalArgumentException("event_id missing from request");
        }
        return id;
    }
}
