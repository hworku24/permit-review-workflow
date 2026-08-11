"""The configuration screen.

The claim is that adding a review discipline is configuration and not a code change. The
test that matters proves it end to end: add a discipline through the form, then drive a new
application through intake and assert a task opened for it. Anything less is a test that the
form writes rows, which is not the claim.
"""

from __future__ import annotations

import re

from permitflow.db import read_connection, transaction
from permitflow.process.engine import Engine
from permitflow.ui.deps import ACTOR_COOKIE

from .conftest import APPLICANT, CLERK


def signed_in(client, username: str = "dhollis"):
    client.cookies.set(ACTOR_COOKIE, username)
    return client


def text_of(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


class TestAccess:
    def test_without_a_cookie_it_redirects(self, api_client) -> None:
        assert api_client.get("/ui/admin", follow_redirects=False).status_code == 303

    def test_a_reviewer_sees_it_read_only(self, api_client) -> None:
        signed_in(api_client, "pvasquez")
        body = text_of(api_client.get("/ui/admin").text)
        assert "read only for you" in body
        assert "Add discipline" not in body

    def test_a_clerk_may_not_add_one(self, api_client) -> None:
        signed_in(api_client, "mcarrero")
        response = api_client.post(
            "/ui/admin/disciplines", data={"code": "NOPE", "name": "Not allowed"}
        )
        assert response.status_code == 403

    def test_a_supervisor_gets_the_form(self, api_client) -> None:
        signed_in(api_client)
        assert "Add discipline" in api_client.get("/ui/admin").text


class TestListing:
    def test_it_lists_the_seeded_disciplines_with_their_wiring(self, api_client) -> None:
        signed_in(api_client)
        body = text_of(api_client.get("/ui/admin").text)
        for code in ("ZONING", "STRUCTURAL", "FIRE", "ENVIRONMENTAL"):
            assert code in body
        assert "Standing on" in body
        assert "Certified reviewers" in body

    def test_a_discipline_with_nobody_certified_is_flagged(self, api_client) -> None:
        """Configured and useless is a state worth seeing on the row."""
        signed_in(api_client)
        api_client.post(
            "/ui/admin/disciplines",
            data={"code": "ACOUSTIC", "name": "Acoustic review", "always_required": ["BLD-COM-NEW"]},
            follow_redirects=False,
        )
        body = text_of(api_client.get("/ui/admin").text)
        assert "nobody certified" in body

    def test_a_discipline_routed_to_nothing_is_flagged(self, api_client) -> None:
        signed_in(api_client)
        api_client.post(
            "/ui/admin/disciplines",
            data={"code": "ORPHAN", "name": "Never routed"},
            follow_redirects=False,
        )
        body = text_of(api_client.get("/ui/admin").text)
        assert "not routed" in body


class TestValidation:
    def test_a_bad_code_is_refused_with_a_reason(self, api_client) -> None:
        signed_in(api_client)
        response = api_client.post(
            "/ui/admin/disciplines", data={"code": "no spaces", "name": "X"}, follow_redirects=True
        )
        assert "not a usable code" in text_of(response.text)

    def test_a_missing_name_is_refused(self, api_client) -> None:
        signed_in(api_client)
        response = api_client.post(
            "/ui/admin/disciplines", data={"code": "VALID", "name": "   "}, follow_redirects=True
        )
        assert "A name is required" in text_of(response.text)

    def test_a_duplicate_code_is_refused(self, api_client) -> None:
        signed_in(api_client)
        response = api_client.post(
            "/ui/admin/disciplines",
            data={"code": "ZONING", "name": "Zoning again"},
            follow_redirects=True,
        )
        assert "already exists" in text_of(response.text)

    def test_standing_wins_when_both_boxes_are_ticked(self, api_client) -> None:
        """Silently dropping one of the two would be worse than picking the stronger rule."""
        signed_in(api_client)
        api_client.post(
            "/ui/admin/disciplines",
            data={
                "code": "BOTH",
                "name": "Both boxes",
                "always_required": ["BLD-RES-ALT"],
                "candidate": ["BLD-RES-ALT"],
            },
            follow_redirects=False,
        )
        with read_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT always_required FROM permit_type_discipline WHERE discipline_code = 'BOTH'"
            )
            rows = cur.fetchall()
        assert [r["always_required"] for r in rows] == [True]


class TestItActuallyRoutes:
    def test_a_new_discipline_opens_a_task_on_the_next_application(
        self, api_client, committed_parties
    ) -> None:
        """The whole claim, checked against the engine and not against the form."""
        signed_in(api_client)
        response = api_client.post(
            "/ui/admin/disciplines",
            data={
                "code": "ACCESSIBILITY",
                "name": "Accessibility review",
                "description": "Checks accessible routes and clearances",
                "always_required": ["BLD-RES-ALT"],
                "reviewers": [],
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        with transaction() as conn:
            engine = Engine(conn)
            application_id = engine.create_application(
                applicant_id=committed_parties["applicant_id"],
                parcel_id=committed_parties["parcel_id"],
                contractor_id=committed_parties["contractor_id"],
                permit_type_code="BLD-RES-ALT",
                scope_narrative="Rear addition, 640 sq ft. Value $180,000.",
                declared_valuation=180000,
                actor=APPLICANT,
            )
            engine.submit(application_id, APPLICANT)
            engine.complete_enrichment(application_id)
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE application_document SET status='RECEIVED', filename='x.pdf',
                       uploaded_at=now(), uploaded_by='t'
                       WHERE application_id=%s AND status='MISSING'""",
                    (str(application_id),),
                )
            engine.accept_intake(application_id, CLERK)

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT discipline_code, status, reviewer_id FROM review_task WHERE application_id = %s",
                    (str(application_id),),
                )
                tasks = cur.fetchall()

        opened = {t["discipline_code"] for t in tasks}
        assert "ACCESSIBILITY" in opened

        # Nobody was certified, so the task exists and waits. The screen says this happens.
        new_task = next(t for t in tasks if t["discipline_code"] == "ACCESSIBILITY")
        assert new_task["reviewer_id"] is None
        assert new_task["status"] == "PENDING"

    def test_certifying_a_reviewer_gets_the_task_assigned(
        self, api_client, committed_parties
    ) -> None:
        with read_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM reviewer WHERE username = 'pvasquez'")
            reviewer_id = cur.fetchone()["id"]

        signed_in(api_client)
        api_client.post(
            "/ui/admin/disciplines",
            data={
                "code": "NOISE",
                "name": "Noise review",
                "always_required": ["BLD-RES-ALT"],
                "reviewers": [str(reviewer_id)],
            },
            follow_redirects=False,
        )

        with transaction() as conn:
            engine = Engine(conn)
            application_id = engine.create_application(
                applicant_id=committed_parties["applicant_id"],
                parcel_id=committed_parties["parcel_id"],
                contractor_id=committed_parties["contractor_id"],
                permit_type_code="BLD-RES-ALT",
                scope_narrative="Rear addition, 640 sq ft. Value $180,000.",
                declared_valuation=180000,
                actor=APPLICANT,
            )
            engine.submit(application_id, APPLICANT)
            engine.complete_enrichment(application_id)
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE application_document SET status='RECEIVED', filename='x.pdf',
                       uploaded_at=now(), uploaded_by='t'
                       WHERE application_id=%s AND status='MISSING'""",
                    (str(application_id),),
                )
            engine.accept_intake(application_id, CLERK)
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT reviewer_id, status FROM review_task
                       WHERE application_id = %s AND discipline_code = 'NOISE'""",
                    (str(application_id),),
                )
                task = cur.fetchone()

        assert task["reviewer_id"] == reviewer_id
        assert task["status"] == "ASSIGNED"

    def test_the_new_discipline_inherits_the_permit_type_allowance(
        self, api_client, committed_parties
    ) -> None:
        """No sla_policy row was written, so it falls back to the null-discipline row."""
        signed_in(api_client)
        api_client.post(
            "/ui/admin/disciplines",
            data={"code": "SURVEY", "name": "Survey review", "always_required": ["BLD-RES-ALT"]},
            follow_redirects=False,
        )

        with read_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT allowance_days FROM sla_policy
                   WHERE permit_type_code = 'BLD-RES-ALT' AND phase = 'REVIEW_TASK'
                     AND discipline_code IS NULL"""
            )
            inherited = cur.fetchone()["allowance_days"]
            cur.execute("SELECT count(*) AS n FROM sla_policy WHERE discipline_code = 'SURVEY'")
            assert cur.fetchone()["n"] == 0
        assert inherited > 0


class TestAudit:
    def test_the_change_is_recorded_with_who_made_it(self, api_client) -> None:
        signed_in(api_client)
        api_client.post(
            "/ui/admin/disciplines",
            data={"code": "AUDITED", "name": "Audited discipline", "always_required": ["BLD-COM-ALT"]},
            follow_redirects=False,
        )
        with read_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT actor, after_value FROM audit_log
                   WHERE entity_type = 'configuration' AND action = 'add:discipline'"""
            )
            row = cur.fetchone()
        assert row["actor"] == "dhollis"
        assert row["after_value"]["code"] == "AUDITED"
        assert row["after_value"]["always_required"] == ["BLD-COM-ALT"]


class TestOpenCasesAreLeftAlone:
    def test_a_case_already_under_review_does_not_gain_the_new_discipline(
        self, api_client, application_under_review
    ) -> None:
        """Routing is decided at intake. Reopening it would rewrite a decision already made."""
        before = _disciplines_on(application_under_review["application_id"])

        signed_in(api_client)
        api_client.post(
            "/ui/admin/disciplines",
            data={"code": "LATECOMER", "name": "Added after the fact",
                  "always_required": ["BLD-RES-ALT"]},
            follow_redirects=False,
        )

        assert _disciplines_on(application_under_review["application_id"]) == before
        assert "LATECOMER" not in before


def _disciplines_on(application_id) -> set[str]:
    with read_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT discipline_code FROM review_task WHERE application_id = %s",
            (str(application_id),),
        )
        return {r["discipline_code"] for r in cur.fetchall()}


def _policy_id(permit_type: str, phase: str, discipline=None) -> str:
    with read_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id FROM sla_policy
               WHERE permit_type_code = %s AND phase = %s
                 AND discipline_code IS NOT DISTINCT FROM %s""",
            (permit_type, phase, discipline),
        )
        return str(cur.fetchone()["id"])


def _open_case(committed_parties, permit_type: str = "BLD-RES-ALT"):
    """Drive one application to UNDER_REVIEW and return its id."""
    with transaction() as conn:
        engine = Engine(conn)
        application_id = engine.create_application(
            applicant_id=committed_parties["applicant_id"],
            parcel_id=committed_parties["parcel_id"],
            contractor_id=committed_parties["contractor_id"],
            permit_type_code=permit_type,
            scope_narrative="Rear addition, 640 sq ft. Value $180,000.",
            declared_valuation=180000,
            actor=APPLICANT,
        )
        engine.submit(application_id, APPLICANT)
        engine.complete_enrichment(application_id)
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE application_document SET status='RECEIVED', filename='x.pdf',
                   uploaded_at=now(), uploaded_by='t'
                   WHERE application_id=%s AND status='MISSING'""",
                (str(application_id),),
            )
        engine.accept_intake(application_id, CLERK)
    return application_id


class TestPhaseAllowance:
    def test_the_screen_lists_the_policies(self, api_client) -> None:
        signed_in(api_client)
        body = text_of(api_client.get("/ui/admin").text)
        assert "Phase allowances" in body
        assert "REVIEW_TASK" in body

    def test_a_reviewer_may_not_change_one(self, api_client) -> None:
        signed_in(api_client, "pvasquez")
        response = api_client.post(
            "/ui/admin/sla",
            data={"policy_id": _policy_id("BLD-RES-ALT", "REVIEW_TASK"), "allowance_days": 5},
        )
        assert response.status_code == 403

    def test_an_absurd_allowance_is_refused(self, api_client) -> None:
        signed_in(api_client)
        response = api_client.post(
            "/ui/admin/sla",
            data={"policy_id": _policy_id("BLD-RES-ALT", "REVIEW_TASK"), "allowance_days": 0},
            follow_redirects=True,
        )
        assert "between 1 and 365" in text_of(response.text)

    def test_a_new_task_takes_the_new_allowance(self, api_client, committed_parties) -> None:
        """The checklist item: the workflow uses the new value with no code change."""
        signed_in(api_client)
        api_client.post(
            "/ui/admin/sla",
            data={"policy_id": _policy_id("BLD-RES-ALT", "REVIEW_TASK"), "allowance_days": 9},
            follow_redirects=False,
        )
        application_id = _open_case(committed_parties)

        with read_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT allowance_days FROM review_task WHERE application_id = %s",
                (str(application_id),),
            )
            allowances = {r["allowance_days"] for r in cur.fetchall()}
        assert allowances == {9}

    def test_an_open_task_keeps_the_allowance_it_opened_with(
        self, api_client, committed_parties
    ) -> None:
        """A supervisor cannot make a reviewer late by editing configuration underneath them."""
        application_id = _open_case(committed_parties)
        with read_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT allowance_days FROM review_task WHERE application_id = %s",
                (str(application_id),),
            )
            before = {r["allowance_days"] for r in cur.fetchall()}

        signed_in(api_client)
        api_client.post(
            "/ui/admin/sla",
            data={"policy_id": _policy_id("BLD-RES-ALT", "REVIEW_TASK"), "allowance_days": 2},
            follow_redirects=False,
        )

        with read_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT allowance_days FROM review_task WHERE application_id = %s",
                (str(application_id),),
            )
            after = {r["allowance_days"] for r in cur.fetchall()}
        assert after == before
        assert 2 not in after

    def test_a_discipline_override_beats_the_general_row(
        self, api_client, committed_parties
    ) -> None:
        signed_in(api_client)
        api_client.post(
            "/ui/admin/sla",
            data={
                "policy_id": _policy_id("BLD-COM-NEW", "REVIEW_TASK", "ENVIRONMENTAL"),
                "allowance_days": 31,
            },
            follow_redirects=False,
        )
        application_id = _open_case(committed_parties, permit_type="BLD-COM-NEW")

        with read_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT discipline_code, allowance_days FROM review_task
                   WHERE application_id = %s""",
                (str(application_id),),
            )
            by_discipline = {r["discipline_code"]: r["allowance_days"] for r in cur.fetchall()}

        assert by_discipline["ENVIRONMENTAL"] == 31
        assert by_discipline["ZONING"] != 31

    def test_the_change_is_audited_with_before_and_after(self, api_client) -> None:
        signed_in(api_client)
        api_client.post(
            "/ui/admin/sla",
            data={"policy_id": _policy_id("BLD-RES-ACC", "REVIEW_TASK"), "allowance_days": 13},
            follow_redirects=False,
        )
        with read_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT actor, before_value, after_value FROM audit_log
                   WHERE action = 'change:sla_allowance'"""
            )
            row = cur.fetchone()
        assert row["actor"] == "dhollis"
        assert row["before_value"]["allowance_days"] == 11
        assert row["after_value"]["allowance_days"] == 13


class TestCouncilStandard:
    def test_a_change_with_no_effect_on_history_goes_straight_through(self, api_client) -> None:
        """Nothing decided yet, so there is nothing to rewrite and nothing to confirm."""
        signed_in(api_client)
        response = api_client.post(
            "/ui/admin/council-standard",
            data={"permit_type_code": "BLD-RES-ALT", "council_standard_days": 21},
            follow_redirects=True,
        )
        assert "standard is now 21 days" in text_of(response.text)

    def test_a_change_that_rewrites_history_asks_first(self, api_client, decided_cases) -> None:
        signed_in(api_client)
        response = api_client.post(
            "/ui/admin/council-standard",
            data={"permit_type_code": "BLD-RES-ALT", "council_standard_days": 2},
            follow_redirects=True,
        )
        body = text_of(response.text)
        assert "Confirm a retroactive change" in body
        assert "Cases changing side" in body

        # Nothing changed yet.
        with read_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT council_standard_days FROM permit_type WHERE code = 'BLD-RES-ALT'")
            assert cur.fetchone()["council_standard_days"] == 20

    def test_confirming_applies_it_and_records_the_movement(
        self, api_client, decided_cases
    ) -> None:
        with read_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT round(100.0*count(*) FILTER (WHERE net_business_days <= 20)/count(*),1) AS pct
                   FROM v_cycle_time WHERE permit_type_code='BLD-RES-ALT' AND decided_at IS NOT NULL"""
            )
            before_pct = cur.fetchone()["pct"]

        signed_in(api_client)
        api_client.post(
            "/ui/admin/council-standard",
            data={
                "permit_type_code": "BLD-RES-ALT",
                "council_standard_days": 2,
                "confirm": "yes",
            },
            follow_redirects=False,
        )

        with read_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT council_standard_days FROM permit_type WHERE code='BLD-RES-ALT'")
            assert cur.fetchone()["council_standard_days"] == 2

            cur.execute(
                """SELECT before_value, after_value FROM audit_log
                   WHERE action = 'change:council_standard'"""
            )
            row = cur.fetchone()

        assert row["before_value"]["council_standard_days"] == 20
        assert row["after_value"]["council_standard_days"] == 2
        assert float(row["before_value"]["reported_compliance_pct"]) == float(before_pct)
        # Signed, and negative here: tightening the standard turns met cases into missed
        # ones. The direction is worth keeping in the row, so the message says which way.
        assert row["after_value"]["decided_cases_reclassified"] < 0

    def test_the_published_compliance_figure_really_does_move(
        self, api_client, decided_cases
    ) -> None:
        """Why the confirmation exists. The view joins permit_type live."""
        with read_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT sum(met_count) AS met FROM v_sla_compliance")
            met_before = cur.fetchone()["met"]

        signed_in(api_client)
        api_client.post(
            "/ui/admin/council-standard",
            data={"permit_type_code": "BLD-RES-ALT", "council_standard_days": 1, "confirm": "yes"},
            follow_redirects=False,
        )

        with read_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT sum(met_count) AS met FROM v_sla_compliance")
            met_after = cur.fetchone()["met"]

        assert met_after < met_before

    def test_a_reviewer_may_not_change_it(self, api_client) -> None:
        signed_in(api_client, "pvasquez")
        response = api_client.post(
            "/ui/admin/council-standard",
            data={"permit_type_code": "BLD-RES-ALT", "council_standard_days": 25},
        )
        assert response.status_code == 403
