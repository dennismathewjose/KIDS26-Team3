package com.aria.templateapi.exception;

import com.azure.storage.blob.models.BlobStorageException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ProblemDetail;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class GlobalExceptionHandler {

    @ExceptionHandler(BlobNotFoundException.class)
    ProblemDetail handleNotFound(BlobNotFoundException ex) {
        return problem(HttpStatus.NOT_FOUND, ex.getMessage());
    }

    @ExceptionHandler(InvalidPathException.class)
    ProblemDetail handleInvalidPath(InvalidPathException ex) {
        return problem(HttpStatus.BAD_REQUEST, ex.getMessage());
    }

    @ExceptionHandler(IllegalArgumentException.class)
    ProblemDetail handleIllegalArgument(IllegalArgumentException ex) {
        return problem(HttpStatus.BAD_REQUEST, ex.getMessage());
    }

    @ExceptionHandler(ContainerNotConfiguredException.class)
    ProblemDetail handleUnknownContainer(ContainerNotConfiguredException ex) {
        return problem(HttpStatus.BAD_REQUEST, ex.getMessage());
    }

    @ExceptionHandler(ContainerReadOnlyException.class)
    ProblemDetail handleReadOnly(ContainerReadOnlyException ex) {
        return problem(HttpStatus.FORBIDDEN, ex.getMessage());
    }

    @ExceptionHandler(IllegalStateException.class)
    ProblemDetail handleConflict(IllegalStateException ex) {
        return problem(HttpStatus.CONFLICT, ex.getMessage());
    }

    @ExceptionHandler(BlobStorageException.class)
    ProblemDetail handleStorage(BlobStorageException ex) {
        HttpStatus status = HttpStatus.resolve(ex.getStatusCode());
        return problem(status != null ? status : HttpStatus.BAD_GATEWAY, ex.getServiceMessage());
    }

    private static ProblemDetail problem(HttpStatus status, String detail) {
        ProblemDetail problemDetail = ProblemDetail.forStatus(status);
        problemDetail.setTitle(status.getReasonPhrase());
        problemDetail.setDetail(detail);
        return problemDetail;
    }
}
