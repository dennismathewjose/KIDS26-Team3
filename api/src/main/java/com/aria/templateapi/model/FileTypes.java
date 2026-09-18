package com.aria.templateapi.model;

import java.util.Arrays;
import java.util.Locale;

public enum FileTypes {
    AMG_ARIA_GUIDE("ARIA_Guide_AMG"),
    ResourceMaster("ResourceMaster");

    private final String displayName;

    FileTypes(String displayName) {
        this.displayName = displayName;
    }

    public boolean isContainedIn(String fileName) {
        return fileName.toLowerCase(Locale.ROOT).contains(displayName.toLowerCase(Locale.ROOT));
    }

    public static boolean contains(String fileName) {
        return Arrays.stream(values()).anyMatch(fileType -> fileType.isContainedIn(fileName));
    }
}