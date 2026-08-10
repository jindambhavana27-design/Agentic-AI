package com.example.shortener.error;
import org.springframework.http.HttpStatus;
import java.util.Map;
public class AppException extends RuntimeException {
  private final HttpStatus status; private final String code; private final Map<String,Object> details;
  public AppException(HttpStatus status,String code,String message){this(status,code,message,Map.of());}
  public AppException(HttpStatus status,String code,String message,Map<String,Object> details){super(message);this.status=status;this.code=code;this.details=details;}
  public HttpStatus status(){return status;} public String code(){return code;} public Map<String,Object> details(){return details;}
}
