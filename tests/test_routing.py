"""Discipline routing rules (FR-07)."""

from __future__ import annotations

from permitflow.process import routing


class TestStandingRules:
    def test_residential_alteration(self, conn) -> None:
        assert routing.always_required_disciplines(conn, "BLD-RES-ALT") == ["STRUCTURAL", "ZONING"]

    def test_commercial_new_construction_routes_all_four(self, conn) -> None:
        assert routing.always_required_disciplines(conn, "BLD-COM-NEW") == [
            "ENVIRONMENTAL",
            "FIRE",
            "STRUCTURAL",
            "ZONING",
        ]

    def test_candidates_are_not_automatic(self, conn) -> None:
        base = routing.always_required_disciplines(conn, "BLD-RES-ALT")
        candidates = routing.candidate_disciplines(conn, "BLD-RES-ALT")
        assert "ENVIRONMENTAL" in candidates
        assert "ENVIRONMENTAL" not in base


class TestResolveRouting:
    def test_no_additions_returns_standing_rules(self, conn) -> None:
        assert routing.resolve_routing(conn, "BLD-RES-ALT") == ["STRUCTURAL", "ZONING"]

    def test_confirmed_candidate_is_added(self, conn) -> None:
        result = routing.resolve_routing(conn, "BLD-RES-ALT", ["ENVIRONMENTAL"])
        assert result == ["ENVIRONMENTAL", "STRUCTURAL", "ZONING"]

    def test_unconfigured_addition_is_dropped(self, conn) -> None:
        """A recommendation cannot invent a discipline the department does not run here."""
        result = routing.resolve_routing(conn, "BLD-RES-ACC", ["FIRE", "ENVIRONMENTAL"])
        assert "FIRE" not in result
        assert "ENVIRONMENTAL" not in result

    def test_duplicate_of_a_standing_rule_does_not_double(self, conn) -> None:
        result = routing.resolve_routing(conn, "BLD-RES-ALT", ["ZONING"])
        assert result == ["STRUCTURAL", "ZONING"]

    def test_routing_can_only_widen(self, conn) -> None:
        """Standing rules survive any recommendation, including an empty one."""
        base = set(routing.always_required_disciplines(conn, "BLD-COM-NEW"))
        assert base <= set(routing.resolve_routing(conn, "BLD-COM-NEW", []))
