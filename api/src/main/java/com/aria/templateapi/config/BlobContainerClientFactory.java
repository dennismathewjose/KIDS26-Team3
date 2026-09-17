package com.aria.templateapi.config;

import com.aria.templateapi.exception.ContainerNotConfiguredException;
import com.azure.identity.DefaultAzureCredentialBuilder;
import com.azure.storage.blob.BlobContainerClient;
import com.azure.storage.blob.BlobContainerClientBuilder;
import com.azure.storage.common.StorageSharedKeyCredential;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * Builds one {@link BlobContainerClient} per configured container, authenticating with the storage account key.
 */
@Component
public class BlobContainerClientFactory {

    private static final Logger log = LoggerFactory.getLogger(BlobContainerClientFactory.class);

    private final Map<String, ResolvedContainer> byAlias = new LinkedHashMap<>();
    private final Map<String, String> aliasByContainerName = new HashMap<>();
    private final String defaultAlias;

    public BlobContainerClientFactory(AzureStorageProperties properties) {
        if (properties.getContainers().isEmpty()) {
            throw new IllegalStateException("Configure at least one container under azure.storage.containers.*");
        }

        properties.getContainers().forEach((alias, config) -> {
            String key = alias.toLowerCase(Locale.ROOT);
            ResolvedContainer resolved = build(key, config, properties);
            byAlias.put(key, resolved);
            aliasByContainerName.put(resolved.containerName().toLowerCase(Locale.ROOT), key);
            log.info("Registered container alias '{}' -> '{}' (auth={}, readOnly={})",
                    key, resolved.containerName(), resolved.authMode(), resolved.readOnly());
        });

        String configuredDefault = properties.getDefaultContainer();
        this.defaultAlias = StringUtils.hasText(configuredDefault)
                ? resolveAlias(configuredDefault)
                : byAlias.keySet().iterator().next();
    }

    public Set<String> aliases() {
        return byAlias.keySet();
    }

    public ResolvedContainer get(String containerOrAlias) {
        String alias = StringUtils.hasText(containerOrAlias) ? resolveAlias(containerOrAlias) : defaultAlias;
        return byAlias.get(alias);
    }

    private String resolveAlias(String containerOrAlias) {
        String key = containerOrAlias.toLowerCase(Locale.ROOT);
        if (byAlias.containsKey(key)) {
            return key;
        }
        String aliasForName = aliasByContainerName.get(key);
        if (aliasForName != null) {
            return aliasForName;
        }
        throw new ContainerNotConfiguredException(
                "Unknown container '" + containerOrAlias + "'. Configured: " + byAlias.keySet());
    }

    private static ResolvedContainer build(String alias, AzureStorageProperties.ContainerConfig config,
                                           AzureStorageProperties properties) {
        String containerName = StringUtils.hasText(config.getName()) ? config.getName() : alias;
        String accountName = firstNonBlank(config.getAccountName(), properties.getAccountName());
        String accountKey = firstNonBlank(config.getAccountKey(), properties.getAccountKey());
        String connectionString = firstNonBlank(config.getConnectionString(), properties.getConnectionString());
        String endpoint = resolveEndpoint(firstNonBlank(config.getEndpoint(), properties.getEndpoint()), accountName);

        BlobContainerClientBuilder builder = new BlobContainerClientBuilder();
        String authMode;

        if (StringUtils.hasText(accountKey)) {
            if (!StringUtils.hasText(accountName)) {
                throw new IllegalStateException("An account key is configured for container '" + alias
                        + "' but no account name. Set azure.storage.account-name or azure.storage.containers."
                        + alias + ".account-name");
            }
            requireEndpoint(alias, endpoint);
            builder.endpoint(endpoint).credential(new StorageSharedKeyCredential(accountName, accountKey));
            authMode = "shared-key";
        } else if (StringUtils.hasText(connectionString)) {
            builder.connectionString(connectionString);
            authMode = "connection-string";
        } else {
            requireEndpoint(alias, endpoint);
            builder.endpoint(endpoint).credential(new DefaultAzureCredentialBuilder().build());
            authMode = "default-azure-credential";
        }

        // Set last: endpoint() overwrites the container name when the URL carries a path.
        builder.containerName(containerName);

        return new ResolvedContainer(alias, containerName, builder.buildClient(), config.isReadOnly(),
                config.isAutoTimestampFolder(), authMode);
    }

    private static String resolveEndpoint(String configured, String accountName) {
        if (StringUtils.hasText(configured)) {
            return configured;
        }
        return StringUtils.hasText(accountName) ? "https://" + accountName + ".blob.core.windows.net" : null;
    }

    private static void requireEndpoint(String alias, String endpoint) {
        if (!StringUtils.hasText(endpoint)) {
            throw new IllegalStateException("No endpoint configured for container '" + alias
                    + "'. Set azure.storage.endpoint or azure.storage.account-name");
        }
    }

    private static String firstNonBlank(String first, String second) {
        return StringUtils.hasText(first) ? first : second;
    }

    public record ResolvedContainer(String alias, String containerName, BlobContainerClient client, boolean readOnly,
                                    boolean autoTimestampFolder, String authMode) {
    }
}
