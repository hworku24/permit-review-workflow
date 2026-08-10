package gov.rivermont.permits.licensing.domain;

/**
 * Outcome of a licence lookup.
 *
 * <p>{@code UNVERIFIED} is a distinct value from {@code EXPIRED}, and neither is
 * {@code ACTIVE}. Collapsing "could not check" into "checked and it passed" is how an
 * outage quietly waves a revoked licence through intake, and it is the kind of defect that
 * only surfaces during an appeal.
 */
public enum LicenseStatus {
    ACTIVE,
    EXPIRED,
    SUSPENDED,
    REVOKED,
    NOT_FOUND,
    UNVERIFIED
}
