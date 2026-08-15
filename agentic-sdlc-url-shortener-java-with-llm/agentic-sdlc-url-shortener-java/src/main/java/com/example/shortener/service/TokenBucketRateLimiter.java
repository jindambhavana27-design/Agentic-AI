package com.example.shortener.service;
import org.springframework.stereotype.Component;
import com.example.shortener.config.ShortenerProperties;
import java.util.concurrent.ConcurrentHashMap;
@Component
public class TokenBucketRateLimiter {
  private record Bucket(double tokens,long nanos){}
  private final ConcurrentHashMap<String,Bucket> buckets=new ConcurrentHashMap<>(); private final ShortenerProperties p;
  public TokenBucketRateLimiter(ShortenerProperties p){this.p=p;}
  public synchronized Result allow(String identity,double cost){if(!p.rateLimitEnabled())return new Result(true,0); long now=System.nanoTime(); Bucket b=buckets.getOrDefault(identity,new Bucket(p.rateLimitCapacity(),now)); double tokens=Math.min(p.rateLimitCapacity(),b.tokens+(now-b.nanos)/1_000_000_000d*p.rateLimitRefillPerSecond()); if(tokens>=cost){buckets.put(identity,new Bucket(tokens-cost,now));return new Result(true,0);} double retry=(cost-tokens)/p.rateLimitRefillPerSecond(); buckets.put(identity,new Bucket(tokens,now));return new Result(false,Math.max(1,(int)Math.ceil(retry)));}
  public record Result(boolean allowed,int retryAfterSeconds){}
}
