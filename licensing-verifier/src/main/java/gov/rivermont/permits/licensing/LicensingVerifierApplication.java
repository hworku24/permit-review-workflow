package gov.rivermont.permits.licensing;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * Owns the JDBC connection to the state contractor licensing replica.
 *
 * <p>The state grants each jurisdiction a read only account and a JDBC driver. Putting one
 * service in front of that means the credential lives in one place, the connection pool is
 * shared instead of every application process opening its own connection to somebody else's
 * database, and the timeout and degradation policy is enforced once.
 */
@SpringBootApplication
public class LicensingVerifierApplication {

    public static void main(String[] args) {
        SpringApplication.run(LicensingVerifierApplication.class, args);
    }
}
