package com.aria.templateapi.service;

import com.aria.templateapi.exception.InvalidPathException;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class BlobStorageServiceTest {

    @Test
    void stripsLeadingSlashesAndNormalisesSeparators() {
        assertEquals("reports/2026/q1.pdf", BlobStorageService.sanitise("/reports\\2026/q1.pdf"));
    }

    @Test
    void rejectsTraversalSequences() {
        assertThrows(InvalidPathException.class, () -> BlobStorageService.sanitise("reports/../../secret.txt"));
        assertThrows(InvalidPathException.class, () -> BlobStorageService.sanitise("reports//secret.txt"));
        assertThrows(InvalidPathException.class, () -> BlobStorageService.sanitise("  "));
    }
}
