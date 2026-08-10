package gov.rivermont.permits.licensing.service;

import gov.rivermont.permits.licensing.domain.IntakeEffect;
import gov.rivermont.permits.licensing.domain.LicenseRecord;
import gov.rivermont.permits.licensing.domain.LicenseStatus;
import gov.rivermont.permits.licensing.domain.VerificationResult;
import gov.rivermont.permits.licensing.repository.ContractorLicenseRepository;
import java.time.LocalDate;
import java.time.Period;
import java.util.Optional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.stereotype.Service;

/**
 * Turns a licence lookup into a decision about what intake should do.
 *
 * <p>These rules are the contract with the permitting application and are duplicated in its
 * Python client so that either path produces the same answer. The parity is asserted by a
 * test on the Python side that drives both against the same replica.
 */
@Service
public class LicenseVerificationService {

    private static final Logger log = LoggerFactory.getLogger(LicenseVerificationService.class);

    /** How close to expiry before the clerk is warned. */
    static final Period EXPIRY_WARNING_WINDOW = Period.ofDays(30);

    private final ContractorLicenseRepository repository;

    public LicenseVerificationService(ContractorLicenseRepository repository) {
        this.repository = repository;
    }

    public VerificationResult verify(String licenseNumber, LocalDate asOf) {
        Optional<LicenseRecord> found;
        try {
            found = repository.findByLicenseNumber(licenseNumber);
        } catch (DataAccessException e) {
            // Degrade rather than fail. A state outage must not stop the city accepting
            // permit applications, so this reports "could not check" and the application
            // proceeds with the gap recorded against it.
            log.warn("licensing replica unavailable for {}: {}", licenseNumber, e.getMessage());
            return VerificationResult.unverified(
                    licenseNumber,
                    "contractor licence could not be verified, the check must be repeated before issuance");
        }

        if (found.isEmpty()) {
            return new VerificationResult(
                    licenseNumber,
                    true,
                    LicenseStatus.NOT_FOUND,
                    IntakeEffect.RAISE_DEFICIENCY,
                    "no contractor licence found for the number supplied",
                    null,
                    null);
        }

        return classify(found.get(), asOf);
    }

    private VerificationResult classify(LicenseRecord record, LocalDate asOf) {
        LicenseStatus reported = parse(record.status());

        // A status this service cannot interpret stops here rather than falling through to
        // the expiry checks below, which would classify an unrecognised licence as active
        // purely because its expiry date happens to be in the future.
        if (reported == LicenseStatus.UNVERIFIED) {
            return result(
                    record,
                    LicenseStatus.UNVERIFIED,
                    IntakeEffect.RAISE_DEFICIENCY,
                    "contractor licence status \"%s\" is not recognised, manual check required"
                            .formatted(record.status()));
        }

        // Suspended and revoked escalate rather than auto rejecting. A revocation that
        // turns out to be an error in someone else's database should not deny a permit
        // without a person looking at it.
        if (reported == LicenseStatus.SUSPENDED || reported == LicenseStatus.REVOKED) {
            return result(
                    record,
                    reported,
                    IntakeEffect.ESCALATE_TO_SUPERVISOR,
                    "contractor licence is %s, supervisor review required"
                            .formatted(reported.name().toLowerCase()));
        }

        if (reported == LicenseStatus.EXPIRED) {
            return result(
                    record,
                    LicenseStatus.EXPIRED,
                    IntakeEffect.RAISE_DEFICIENCY,
                    "contractor licence expired on " + record.expiresOn());
        }

        // The replica's status column lags its own expiry dates, so the date wins.
        if (record.expiresOn() != null && !record.expiresOn().isAfter(asOf)) {
            return result(
                    record,
                    LicenseStatus.EXPIRED,
                    IntakeEffect.RAISE_DEFICIENCY,
                    "contractor licence expired on " + record.expiresOn());
        }

        if (record.expiresOn() != null
                && !record.expiresOn().isAfter(asOf.plus(EXPIRY_WARNING_WINDOW))) {
            return result(
                    record,
                    LicenseStatus.ACTIVE,
                    IntakeEffect.PROCEED_WITH_WARNING,
                    "contractor licence expires on %s, inside the 30 day window"
                            .formatted(record.expiresOn()));
        }

        return result(record, LicenseStatus.ACTIVE, IntakeEffect.PROCEED, "contractor licence is active");
    }

    /**
     * An unrecognised status string is treated as unverified rather than guessed at. The
     * column has no constraint, so a value nobody anticipated is a real possibility, and
     * defaulting it to active would be the worst available choice.
     */
    private LicenseStatus parse(String raw) {
        if (raw == null) {
            return LicenseStatus.UNVERIFIED;
        }
        try {
            return LicenseStatus.valueOf(raw);
        } catch (IllegalArgumentException e) {
            log.warn("unrecognised licence status from the replica: {}", raw);
            return LicenseStatus.UNVERIFIED;
        }
    }

    private VerificationResult result(
            LicenseRecord record, LicenseStatus status, IntakeEffect effect, String message) {
        return new VerificationResult(
                record.licenseNumber(),
                true,
                status,
                effect,
                message,
                record.businessName(),
                record.expiresOn());
    }
}
