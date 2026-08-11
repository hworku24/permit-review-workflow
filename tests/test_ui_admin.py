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
