package com.aria.templateapi.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.util.LinkedHashMap;
import java.util.Map;

@ConfigurationProperties(prefix = "azure.storage")
public class AzureStorageProperties {

    /** Account endpoint, e.g. https://myaccount.blob.core.windows.net. Derived from accountName when omitted. */
    private String endpoint;

    /** Storage account name used to build the shared key credential. */
    private String accountName;

    /** Storage account access key. Supply via environment/Key Vault, never in source control. */
    private String accountKey;

    /** Account-level connection string; used only when no account key applies. */
    private String connectionString;

    /** Alias of the container used when a request does not name one. */
    private String defaultContainer;

    /** Container aliases (e.g. input, output, staging) mapped to their own name and optional account override. */
    private Map<String, ContainerConfig> containers = new LinkedHashMap<>();

    public String getEndpoint() {
        return endpoint;
    }

    public void setEndpoint(String endpoint) {
        this.endpoint = endpoint;
    }

    public String getAccountName() {
        return accountName;
    }

    public void setAccountName(String accountName) {
        this.accountName = accountName;
    }

    public String getAccountKey() {
        return accountKey;
    }

    public void setAccountKey(String accountKey) {
        this.accountKey = accountKey;
    }

    public String getConnectionString() {
        return connectionString;
    }

    public void setConnectionString(String connectionString) {
        this.connectionString = connectionString;
    }

    public String getDefaultContainer() {
        return defaultContainer;
    }

    public void setDefaultContainer(String defaultContainer) {
        this.defaultContainer = defaultContainer;
    }

    public Map<String, ContainerConfig> getContainers() {
        return containers;
    }

    public void setContainers(Map<String, ContainerConfig> containers) {
        this.containers = containers;
    }

    public static class ContainerConfig {

        /** Actual container name in the storage account; defaults to the alias when omitted. */
        private String name;

        /** Overrides the account endpoint when this container lives in a different account. */
        private String endpoint;

        /** Overrides the account name when this container lives in a different account. */
        private String accountName;

        /** Overrides the account key when this container lives in a different account. */
        private String accountKey;

        /** Overrides the account connection string. */
        private String connectionString;

        /** Reject upload, folder-create and delete requests for this container. */
        private boolean readOnly = false;

        /** Place every upload in a new yyyyMMdd_<epochMillis> folder. */
        private boolean autoTimestampFolder = false;

        public String getName() {
            return name;
        }

        public void setName(String name) {
            this.name = name;
        }

        public String getAccountName() {
            return accountName;
        }

        public void setAccountName(String accountName) {
            this.accountName = accountName;
        }

        public String getAccountKey() {
            return accountKey;
        }

        public void setAccountKey(String accountKey) {
            this.accountKey = accountKey;
        }

        public String getEndpoint() {
            return endpoint;
        }

        public void setEndpoint(String endpoint) {
            this.endpoint = endpoint;
        }

        public String getConnectionString() {
            return connectionString;
        }

        public void setConnectionString(String connectionString) {
            this.connectionString = connectionString;
        }

        public boolean isReadOnly() {
            return readOnly;
        }

        public void setReadOnly(boolean readOnly) {
            this.readOnly = readOnly;
        }

        public boolean isAutoTimestampFolder() {
            return autoTimestampFolder;
        }

        public void setAutoTimestampFolder(boolean autoTimestampFolder) {
            this.autoTimestampFolder = autoTimestampFolder;
        }
    }
}
