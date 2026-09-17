FROM gradle:8.14-jdk21 AS build

WORKDIR /workspace
COPY gradle gradle
COPY gradlew gradlew
COPY gradlew.bat gradlew.bat
COPY settings.gradle build.gradle gradle.properties ./
RUN chmod +x gradlew && ./gradlew dependencies --no-daemon

COPY src src
RUN ./gradlew bootJar --no-daemon

FROM mcr.microsoft.com/openjdk/jdk:21-distroless

LABEL maintainer="Researchinformationservices@stjude.org"

WORKDIR /app
COPY --from=build /workspace/build/libs/*.jar app.jar

EXPOSE 80

ENTRYPOINT ["java", "-Djava.security.egd=file:/dev/./urandom", "-jar", "/app/app.jar"]