/* SPDX-License-Identifier: GPL-3.0-only */
package bms.player.beatoraja.ir;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Small JSON codec used at the plugin boundary.
 *
 * <p>The plugin deliberately has no runtime dependency other than the Java 17
 * standard library.  This codec accepts only the JSON types needed by the
 * versioned event and ACK contracts and rejects duplicate object keys.</p>
 */
public final class JsonCodec {

    private JsonCodec() {
    }

    public static String stringify(Object value) {
        StringBuilder out = new StringBuilder();
        write(value, out);
        return out.toString();
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> object(String json) {
        Object value = new Parser(json).parse();
        if (!(value instanceof Map<?, ?> map)) {
            throw new IllegalArgumentException("JSON object required");
        }
        return (Map<String, Object>) map;
    }

    private static void write(Object value, StringBuilder out) {
        if (value == null) {
            out.append("null");
        } else if (value instanceof String string) {
            writeString(string, out);
        } else if (value instanceof Boolean || value instanceof Integer || value instanceof Long
                || value instanceof Short || value instanceof Byte) {
            out.append(value);
        } else if (value instanceof Number number) {
            double doubleValue = number.doubleValue();
            if (!Double.isFinite(doubleValue)) {
                throw new IllegalArgumentException("non-finite JSON number");
            }
            out.append(number);
        } else if (value instanceof Map<?, ?> map) {
            out.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                if (!(entry.getKey() instanceof String key)) {
                    throw new IllegalArgumentException("JSON object keys must be strings");
                }
                if (!first) {
                    out.append(',');
                }
                first = false;
                writeString(key, out);
                out.append(':');
                write(entry.getValue(), out);
            }
            out.append('}');
        } else if (value instanceof Iterable<?> iterable) {
            out.append('[');
            boolean first = true;
            for (Object element : iterable) {
                if (!first) {
                    out.append(',');
                }
                first = false;
                write(element, out);
            }
            out.append(']');
        } else {
            throw new IllegalArgumentException("unsupported JSON value: " + value.getClass().getName());
        }
    }

    private static void writeString(String value, StringBuilder out) {
        out.append('"');
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\b' -> out.append("\\b");
                case '\f' -> out.append("\\f");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                default -> {
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
                }
            }
        }
        out.append('"');
    }

    private static final class Parser {
        private final String input;
        private int position;

        private Parser(String input) {
            if (input == null) {
                throw new IllegalArgumentException("JSON is null");
            }
            this.input = input;
        }

        private Object parse() {
            skipWhitespace();
            Object result = parseValue(0);
            skipWhitespace();
            if (position != input.length()) {
                throw error("trailing JSON data");
            }
            return result;
        }

        private Object parseValue(int depth) {
            if (depth > 32) {
                throw error("JSON nesting limit exceeded");
            }
            skipWhitespace();
            if (position >= input.length()) {
                throw error("unexpected end of JSON");
            }
            return switch (input.charAt(position)) {
                case '{' -> parseObject(depth + 1);
                case '[' -> parseArray(depth + 1);
                case '"' -> parseString();
                case 't' -> parseLiteral("true", Boolean.TRUE);
                case 'f' -> parseLiteral("false", Boolean.FALSE);
                case 'n' -> parseLiteral("null", null);
                default -> parseNumber();
            };
        }

        private Map<String, Object> parseObject(int depth) {
            expect('{');
            Map<String, Object> result = new LinkedHashMap<>();
            skipWhitespace();
            if (take('}')) {
                return result;
            }
            while (true) {
                skipWhitespace();
                if (position >= input.length() || input.charAt(position) != '"') {
                    throw error("object key required");
                }
                String key = parseString();
                if (result.containsKey(key)) {
                    throw error("duplicate object key");
                }
                skipWhitespace();
                expect(':');
                Object value = parseValue(depth);
                result.put(key, value);
                skipWhitespace();
                if (take('}')) {
                    return result;
                }
                expect(',');
            }
        }

        private List<Object> parseArray(int depth) {
            expect('[');
            List<Object> result = new ArrayList<>();
            skipWhitespace();
            if (take(']')) {
                return result;
            }
            while (true) {
                result.add(parseValue(depth));
                skipWhitespace();
                if (take(']')) {
                    return result;
                }
                expect(',');
            }
        }

        private String parseString() {
            expect('"');
            StringBuilder result = new StringBuilder();
            while (position < input.length()) {
                char c = input.charAt(position++);
                if (c == '"') {
                    return result.toString();
                }
                if (c == '\\') {
                    if (position >= input.length()) {
                        throw error("unterminated escape");
                    }
                    char escaped = input.charAt(position++);
                    switch (escaped) {
                        case '"', '\\', '/' -> result.append(escaped);
                        case 'b' -> result.append('\b');
                        case 'f' -> result.append('\f');
                        case 'n' -> result.append('\n');
                        case 'r' -> result.append('\r');
                        case 't' -> result.append('\t');
                        case 'u' -> result.append(parseUnicodeEscape());
                        default -> throw error("invalid string escape");
                    }
                } else {
                    if (c < 0x20) {
                        throw error("control character in string");
                    }
                    result.append(c);
                }
            }
            throw error("unterminated string");
        }

        private char parseUnicodeEscape() {
            if (position + 4 > input.length()) {
                throw error("short unicode escape");
            }
            int value = 0;
            for (int i = 0; i < 4; i++) {
                int digit = Character.digit(input.charAt(position++), 16);
                if (digit < 0) {
                    throw error("invalid unicode escape");
                }
                value = (value << 4) | digit;
            }
            return (char) value;
        }

        private Object parseLiteral(String literal, Object value) {
            if (!input.startsWith(literal, position)) {
                throw error("invalid JSON literal");
            }
            position += literal.length();
            return value;
        }

        private Number parseNumber() {
            int start = position;
            if (take('-')) {
                // sign consumed
            }
            if (take('0')) {
                // zero is complete before a decimal/exponent
            } else {
                requireDigits();
            }
            boolean fractional = false;
            if (take('.')) {
                fractional = true;
                requireDigits();
            }
            if (position < input.length() && (input.charAt(position) == 'e' || input.charAt(position) == 'E')) {
                fractional = true;
                position++;
                if (position < input.length() && (input.charAt(position) == '+' || input.charAt(position) == '-')) {
                    position++;
                }
                requireDigits();
            }
            String token = input.substring(start, position);
            try {
                if (!fractional) {
                    return Long.parseLong(token);
                }
                double value = Double.parseDouble(token);
                if (!Double.isFinite(value)) {
                    throw error("non-finite JSON number");
                }
                return value;
            } catch (NumberFormatException e) {
                throw error("invalid JSON number");
            }
        }

        private void requireDigits() {
            int start = position;
            while (position < input.length() && Character.isDigit(input.charAt(position))) {
                position++;
            }
            if (start == position) {
                throw error("JSON digits required");
            }
        }

        private void skipWhitespace() {
            while (position < input.length() && Character.isWhitespace(input.charAt(position))) {
                position++;
            }
        }

        private boolean take(char expected) {
            if (position < input.length() && input.charAt(position) == expected) {
                position++;
                return true;
            }
            return false;
        }

        private void expect(char expected) {
            if (!take(expected)) {
                throw error("expected '" + expected + "'");
            }
        }

        private IllegalArgumentException error(String message) {
            return new IllegalArgumentException(message + " at JSON offset " + position);
        }
    }
}
