package com.aria.templateapi.controller;

import com.aria.templateapi.model.BatchUploadResult;
import com.aria.templateapi.model.BlobEntry;
import com.aria.templateapi.model.BlobFolderContents;
import com.aria.templateapi.model.ContainerInfo;
import com.aria.templateapi.model.FileTypes;
import com.aria.templateapi.service.BlobStorageService;
import com.aria.templateapi.service.BlobStorageService.UploadDestination;
import com.aria.templateapi.service.ExternalJobService;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.util.StringUtils;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/storage")
@Validated
public class BlobStorageController {

    private static final int MIN_BATCH_FILES = 2;
    private static final String GENERATED_TEMPLATE_SOURCE = "Data NBL Template for Biohackathon 1 SEP 2026_KS.xlsx";
    private static final String EXCEL_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

    private final BlobStorageService blobStorageService;
    private final ExternalJobService externalJobService;

    public BlobStorageController(BlobStorageService blobStorageService, ExternalJobService externalJobService) {
        this.blobStorageService = blobStorageService;
        this.externalJobService = externalJobService;
    }

    @GetMapping("/heartbeat")
    public Map<String, Object> heartbeat() {
        return Map.of("message", "Server UP", "currentDateTime", OffsetDateTime.now());
    }

    @GetMapping("/folders/contents")
    public List<BlobFolderContents> listFolderContents() {
        return blobStorageService.listFolderContents("input");
    }

    @PostMapping(path = "/items/batch", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<BatchUploadResult> uploadBatch(
            @RequestParam(required = false, defaultValue = "") String folder,
            @RequestParam(defaultValue = "true") boolean overwrite,
            @RequestPart("files") MultipartFile[] files) {

        if (files == null || files.length < MIN_BATCH_FILES) {
            throw new IllegalArgumentException(
                    "At least " + MIN_BATCH_FILES + " files are required; received "
                            + (files == null ? 0 : files.length));
        }

        ContainerInfo target = blobStorageService.describe("input");
        UploadDestination destination = blobStorageService.resolveUploadDestination(target.alias(), folder);
        String prefix = destination.prefix();
        blobStorageService.createFolder("output", prefix);

        List<BlobEntry> uploaded = new ArrayList<>();
        List<BatchUploadResult.FailedUpload> failed = new ArrayList<>();
        BlobEntry resourceMaster = null;

        for (MultipartFile file : files) {
            String fileName = leafName(file.getOriginalFilename());
            if (!StringUtils.hasText(fileName)) {
                failed.add(new BatchUploadResult.FailedUpload("(unnamed)", "Missing file name"));
                continue;
            }
            if (!FileTypes.contains(fileName)) {
                failed.add(new BatchUploadResult.FailedUpload(fileName, "Unsupported file type"));
                continue;
            }
            if (file.isEmpty()) {
                failed.add(new BatchUploadResult.FailedUpload(fileName, "File is empty"));
                continue;
            }
            try (var in = file.getInputStream()) {
                String targetPath = prefix + timestampFileName(fileName, destination.timestampMillis());
                BlobEntry uploadedEntry = blobStorageService.upload(target.alias(), targetPath, in,
                        file.getContentType(), overwrite);
                uploaded.add(uploadedEntry);
                if (FileTypes.ResourceMaster.isContainedIn(fileName)) {
                    resourceMaster = uploadedEntry;
                }
            } catch (IOException | RuntimeException ex) {
                failed.add(new BatchUploadResult.FailedUpload(fileName, ex.getMessage()));
            }
        }

        if (failed.isEmpty() && resourceMaster != null) {
            long timestamp = destination.timestampMillis() != null
                    ? destination.timestampMillis() : System.currentTimeMillis();
            try (var template = Files.newInputStream(Path.of(GENERATED_TEMPLATE_SOURCE))) {
            blobStorageService.upload("output", prefix + timestampFileName(GENERATED_TEMPLATE_SOURCE, timestamp),
                template, EXCEL_CONTENT_TYPE, overwrite);
            } catch (IOException | RuntimeException ex) {
                failed.add(new BatchUploadResult.FailedUpload("aria generated template", ex.getMessage()));
            }
        }
        /* 
        if (failed.isEmpty()) {
            try {
                List<String> inputUrls = uploaded.stream()
                        .map(entry -> blobStorageService.blobUrl(target.alias(), entry.path()))
                        .toList();
                externalJobService.submit(jobId(prefix), inputUrls,
                        blobStorageService.blobUrl("output", prefix));
            } catch (RuntimeException ex) {
                failed.add(new BatchUploadResult.FailedUpload("external job", ex.getMessage()));
            }
        }

        */
        
        BatchUploadResult result = new BatchUploadResult(target.alias(), target.containerName(), prefix,
                files.length, uploaded, failed);
        /*
        if (failed.isEmpty()) {
            try {
                externalJobService.notifyUpload(result);
            } catch (RuntimeException ex) {
                failed.add(new BatchUploadResult.FailedUpload("email notification", ex.getMessage()));
                result = new BatchUploadResult(target.alias(), target.containerName(), prefix,
                        files.length, uploaded, failed);
            }
        }
        */
        return ResponseEntity.status(failed.isEmpty() ? HttpStatus.CREATED : HttpStatus.MULTI_STATUS).body(result);
    }

    /** Browsers send the full client path in some cases; only the file name is meaningful here. */
    private static String leafName(String fileName) {
        if (fileName == null) {
            return null;
        }
        String normalised = fileName.replace('\\', '/');
        return normalised.contains("/") ? normalised.substring(normalised.lastIndexOf('/') + 1) : normalised;
    }

    private static String jobId(String prefix) {
        String trimmed = prefix.endsWith("/") ? prefix.substring(0, prefix.length() - 1) : prefix;
        int separator = trimmed.lastIndexOf('/');
        return separator >= 0 ? trimmed.substring(separator + 1) : trimmed;
    }

    private static String timestampFileName(String path, Long timestampMillis) {
        if (timestampMillis == null) {
            return path;
        }
        int separator = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
        String directory = separator >= 0 ? path.substring(0, separator + 1) : "";
        String fileName = separator >= 0 ? path.substring(separator + 1) : path;
        int extensionIndex = fileName.lastIndexOf('.');
        String extension = extensionIndex > 0 ? fileName.substring(extensionIndex) : "";
        String baseName = extensionIndex > 0 ? fileName.substring(0, extensionIndex) : fileName;
        baseName = baseName.replaceFirst("[-_]\\d+$", "");
        return directory + baseName + "_" + timestampMillis + extension;
    }
}
