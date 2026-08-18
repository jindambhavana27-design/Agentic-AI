package com.example.shortener.config;
import com.example.shortener.api.ApiKeyInterceptor; import org.springframework.context.annotation.Configuration; import org.springframework.web.servlet.config.annotation.*;
@Configuration public class WebConfig implements WebMvcConfigurer {private final ApiKeyInterceptor i;public WebConfig(ApiKeyInterceptor i){this.i=i;}public void addInterceptors(InterceptorRegistry r){r.addInterceptor(i).addPathPatterns("/api/v1/**");}}
