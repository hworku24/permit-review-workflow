package gov.rivermont.permits.licensing.domain;

import java.time.LocalDate;

/**
 * One row of {@code licensing.contractor_license}, as the state stores it.
 *
 * <p>Field names follow the replica's columns rather than this service's conventions,
 * because the schema belongs to the state and renaming it here would hide that.
 */
public record LicenseRecord(
        String licenseNumber,
        String businessName,
        String licenseType,
        String status,
        LocalDate issuedOn,
        LocalDate expiresOn,
        int disciplinaryActionCount) {
}
