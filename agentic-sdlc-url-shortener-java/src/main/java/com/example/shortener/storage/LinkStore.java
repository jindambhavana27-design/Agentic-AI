package com.example.shortener.storage;
import com.example.shortener.domain.*;
import java.time.Instant;
import java.util.*;
public interface LinkStore extends AutoCloseable {
  void create(Link link); Optional<Link> get(String code); Optional<Link> findByIdempotencyKey(String key,String owner);
  Page list(int limit,String cursor); boolean softDelete(String code, Instant when); void record(ClickEvent event); LinkStats stats(String code,int days);
  record Page(List<Link> items,String nextCursor){} default void close(){}
}
