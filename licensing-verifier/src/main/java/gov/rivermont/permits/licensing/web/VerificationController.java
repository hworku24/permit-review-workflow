package gov.rivermont.permits.licensing.web;

import gov.rivermont.permits.licensing.domain.VerificationResult;
import gov.rivermont.permits.licensing.service.LicenseVerificationService;
import java.time.Clock;
import java.time.LocalDate;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * Internal API. Not exposed outside the department network.
 *
 * <p>Every response is a 200, including when the replica is unreachable. That is
 * deliberate: an unreachable replica is a business outcome the caller has to record and act
 * on, not a transport failure it should retry blindly. The {@code verified} flag carries
 * the distinction.
 */
@RestController
@RequestMapping("/internal/licenses")
public class VerificationController {

    private final LicenseVerificationService service;
    private final Clock clock;

    public VerificationController(LicenseVerificationService service, Clock clock) {
        this.service = service;
        this.clock = clock;
    }

    @GetMapping("/{licenseNumber}/verification")
    public ResponseEntity<VerificationResult> verify(
            @PathVariable String licenseNumber,
            @RequestParam(required = false)
                    @DateTimeFormat(iso = DateTimeFormat.ISO.DATE)
                    LocalDate asOf) {

        LocalDate effectiveDate = asOf != null ? asOf : LocalDate.now(clock);
        return ResponseEntity.ok(service.verify(licenseNumber.trim(), effectiveDate));
    }
}
