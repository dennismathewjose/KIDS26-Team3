package com.aria.templateapi.model;

import java.time.OffsetDateTime;

/**
 * A single entry inside a container listing. Folders are virtual directory prefixes.
 */
public record BlobEntry(
        String name,
        String path,
        EntryType type,
        Long sizeInBytes,
        String contentType,
        String eTag,
        OffsetDateTime lastModified) {

    public enum EntryType {
        FOLDER,
        FILE
    }

    public static BlobEntry folder(String path) {
        String trimmed = path.endsWith("/") ? path.substring(0, path.length() - 1) : path;
        String name = trimmed.contains("/") ? trimmed.substring(trimmed.lastIndexOf('/') + 1) : trimmed;
        return new BlobEntry(name, path, EntryType.FOLDER, null, null, null, null);
    }
}
