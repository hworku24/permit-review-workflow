package gov.rivermont.permits.licensing.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;

import gov.rivermont.permits.licensing.domain.IntakeEffect;
import gov.rivermont.permits.licensing.domain.LicenseRecord;
import gov.rivermont.permits.licensing.domain.LicenseStatus;
import gov.rivermont.permits.licensing.domain.VerificationResult;
import gov.rivermont.permits.licensing.repository.ContractorLicenseRepository;
import java.time.LocalDate;
import java.util.Optional;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.dao.QueryTimeoutException;

/** The rules, with no database in the way. */
@ExtendWith(MockitoExtension.class)
class LicenseVerificationServiceTest {

    private static final LocalDate AS_OF = LocalDate.of(2026, 8, 3);

    @Mock private ContractorLicenseRepository repository;

    private LicenseVerificationService service() {
        return new LicenseVerificationService(repository);
    }

    private LicenseRecord record(String status, LocalDate expiresOn) {
        return new LicenseRecord(
                "VA-CL-004182", "Harlowe Building Group LLC", "CLSA", status,
                LocalDate.of(2019, 4, 2), expiresOn, 0);
    }

    private VerificationResult verifyWith(String status, LocalDate expiresOn) {
        when(repository.findByLicenseNumber(anyString()))
                .thenReturn(Optional.of(record(status, expiresOn)));
        return service().verify("VA-CL-004182", AS_OF);
    }

    @Test
    @DisplayName("an active licence well before expiry proceeds")
    void activeProceeds() {
        VerificationResult result = verifyWith("ACTIVE", LocalDate.of(2028, 4, 2));
        assertThat(result.verified()).isTrue();
        assertThat(result.status()).isEqualTo(LicenseStatus.ACTIVE);
        assertThat(result.effect()).isEqualTo(IntakeEffect.PROCEED);
    }

    @Test
    @DisplayName("expiry inside the 30 day window warns but does not block")
    void expiringSoonWarns() {
        VerificationResult result = verifyWith("ACTIVE", AS_OF.plusDays(25));
        assertThat(result.status()).isEqualTo(LicenseStatus.ACTIVE);
        assertThat(result.effect()).isEqualTo(IntakeEffect.PROCEED_WITH_WARNING);
        assertThat(result.message()).contains("30 day window");
    }

    @Test
    @DisplayName("the boundary of the warning window is inclusive")
    void warningWindowBoundary() {
        assertThat(verifyWith("ACTIVE", AS_OF.plusDays(30)).effect())
                .isEqualTo(IntakeEffect.PROCEED_WITH_WARNING);
        assertThat(verifyWith("ACTIVE", AS_OF.plusDays(31)).effect())
                .isEqualTo(IntakeEffect.PROCEED);
    }

    @Test
    @DisplayName("an expiry date in the past beats a stale ACTIVE status column")
    void expiryDateBeatsStaleStatus() {
        VerificationResult result = verifyWith("ACTIVE", AS_OF.minusDays(1));
        assertThat(result.status()).isEqualTo(LicenseStatus.EXPIRED);
        assertThat(result.effect()).isEqualTo(IntakeEffect.RAISE_DEFICIENCY);
    }

    @Test
    @DisplayName("expiring today counts as expired")
    void expiresToday() {
        assertThat(verifyWith("ACTIVE", AS_OF).status()).isEqualTo(LicenseStatus.EXPIRED);
    }

    @Test
    @DisplayName("suspended and revoked escalate rather than auto rejecting")
    void suspendedAndRevokedEscalate() {
        assertThat(verifyWith("SUSPENDED", LocalDate.of(2027, 3, 14)).effect())
                .isEqualTo(IntakeEffect.ESCALATE_TO_SUPERVISOR);
        assertThat(verifyWith("REVOKED", LocalDate.of(2026, 8, 30)).effect())
                .isEqualTo(IntakeEffect.ESCALATE_TO_SUPERVISOR);
    }

    @Test
    @DisplayName("a missing row is a definitive answer, not an outage")
    void notFound() {
        when(repository.findByLicenseNumber(anyString())).thenReturn(Optional.empty());
        VerificationResult result = service().verify("VA-CL-999999", AS_OF);
        assertThat(result.verified()).isTrue();
        assertThat(result.status()).isEqualTo(LicenseStatus.NOT_FOUND);
        assertThat(result.effect()).isEqualTo(IntakeEffect.RAISE_DEFICIENCY);
    }

    @Test
    @DisplayName("an unreachable replica is never reported as active")
    void unreachableIsNeverActive() {
        when(repository.findByLicenseNumber(anyString()))
                .thenThrow(new QueryTimeoutException("replica unreachable"));

        VerificationResult result = service().verify("VA-CL-004182", AS_OF);
        assertThat(result.verified()).isFalse();
        assertThat(result.status()).isEqualTo(LicenseStatus.UNVERIFIED);
        assertThat(result.status()).isNotEqualTo(LicenseStatus.ACTIVE);
        assertThat(result.effect()).isEqualTo(IntakeEffect.RAISE_DEFICIENCY);
    }

    @Test
    @DisplayName("an unrecognised status is not guessed at")
    void unrecognisedStatus() {
        VerificationResult result = verifyWith("PENDING_RENEWAL", LocalDate.of(2030, 1, 1));
        assertThat(result.status()).isEqualTo(LicenseStatus.UNVERIFIED);
        assertThat(result.effect()).isEqualTo(IntakeEffect.RAISE_DEFICIENCY);
        assertThat(result.message()).contains("not recognised");
    }

    @Test
    @DisplayName("a null expiry date does not crash the expiry checks")
    void nullExpiry() {
        VerificationResult result = verifyWith("ACTIVE", null);
        assertThat(result.status()).isEqualTo(LicenseStatus.ACTIVE);
        assertThat(result.effect()).isEqualTo(IntakeEffect.PROCEED);
    }
}
