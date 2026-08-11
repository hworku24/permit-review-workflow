# The licensing verifier. Built in two stages so the runtime image carries a JRE and a jar
# and not a JDK and a Maven cache.

FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /build
COPY pom.xml .
# Dependencies first, so a source change does not re-download the world.
RUN mvn -B -q dependency:go-offline
COPY src ./src
RUN mvn -B -q package -DskipTests

FROM eclipse-temurin:17-jre-jammy AS runtime
ENV TZ=America/New_York
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && useradd --create-home --uid 10002 verifier
COPY --from=build /build/target/*.jar /app/licensing-verifier.jar
USER verifier
EXPOSE 8082
ENTRYPOINT ["java", "-jar", "/app/licensing-verifier.jar"]
