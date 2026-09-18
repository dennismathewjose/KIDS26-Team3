package com.aria.templateapi.service;

import com.aria.templateapi.config.BlobContainerClientFactory;
import com.aria.templateapi.config.BlobContainerClientFactory.ResolvedContainer;
import com.aria.templateapi.exception.BlobNotFoundException;
import com.aria.templateapi.exception.ContainerReadOnlyException;
import com.aria.templateapi.exception.InvalidPathException;
import com.aria.templateapi.model.BlobEntry;
import com.aria.templateapi.model.BlobFolderContents;
import com.aria.templateapi.model.BlobListing;
import com.aria.templateapi.model.ContainerInfo;
import com.azure.core.util.BinaryData;
import com.azure.storage.blob.BlobClient;
import com.azure.storage.blob.BlobContainerClient;
import com.azure.storage.blob.models.BlobHttpHeaders;
import com.azure.storage.blob.models.BlobItem;
import com.azure.storage.blob.models.BlobItemProperties;
import com.azure.storage.blob.models.BlobProperties;
import com.azure.storage.blob.models.DeleteSnapshotsOptionType;
import com.azure.storage.blob.models.ListBlobsOptions;
import com.azure.storage.blob.options.BlobParallelUploadOptions;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

import java.io.InputStream;
import java.io.OutputStream;
import java.time.Duration;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@Service
public class BlobStorageService {

    private static final String DELIMITER = "/";
    /** Azure rejects blob names ending in '/', so an empty folder is represented by a zero-byte marker blob. */
    private static final String FOLDER_MARKER = ".keep";
    private static final Duration TIMEOUT = Duration.ofSeconds(60);

    private final BlobContainerClientFactory containerFactory;

    public BlobStorageService(BlobContainerClientFactory containerFactory) {
        this.containerFactory = containerFactory;
    }

    public List<ContainerInfo> listContainers() {
        return containerFactory.aliases().stream()
                .map(this::describe)
                .toList();
    }

    public ContainerInfo describe(String container) {
        ResolvedContainer resolved = containerFactory.get(container);
        return new ContainerInfo(resolved.alias(), resolved.containerName(), resolved.readOnly(),
                resolved.authMode());
    }

    public UploadDestination resolveUploadDestination(String container, String folder) {
        ResolvedContainer resolved = containerFactory.get(container);
        String base = StringUtils.hasText(folder) ? normalisePrefix(folder) : "";
        if (!resolved.autoTimestampFolder()) {
            return new UploadDestination(base, null);
        }
        long timestampMillis = System.currentTimeMillis();
        String stamp = LocalDate.now(ZoneOffset.UTC).format(DateTimeFormatter.BASIC_ISO_DATE)
                + "_" + timestampMillis;
        return new UploadDestination(base + stamp + DELIMITER, timestampMillis);
    }

    public record UploadDestination(String prefix, Long timestampMillis) {
    }

    /**
     * Lists one level of the virtual folder hierarchy, or every blob under the prefix when recursive.
     */
    public BlobListing list(String container, String prefix, boolean recursive) {
        ResolvedContainer resolved = containerFactory.get(container);
        BlobContainerClient containerClient = resolved.client();
        String normalisedPrefix = normalisePrefix(prefix);

        ListBlobsOptions options = new ListBlobsOptions().setPrefix(normalisedPrefix);
        List<BlobEntry> folders = new ArrayList<>();
        List<BlobEntry> files = new ArrayList<>();

        Iterable<BlobItem> items = recursive
                ? containerClient.listBlobs(options, TIMEOUT)
                : containerClient.listBlobsByHierarchy(DELIMITER, options, TIMEOUT);

        for (BlobItem item : items) {
            if (Boolean.TRUE.equals(item.isPrefix())) {
                folders.add(BlobEntry.folder(item.getName()));
            } else if (!isFolderMarker(item)) {
                files.add(toFileEntry(item));
            }
        }

        return new BlobListing(resolved.alias(), resolved.containerName(), normalisedPrefix, folders, files);
    }

    public List<BlobFolderContents> listFolderContents(String container) {
        Map<String, FolderContents> inputFolders = folderContents(containerFactory.get(container).client());
        Map<String, FolderContents> outputFolders = folderContents(containerFactory.get("output").client());

        return inputFolders.entrySet().stream()
                .sorted(Map.Entry.<String, FolderContents>comparingByValue(
                        Comparator.comparing(FolderContents::lastModified,
                                Comparator.nullsLast(Comparator.naturalOrder())).reversed()))
                .map(entry -> new BlobFolderContents(entry.getKey(), entry.getValue().lastModified(), entry.getValue().files(),
                        outputFolders.getOrDefault(entry.getKey(), FolderContents.EMPTY).files()))
                .toList();
    }

    private Map<String, FolderContents> folderContents(BlobContainerClient containerClient) {
        Map<String, FolderContents> folders = new LinkedHashMap<>();
        for (BlobItem item : containerClient.listBlobs()) {
            if (!item.getName().contains(DELIMITER)) {
                continue;
            }

            String folderName = item.getName().substring(0, item.getName().indexOf(DELIMITER));
            BlobItemProperties properties = item.getProperties();
            FolderContents folder = folders.computeIfAbsent(folderName, ignored -> new FolderContents());
            folder.updateLastModified(properties != null ? properties.getLastModified() : null);
            if (!isFolderMarker(item)) {
                folder.files().add(new BlobFolderContents.FolderFile(
                        leafName(item.getName()),
                        containerClient.getBlobClient(item.getName()).getBlobUrl(),
                        properties != null ? properties.getCreationTime() : null));
            }
        }
        return folders;
    }

    private static final class FolderContents {
        private static final FolderContents EMPTY = new FolderContents();
        private final List<BlobFolderContents.FolderFile> files = new ArrayList<>();
        private OffsetDateTime lastModified;

        List<BlobFolderContents.FolderFile> files() {
            return files;
        }

        OffsetDateTime lastModified() {
            return lastModified;
        }

        void updateLastModified(OffsetDateTime candidate) {
            if (candidate != null && (lastModified == null || candidate.isAfter(lastModified))) {
                lastModified = candidate;
            }
        }
    }

    public BlobEntry upload(String container, String path, InputStream content, String contentType,
                            boolean overwrite) {
        BlobClient blobClient = writableBlobClient(container, path);

        if (!overwrite && Boolean.TRUE.equals(blobClient.exists())) {
            throw new IllegalStateException("Blob already exists: " + path);
        }

        BlobParallelUploadOptions uploadOptions = new BlobParallelUploadOptions(content)
                .setHeaders(new BlobHttpHeaders().setContentType(
                        StringUtils.hasText(contentType) ? contentType : "application/octet-stream"));

        blobClient.uploadWithResponse(uploadOptions, TIMEOUT, null);
        return toFileEntry(blobClient.getBlobName(), blobClient.getProperties());
    }

    public String blobUrl(String container, String path) {
        return blobClient(container, path).getBlobUrl();
    }

    public BlobEntry copy(String sourceContainer, String sourcePath, String destinationContainer, String destinationPath,
                          boolean overwrite) {
        BlobClient source = blobClient(sourceContainer, sourcePath);
        if (Boolean.FALSE.equals(source.exists())) {
            throw new BlobNotFoundException("Blob not found: " + sourcePath);
        }

        try (InputStream content = source.openInputStream()) {
            return upload(destinationContainer, destinationPath, content,
                    source.getProperties().getContentType(), overwrite);
        } catch (java.io.IOException ex) {
            throw new IllegalStateException("Could not copy blob: " + sourcePath, ex);
        }
    }

    public BlobEntry createFolder(String container, String folderPath) {
        ResolvedContainer resolved = requireWritable(container);
        String normalised = normalisePrefix(folderPath);
        if (!StringUtils.hasText(normalised)) {
            throw new InvalidPathException("Folder path must not be empty");
        }

        BlobClient blobClient = resolved.client().getBlobClient(normalised + FOLDER_MARKER);
        if (Boolean.FALSE.equals(blobClient.exists())) {
            blobClient.upload(BinaryData.fromBytes(new byte[0]), true);
        }
        return BlobEntry.folder(normalised);
    }

    public void download(String container, String path, OutputStream target) {
        BlobClient blobClient = blobClient(container, path);
        if (Boolean.FALSE.equals(blobClient.exists())) {
            throw new BlobNotFoundException("Blob not found: " + path);
        }
        blobClient.downloadStream(target);
    }

    public BlobEntry metadata(String container, String path) {
        BlobClient blobClient = blobClient(container, path);
        if (Boolean.FALSE.equals(blobClient.exists())) {
            throw new BlobNotFoundException("Blob not found: " + path);
        }
        return toFileEntry(blobClient.getBlobName(), blobClient.getProperties());
    }

    public boolean delete(String container, String path) {
        return Boolean.TRUE.equals(writableBlobClient(container, path)
                .deleteIfExistsWithResponse(DeleteSnapshotsOptionType.INCLUDE, null, TIMEOUT, null)
                .getValue());
    }

    /** Deletes every blob under the prefix, including the folder marker itself. */
    public int deleteFolder(String container, String folderPath) {
        ResolvedContainer resolved = requireWritable(container);
        String normalised = normalisePrefix(folderPath);
        if (!StringUtils.hasText(normalised)) {
            throw new InvalidPathException("Refusing to delete the container root");
        }

        BlobContainerClient containerClient = resolved.client();
        List<String> names = new ArrayList<>();
        for (BlobItem item : containerClient.listBlobs(new ListBlobsOptions().setPrefix(normalised), TIMEOUT)) {
            names.add(item.getName());
        }
        // On a hierarchical-namespace account a directory cannot be removed before its children.
        names.sort(Comparator.comparingInt((String n) -> n.split(DELIMITER).length).reversed());
        names.add(normalised.substring(0, normalised.length() - 1));

        int deleted = 0;
        for (String name : names) {
            if (Boolean.TRUE.equals(containerClient.getBlobClient(name).deleteIfExists())) {
                deleted++;
            }
        }
        return deleted;
    }

    private BlobClient blobClient(String container, String path) {
        return containerFactory.get(container).client().getBlobClient(sanitise(path));
    }

    private BlobClient writableBlobClient(String container, String path) {
        return requireWritable(container).client().getBlobClient(sanitise(path));
    }

    private ResolvedContainer requireWritable(String container) {
        ResolvedContainer resolved = containerFactory.get(container);
        if (resolved.readOnly()) {
            throw new ContainerReadOnlyException("Container '" + resolved.alias() + "' is configured read-only");
        }
        return resolved;
    }

    /**
     * Rejects traversal sequences and absolute paths so a caller cannot escape the container prefix.
     */
    static String sanitise(String path) {
        if (!StringUtils.hasText(path)) {
            throw new InvalidPathException("Path must not be empty");
        }
        String cleaned = path.replace('\\', '/').trim();
        while (cleaned.startsWith("/")) {
            cleaned = cleaned.substring(1);
        }
        if (cleaned.contains("\u0000") || cleaned.contains("//")) {
            throw new InvalidPathException("Illegal path: " + path);
        }
        for (String segment : cleaned.split("/")) {
            if (segment.equals("..") || segment.equals(".")) {
                throw new InvalidPathException("Illegal path: " + path);
            }
        }
        if (!StringUtils.hasText(cleaned)) {
            throw new InvalidPathException("Path must not be empty");
        }
        return cleaned;
    }

    private static String normalisePrefix(String prefix) {
        if (!StringUtils.hasText(prefix)) {
            return "";
        }
        String cleaned = sanitise(prefix);
        return cleaned.endsWith(DELIMITER) ? cleaned : cleaned + DELIMITER;
    }

    private static boolean isFolderMarker(BlobItem item) {
        BlobItemProperties props = item.getProperties();
        boolean empty = props == null || props.getContentLength() == null || props.getContentLength() == 0L;
        return empty && (item.getName().endsWith(DELIMITER) || leafName(item.getName()).equals(FOLDER_MARKER));
    }

    private static BlobEntry toFileEntry(BlobItem item) {
        BlobItemProperties props = item.getProperties();
        String name = item.getName();
        return new BlobEntry(
                leafName(name),
                name,
                BlobEntry.EntryType.FILE,
                props != null ? props.getContentLength() : null,
                props != null ? props.getContentType() : null,
                props != null ? props.getETag() : null,
                props != null ? props.getLastModified() : null);
    }

    private static BlobEntry toFileEntry(String name, BlobProperties props) {
        return new BlobEntry(
                leafName(name),
                name,
                BlobEntry.EntryType.FILE,
                props.getBlobSize(),
                props.getContentType(),
                props.getETag(),
                props.getLastModified());
    }

    private static String leafName(String name) {
        return name.contains(DELIMITER) ? name.substring(name.lastIndexOf('/') + 1) : name;
    }
}
