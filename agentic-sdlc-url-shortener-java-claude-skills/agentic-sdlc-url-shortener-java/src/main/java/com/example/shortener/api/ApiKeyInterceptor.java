package com.example.shortener.api;
import com.example.shortener.config.ShortenerProperties; import com.example.shortener.error.AppException; import com.example.shortener.service.TokenBucketRateLimiter;
import jakarta.servlet.http.*; import org.springframework.http.HttpStatus; import org.springframework.stereotype.Component; import org.springframework.web.servlet.HandlerInterceptor;
import java.nio.charset.StandardCharsets; import java.security.*;
@Component
public class ApiKeyInterceptor implements HandlerInterceptor {
  private final ShortenerProperties p; private final TokenBucketRateLimiter limiter;
  public ApiKeyInterceptor(ShortenerProperties p,TokenBucketRateLimiter l){this.p=p;this.limiter=l;}
  public boolean preHandle(HttpServletRequest req,HttpServletResponse res,Object handler){
    String key=req.getHeader("X-API-Key"); String identity=key==null?"ip:"+req.getRemoteAddr():"key:"+hash(key); double cost="POST".equals(req.getMethod())?2:1;
    var result=limiter.allow(identity,cost);if(!result.allowed()){res.setHeader("Retry-After",String.valueOf(result.retryAfterSeconds()));throw new AppException(HttpStatus.TOO_MANY_REQUESTS,"rate_limited","too many requests");}
    if(p.requireAuth() && !constantTimeMember(key))throw new AppException(HttpStatus.UNAUTHORIZED,"authentication_error","missing or invalid API key");
    req.setAttribute("principal",key==null?null:hash(key)); return true;
  }
  private boolean constantTimeMember(String key){if(key==null)return false;byte[] candidate=key.getBytes(StandardCharsets.UTF_8);boolean ok=false;for(String allowed:p.apiKeySet())ok|=MessageDigest.isEqual(candidate,allowed.getBytes(StandardCharsets.UTF_8));return ok;}
  private String hash(String s){try{return java.util.HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8))).substring(0,16);}catch(Exception e){throw new IllegalStateException(e);}}
}
