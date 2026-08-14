package bms.player.beatoraja.ir;

public interface IRResponse<T> {
    boolean isSucceeded();
    String getMessage();
    T getData();
}
