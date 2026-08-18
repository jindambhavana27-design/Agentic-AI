package com.example.shortener.api;
import com.example.shortener.api.ApiDtos.*; import com.example.shortener.error.AppException; import jakarta.servlet.http.*; import org.springframework.http.*; import org.springframework.web.bind.MethodArgumentNotValidException; import org.springframework.web.bind.annotation.*; import java.util.*;
@RestControllerAdvice
public class ApiExceptionHandler {
  @ExceptionHandler(AppException.class) ResponseEntity<ErrorBody> app(AppException e,HttpServletRequest r){return ResponseEntity.status(e.status()).body(body(e.code(),e.getMessage(),r,e.details()));}
  @ExceptionHandler(MethodArgumentNotValidException.class) ResponseEntity<ErrorBody> validation(MethodArgumentNotValidException e,HttpServletRequest r){return ResponseEntity.badRequest().body(body("validation_error",e.getBindingResult().getAllErrors().get(0).getDefaultMessage(),r,Map.of()));}
  @ExceptionHandler(Exception.class)
ResponseEntity<ErrorBody> unexpected(Exception e, HttpServletRequest r) {
    // Print the real exception so we can identify the redirect failure.
    e.printStackTrace();

    return ResponseEntity.internalServerError()
            .body(body("internal_error", "internal server error", r, Map.of()));
}
  private ErrorBody body(String c,String m,HttpServletRequest r,Map<String,Object>d){String id=Optional.ofNullable(r.getHeader("X-Request-Id")).filter(s->s.matches("[A-Za-z0-9._-]{1,64}")).orElse(UUID.randomUUID().toString());return new ErrorBody(new ErrorBody.ErrorValue(c,m,id,d));}
}
