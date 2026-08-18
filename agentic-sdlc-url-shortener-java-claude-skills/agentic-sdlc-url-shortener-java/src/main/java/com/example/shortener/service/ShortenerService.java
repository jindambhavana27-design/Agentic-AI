package com.example.shortener.service;

import com.example.shortener.config.ShortenerProperties; import com.example.shortener.domain.*; import com.example.shortener.error.AppException; import com.example.shortener.storage.LinkStore; import com.example.shortener.validation.UrlValidator;
import org.springframework.http.HttpStatus; import org.springframework.stereotype.Service;
import java.security.SecureRandom; import java.time.*; import java.util.*;

@Service
public class ShortenerService {
  private static final char[] ALPHABET="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz".toCharArray();
  private final LinkStore store; private final ShortenerProperties p; private final UrlValidator validator; private final SecureRandom random=new SecureRandom();
  public ShortenerService(LinkStore store,ShortenerProperties p,UrlValidator validator){this.store=store;this.p=p;this.validator=validator;}
  public Result create(String raw,String alias,Long ttl,Map<String,Object> metadata,String owner,String idem){
    String target=validator.target(raw); long seconds=validator.ttl(ttl!=null?ttl:p.defaultTtlSeconds()); Map<String,Object> meta=validateMetadata(metadata);
    if(idem!=null){Optional<Link> old=store.findByIdempotencyKey(idem,owner);if(old.isPresent()){if(!old.get().targetUrl().equals(target))throw new AppException(HttpStatus.CONFLICT,"idempotency_conflict","idempotency key was already used with a different url");return new Result(old.get(),false);}}
    Instant now=Instant.now(),expires=seconds==0?null:now.plusSeconds(seconds);
    if(alias!=null){String code=validator.alias(alias);Link l=new Link(code,target,now,expires,null,owner,idem,true,meta);try{store.create(l);}catch(AppException e){throw new AppException(HttpStatus.CONFLICT,"alias_taken","alias is already taken",Map.of("alias",code));}return new Result(l,true);}
    for(int i=0;i<12;i++){String code=code();Link l=new Link(code,target,now,expires,null,owner,idem,false,meta);try{store.create(l);return new Result(l,true);}catch(AppException e){if(idem!=null){Optional<Link> old=store.findByIdempotencyKey(idem,owner);if(old.isPresent())return new Result(old.get(),false);}}}
    throw new AppException(HttpStatus.SERVICE_UNAVAILABLE,"code_exhaustion","could not allocate a free short code");
  }
  public Link resolve(String code,String referrer,String ua){Link l=require(code); if(l.deleted())throw gone("short link has been deleted"); if(l.expired())throw gone("short link has expired"); if(p.analyticsEnabled())store.record(new ClickEvent(code,Instant.now(),referrer,ua)); return l;}
  public Link get(String code){Link l=require(code);if(l.deleted())throw gone("short link has been deleted");return l;}
  public LinkStats stats(String code,int days){get(code);return store.stats(code,days);}
  public LinkStore.Page list(int limit,String cursor){if(limit<1||limit>200)throw new AppException(HttpStatus.BAD_REQUEST,"validation_error","limit must be between 1 and 200");return store.list(limit,cursor);}
  public void delete(String code){Link l=require(code);if(l.deleted())throw gone("short link has already been deleted");if(!store.softDelete(code,Instant.now()))throw new AppException(HttpStatus.CONFLICT,"conflict","delete failed");}
  private Link require(String c){return store.get(c).orElseThrow(()->new AppException(HttpStatus.NOT_FOUND,"not_found","no such short link",Map.of("code",c)));}
  private AppException gone(String m){return new AppException(HttpStatus.GONE,"gone",m);}
  private String code(){StringBuilder b=new StringBuilder();for(int i=0;i<p.codeLength();i++)b.append(ALPHABET[random.nextInt(ALPHABET.length)]);return b.toString();}
  private Map<String,Object> validateMetadata(Map<String,Object> m){if(m==null)return Map.of();if(m.size()>16)throw new AppException(HttpStatus.BAD_REQUEST,"validation_error","metadata may not exceed 16 keys");Map<String,Object> copy=new LinkedHashMap<>();m.forEach((k,v)->{if(k==null||k.isBlank()||!(v instanceof String||v instanceof Number||v instanceof Boolean))throw new AppException(HttpStatus.BAD_REQUEST,"validation_error","metadata values must be scalars");if(v instanceof String s&&s.length()>256)throw new AppException(HttpStatus.BAD_REQUEST,"validation_error","metadata value exceeds 256 characters");copy.put(k,v);});return Map.copyOf(copy);}
  public record Result(Link link,boolean created){}
}
