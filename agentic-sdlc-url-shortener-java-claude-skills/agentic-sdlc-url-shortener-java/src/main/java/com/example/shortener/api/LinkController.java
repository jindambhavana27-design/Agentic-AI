package com.example.shortener.api;

import com.example.shortener.api.ApiDtos.*; import com.example.shortener.config.ShortenerProperties; import com.example.shortener.domain.*; import com.example.shortener.service.ShortenerService;
import jakarta.servlet.http.HttpServletRequest; import jakarta.validation.Valid; import org.springframework.http.*; import org.springframework.web.bind.annotation.*;
import java.net.URI; import java.util.*;

@RestController
public class LinkController {
  private final ShortenerService service; private final ShortenerProperties p;
  public LinkController(ShortenerService service,ShortenerProperties p){this.service=service;this.p=p;}
  @PostMapping("/api/v1/links") public ResponseEntity<LinkResponse> create(@Valid @RequestBody CreateLinkRequest body,@RequestHeader(value="Idempotency-Key",required=false)String idem,HttpServletRequest req){var r=service.create(body.url(),body.alias(),body.ttlSeconds(),body.metadata(),(String)req.getAttribute("principal"),idem);return ResponseEntity.status(r.created()?201:200).body(dto(r.link()));}
  @GetMapping("/api/v1/links") public PageResponse list(@RequestParam(defaultValue="50")int limit,@RequestParam(required=false)String cursor){var page=service.list(limit,cursor);return new PageResponse(page.items().stream().map(this::dto).toList(),page.nextCursor());}
  @GetMapping("/api/v1/links/{code}") public LinkResponse get(@PathVariable("code") String code){return dto(service.get(code));}
  @GetMapping("/api/v1/links/{code}/stats") public LinkStats stats(@PathVariable("code") String code,@RequestParam(defaultValue="7d")String window){return service.stats(code,parseDays(window));}
  @DeleteMapping("/api/v1/links/{code}") @ResponseStatus(HttpStatus.NO_CONTENT) public void delete(@PathVariable("code") String code){service.delete(code);}
  @GetMapping("/{code:[A-Za-z0-9_-]{1,64}}") public ResponseEntity<Void> redirect(@PathVariable("code") String code,@RequestHeader(value="Referer",required=false)String ref,@RequestHeader(value="User-Agent",required=false)String ua)
  {Link l=service.resolve(code,ref,ua);return ResponseEntity.status(p.redirectStatus()).location(URI.create(l.targetUrl())).header("Referrer-Policy","no-referrer").header("Cache-Control","private, max-age=0, no-store").build();}
  private LinkResponse dto(Link l){return new LinkResponse(l.code(),l.targetUrl(),p.baseUrl()+"/"+l.code(),l.createdAt(),l.expiresAt(),l.customAlias(),l.metadata());}
  private int parseDays(String w){try{if(w.endsWith("d"))return Integer.parseInt(w.substring(0,w.length()-1));if(w.endsWith("w"))return Integer.parseInt(w.substring(0,w.length()-1))*7;if(w.endsWith("h"))return Math.max(1,(int)Math.ceil(Integer.parseInt(w.substring(0,w.length()-1))/24d));}catch(Exception ignored){}return 7;}
}
