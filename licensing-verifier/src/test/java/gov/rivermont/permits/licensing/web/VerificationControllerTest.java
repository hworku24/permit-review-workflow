package gov.rivermont.permits.licensing.web;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import gov.rivermont.permits.licensing.domain.IntakeEffect;
import gov.rivermont.permits.licensing.domain.LicenseStatus;
import gov.rivermont.permits.licensing.domain.VerificationResult;
import gov.rivermont.permits.licensing.service.LicenseVerificationService;
import java.time.Clock;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.context.annotation.Bean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(VerificationController.class)
class VerificationControllerTest {

    @Autowired private MockMvc mockMvc;
    @MockBean private LicenseVerificationService service;

    @TestConfiguration
    static class FixedClock {
        @Bean
        Clock clock() {
            return Clock.fixed(Instant.parse("2026-08-03T13:00:00Z"), ZoneId.of("America/New_York"));
        }
    }

    @Test
    @DisplayName("serialises a verification result")
    void serialisesResult() throws Exception {
        when(service.verify(eq("VA-CL-004182"), any()))
                .thenReturn(
                        new VerificationResult(
                                "VA-CL-004182",
                                true,
                                LicenseStatus.ACTIVE,
                                IntakeEffect.PROCEED,
                                "contractor licence is active",
                                "Harlowe Building Group LLC",
                                LocalDate.of(2028, 4, 2)));

        mockMvc.perform(get("/internal/licenses/VA-CL-004182/verification"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.licenseNumber").value("VA-CL-004182"))
                .andExpect(jsonPath("$.verified").value(true))
                .andExpect(jsonPath("$.status").value("ACTIVE"))
                .andExpect(jsonPath("$.effect").value("PROCEED"))
                .andExpect(jsonPath("$.expiresOn").value("2028-04-02"));
    }

    @Test
    @DisplayName("an unreachable replica is still a 200, carrying verified=false")
    void unreachableIsNotAServerError() throws Exception {
        when(service.verify(any(), any()))
                .thenReturn(VerificationResult.unverified("VA-CL-004182", "replica unavailable"));

        mockMvc.perform(get("/internal/licenses/VA-CL-004182/verification"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.verified").value(false))
                .andExpect(jsonPath("$.status").value("UNVERIFIED"))
                .andExpect(jsonPath("$.effect").value("RAISE_DEFICIENCY"));
    }

    @Test
    @DisplayName("asOf defaults to today in the department's zone")
    void asOfDefaultsToToday() throws Exception {
        when(service.verify(any(), any()))
                .thenReturn(VerificationResult.unverified("X", "x"));

        mockMvc.perform(get("/internal/licenses/VA-CL-004182/verification"))
                .andExpect(status().isOk());

        verify(service).verify("VA-CL-004182", LocalDate.of(2026, 8, 3));
    }

    @Test
    @DisplayName("asOf can be supplied, which is what makes expiry behaviour testable")
    void asOfCanBeSupplied() throws Exception {
        when(service.verify(any(), any()))
                .thenReturn(VerificationResult.unverified("X", "x"));

        mockMvc.perform(get("/internal/licenses/VA-CL-004182/verification").param("asOf", "2027-01-01"))
                .andExpect(status().isOk());

        verify(service).verify("VA-CL-004182", LocalDate.of(2027, 1, 1));
    }
}
