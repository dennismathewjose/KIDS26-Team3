package com.aria.templateapi.model;

import java.util.List;

public record BlobListing(String container, String containerName, String prefix, List<BlobEntry> folders,
                          List<BlobEntry> files) {
}
