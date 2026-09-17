package com.aria.templateapi.model;

import java.time.OffsetDateTime;
import java.util.List;

public record BlobFolderContents(String folderName, OffsetDateTime folderdate, List<FolderFile> inputfiles,
                                 List<FolderFile> outputfiles) {

    public record FolderFile(String fileName, String filePath, OffsetDateTime timeCreated) {
    }
}