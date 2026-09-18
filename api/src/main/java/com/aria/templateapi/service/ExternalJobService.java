package com.aria.templateapi.service;

import com.aria.templateapi.config.ExternalJobProperties;
import com.aria.templateapi.model.BatchUploadResult;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;
import org.springframework.web.client.RestClient;

import java.net.URI;
import java.util.List;
import java.util.Map;

@Service
public class ExternalJobService {

    private static final Logger log = LoggerFactory.getLogger(ExternalJobService.class);

    private final ExternalJobProperties properties;
    private final RestClient restClient;

    public ExternalJobService(ExternalJobProperties properties) {
        this.properties = properties;
        this.restClient = RestClient.create();
    }

    public void submit(String jobId, List<String> inputUrls, String outputUrl) {
        if (!StringUtils.hasText(properties.getUrl()) || !StringUtils.hasText(properties.getApiKey())
                || !StringUtils.hasText(properties.getEmailUrl())) {
            throw new IllegalStateException("External job API is not configured");
        }

            JobRequest request = new JobRequest(jobId, inputUrls, outputUrl, properties.getEmailUrl(), Map.of());
            log.info("Calling external job API: url={}, request={}", properties.getUrl(), request);

        restClient.post()
                .uri(properties.getUrl())
                .contentType(MediaType.APPLICATION_JSON)
                .header("x-api-key", properties.getApiKey())
                .body(request)
                .retrieve()
                .toBodilessEntity();
    }

    public void notifyUpload(BatchUploadResult result) {
        if (!StringUtils.hasText(properties.getEmailUrl())) {
            throw new IllegalStateException("External job email URL is not configured");
        }

        log.info("Calling email webhook for upload result: container={}, folder={}, uploaded={}, failed={}",
                result.container(), result.folder(), result.uploaded().size(), result.failed().size());
        restClient.post()
            .uri(URI.create(properties.getEmailUrl()))
                .contentType(MediaType.APPLICATION_JSON)
                .body(result)
                .retrieve()
                .toBodilessEntity();
    }

    private record JobRequest(String job_id, List<String> input_urls, String output_url, String email_url,
                              Map<String, Object> additionalProp1) {
    }
}
