package gov.rivermont.permits.licensing.config;

import java.time.Clock;
import java.time.ZoneId;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * The clock is a bean so expiry-window behaviour can be tested at a fixed date rather than
 * by waiting for one to arrive.
 */
@Configuration
public class ClockConfig {

    /** The department's local calendar, which is what a licence expiry date means. */
    static final ZoneId DEPARTMENT_ZONE = ZoneId.of("America/New_York");

    @Bean
    public Clock clock() {
        return Clock.system(DEPARTMENT_ZONE);
    }
}
