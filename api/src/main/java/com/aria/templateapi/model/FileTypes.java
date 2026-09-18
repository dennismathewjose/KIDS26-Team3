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

        public static boolean contains(String fileName) {
            return Arrays.stream(values()).anyMatch(fileType ->
                fileName.toLowerCase(Locale.ROOT).contains(fileType.displayName.toLowerCase(Locale.ROOT)));
    }
}