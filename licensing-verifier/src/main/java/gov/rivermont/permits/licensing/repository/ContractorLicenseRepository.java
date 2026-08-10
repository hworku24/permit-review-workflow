package gov.rivermont.permits.licensing.repository;

import gov.rivermont.permits.licensing.domain.LicenseRecord;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.Optional;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

/**
 * JDBC access to the state's contractor licensing replica.
 *
 * <p>Hand-written SQL against a schema this jurisdiction does not own and cannot migrate.
 * The quirks below are the schema's, not ours, and are handled here so no caller has to
 * know about them:
 *
 * <ul>
 *   <li>{@code license_number} is {@code char(12)} and comes back space padded.
 *   <li>{@code status} is an unconstrained {@code varchar}, so it can hold anything and is
 *       normalised rather than trusted.
 *   <li>{@code issued_on} and {@code expires_on} are nullable even on active licences.
 * </ul>
 *
 * <p>A read only credential is configured on the data source. That is belt and braces
 * alongside the grant on the state's side: the account we are issued only has SELECT, and
 * the pool refuses writes as well, so a mistake here fails locally rather than against
 * someone else's database.
 */
@Repository
public class ContractorLicenseRepository {

    private static final String FIND_BY_NUMBER =
            """
            SELECT license_number,
                   business_name,
                   license_type,
                   status,
                   issued_on,
                   expires_on,
                   disciplinary_action_count
            FROM licensing.contractor_license
            WHERE license_number = ?
            """;

    private final JdbcTemplate jdbcTemplate;

    public ContractorLicenseRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    /**
     * Look up one licence.
     *
     * @return the record, or empty when the state has no row for this number. Empty is a
     *     definitive answer and is different from the query failing, which throws.
     * @throws DataAccessException when the replica could not be reached or the query
     *     failed. The caller turns that into an unverified result rather than a 500.
     */
    public Optional<LicenseRecord> findByLicenseNumber(String licenseNumber) {
        return jdbcTemplate.query(FIND_BY_NUMBER, ROW_MAPPER, licenseNumber).stream().findFirst();
    }

    private static final RowMapper<LicenseRecord> ROW_MAPPER = ContractorLicenseRepository::mapRow;

    private static LicenseRecord mapRow(ResultSet rs, int rowNum) throws SQLException {
        return new LicenseRecord(
                trimmed(rs.getString("license_number")),
                trimmed(rs.getString("business_name")),
                trimmed(rs.getString("license_type")),
                normalisedStatus(rs.getString("status")),
                rs.getDate("issued_on") == null ? null : rs.getDate("issued_on").toLocalDate(),
                rs.getDate("expires_on") == null ? null : rs.getDate("expires_on").toLocalDate(),
                rs.getInt("disciplinary_action_count"));
    }

    private static String trimmed(String value) {
        return value == null ? null : value.trim();
    }

    /**
     * The status column has no constraint on it, so casing and padding vary by whichever
     * system wrote the row. Normalising once here keeps that out of the rules.
     */
    private static String normalisedStatus(String value) {
        return value == null ? null : value.trim().toUpperCase();
    }
}
