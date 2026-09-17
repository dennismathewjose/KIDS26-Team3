package com.aria.templateapi.model;

import java.util.List;

public record BatchUploadResult(String container, String containerName, String folder, int requested,
                                List<BlobEntry> uploaded, List<FailedUpload> failed) {

    public record FailedUpload(String fileName, String reason) {
    }
}
