package com.aria.templateapi;

import com.aria.templateapi.config.AzureStorageProperties;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.EnableConfigurationProperties;

@SpringBootApplication
@EnableConfigurationProperties(AzureStorageProperties.class)
public class AriaTemplateApiApplication {

    public static void main(String[] args) {
        SpringApplication.run(AriaTemplateApiApplication.class, args);
    }
}
