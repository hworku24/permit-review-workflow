package gov.rivermont.permits.licensing.domain;

/** What a licence result does to the permit application. */
public enum IntakeEffect {
    PROCEED,
    PROCEED_WITH_WARNING,
    RAISE_DEFICIENCY,
    ESCALATE_TO_SUPERVISOR
}
