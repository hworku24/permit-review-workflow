package gov.rivermont.permits.licensing.repository;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import gov.rivermont.permits.licensing.domain.LicenseRecord;
import java.util.Optional;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.jdbc.AutoConfigureTestDatabase;
import org.springframework.boot.test.autoconfigure.jdbc.AutoConfigureTestDatabase.Replace;
import org.springframework.boot.test.autoconfigure.jdbc.JdbcTest;
import org.springframework.context.annotation.Import;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;

/**
 * Real JDBC against the licensing replica from docker-compose.
 *
 * <p>Deliberately not an embedded database. The behaviour under test is how this code
 * handles someone else's schema: {@code char(12)} padding, an unconstrained status column,
 * nullable dates. An in-memory substitute would have none of those and would pass.
 */
@JdbcTest
@AutoConfigureTestDatabase(replace = Replace.NONE)
@Import(ContractorLicenseRepository.class)
class ContractorLicenseRepositoryTest {

    @Autowired private ContractorLicenseRepository repository;
    @Autowired private JdbcTemplate jdbcTemplate;

    @Test
    @DisplayName("reads a licence over JDBC")
    void findsALicence() {
        Optional<LicenseRecord> found = repository.findByLicenseNumber("VA-CL-004182");

        assertThat(found).isPresent();
        LicenseRecord record = found.get();
        assertThat(record.businessName()).isEqualTo("Harlowe Building Group LLC");
        assertThat(record.status()).isEqualTo("ACTIVE");
        assertThat(record.expiresOn()).isNotNull();
    }

    @Test
    @DisplayName("strips the char(12) padding the replica returns")
    void stripsPadding() {
        LicenseRecord record = repository.findByLicenseNumber("VA-CL-004182").orElseThrow();

        assertThat(record.licenseNumber()).isEqualTo("VA-CL-004182");
        assertThat(record.licenseNumber()).doesNotContain(" ");
        assertThat(record.licenseType()).isEqualTo("CLSA");
    }

    @Test
    @DisplayName("an absent licence is an empty optional, not an exception")
    void absentLicence() {
        assertThat(repository.findByLicenseNumber("VA-CL-000000")).isEmpty();
    }

    @Test
    @DisplayName("reads every status the replica holds")
    void readsEveryStatus() {
        assertThat(repository.findByLicenseNumber("VA-CL-018871").orElseThrow().status())
                .isEqualTo("EXPIRED");
        assertThat(repository.findByLicenseNumber("VA-CL-020445").orElseThrow().status())
                .isEqualTo("SUSPENDED");
        assertThat(repository.findByLicenseNumber("VA-CL-022108").orElseThrow().status())
                .isEqualTo("REVOKED");
    }

    @Test
    @DisplayName("the licence number is a bound parameter, not interpolated SQL")
    void parametersAreBound() {
        String injection = "'; UPDATE licensing.contractor_license SET status='ACTIVE'; --";

        assertThat(repository.findByLicenseNumber(injection)).isEmpty();
        assertThat(repository.findByLicenseNumber("VA-CL-022108").orElseThrow().status())
                .isEqualTo("REVOKED");
    }

    @Test
    @DisplayName("a write fails here rather than travelling to the state's replica")
    void writesAreRefused() {
        // The refusal is the whole assertion. Nothing is read back afterwards because the
        // failed statement aborts this test's transaction, and Postgres rejects every
        // command until it ends. That the write never lands is confirmed by
        // parametersAreBound, which reads the same row in its own transaction.
        assertThatThrownBy(
                        () ->
                                jdbcTemplate.update(
                                        "UPDATE licensing.contractor_license SET status = 'ACTIVE' WHERE license_number = ?",
                                        "VA-CL-022108"))
                .isInstanceOf(DataAccessException.class);
    }
}
