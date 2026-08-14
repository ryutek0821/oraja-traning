package bms.player.beatoraja.ir;

final class SimpleIRResponse<T> implements IRResponse<T> {
    private final boolean succeeded; private final String message; private final T data;
    SimpleIRResponse(boolean succeeded, String message, T data) {
        this.succeeded = succeeded; this.message = message == null ? "" : message; this.data = data;
    }
    public boolean isSucceeded() { return succeeded; }
    public String getMessage() { return message; }
    public T getData() { return data; }
}
