package gov.rivermont.permits.licensing.domain;

import java.time.LocalDate;

/**
 * What the caller gets back.
 *
 * <p>{@code verified} answers "did we reach the state's replica", separately from
 * {@code status}, which answers "what did it say". A caller that only reads {@code status}
 * still cannot mistake an outage for a pass, because an unreachable replica reports
 * {@link LicenseStatus#UNVERIFIED}.
 */
public record VerificationResult(
        String licenseNumber,
        boolean verified,
        LicenseStatus status,
        IntakeEffect effect,
        String message,
        String businessName,
        LocalDate expiresOn) {

    public static VerificationResult unverified(String licenseNumber, String message) {
        return new VerificationResult(
                licenseNumber,
                false,
                LicenseStatus.UNVERIFIED,
                IntakeEffect.RAISE_DEFICIENCY,
                message,
                null,
                null);
    }
}
