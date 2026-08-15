package com.example.shortener.validation;

import com.example.shortener.config.ShortenerProperties;
import com.example.shortener.error.AppException;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import java.net.*;
import java.util.regex.Pattern;

@Component
public class UrlValidator {
  private static final Pattern ALIAS=Pattern.compile("[A-Za-z0-9_-]{1,64}");
  private final ShortenerProperties p;
  public UrlValidator(ShortenerProperties p){this.p=p;}
  public String target(String raw){
    try {
      URI uri=URI.create(raw); String scheme=uri.getScheme(); String host=uri.getHost();
      if (!("http".equalsIgnoreCase(scheme)||"https".equalsIgnoreCase(scheme)) || host==null)
        throw bad("url must be an absolute http or https URL");
      if (uri.getUserInfo()!=null) throw bad("url must not include credentials");
      if (!p.allowPrivateHosts()) {
        InetAddress address=InetAddress.getByName(host);
        if (address.isAnyLocalAddress()||address.isLoopbackAddress()||address.isLinkLocalAddress()||address.isSiteLocalAddress()||address.isMulticastAddress())
          throw new AppException(HttpStatus.BAD_REQUEST,"unsafe_url","url host is not publicly routable");
      }
      return uri.normalize().toString();
    } catch (IllegalArgumentException|UnknownHostException e){ throw bad("invalid target URL"); }
  }
  public String alias(String value){ if(value==null||!ALIAS.matcher(value).matches()) throw bad("alias must contain 1-64 letters, digits, underscores, or hyphens"); return value; }
  public long ttl(Long value){ if(value==null) return 0; if(value<60||value>31_536_000L) throw bad("ttl_seconds must be between 60 and 31536000"); return value; }
  private AppException bad(String m){return new AppException(HttpStatus.BAD_REQUEST,"validation_error",m);}
}
