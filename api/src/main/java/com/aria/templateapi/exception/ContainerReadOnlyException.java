package com.aria.templateapi.exception;

public class ContainerReadOnlyException extends RuntimeException {

    public ContainerReadOnlyException(String message) {
        super(message);
    }
}
