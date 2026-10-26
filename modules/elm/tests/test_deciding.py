"""WF-EL-01 from the substitute to the final decision, and who may take it."""
from decimal import Decimal

import pytest
from freezegun import freeze_time

from core.api.exceptions import ConflictError, PermissionDeniedError
from modules.elm.models import LeaveApplication, LeaveBalance, LeaveNotification
from modules.elm.services import applications, decisions
from modules.elm.services.common import RuleBroken
from modules.elm.tests.support import (
    DIRECTOR,
    DIRECTOR_PERMS,
    EMPLOYEE,
    ESTABLISHMENT,
    ESTABLISHMENT_PERMS,
    FIRST_DAY,
    OTHER_UNIT_HEAD,
    PEER,
    SANCTION_PERMS,
    SANCTIONER,
    UNIT_HEAD,
    UNIT_HEAD_PERMS,
    D,
    acting,
    request,
)

pytestmark = pytest.mark.django_db


def unit_head(user=UNIT_HEAD):
    return acting(user, UNIT_HEAD_PERMS)


def sanctioner():
    return acting(SANCTIONER, SANCTION_PERMS)


def establishment():
    return acting(ESTABLISHMENT, ESTABLISHMENT_PERMS)


def used(leave_type="CL", user=EMPLOYEE):
    return LeaveBalance.objects.get(user_id=user, year=2026, leave_type=leave_type).used


def apply(leave_type="CL", start=FIRST_DAY, end=None, **extra):
    return applications.submit(acting(EMPLOYEE), request(leave_type, start, end, **extra))


class TestSubstitute:
    def test_acceptance_releases_the_request_to_the_unit_head(self):
        app = apply(substitute_user_id=PEER)
        done = decisions.respond_as_substitute(acting(PEER), app.pk, accept=True)
        assert done.status == "AWAITING_UNIT_HEAD"
        assert LeaveNotification.objects.filter(
            application=app, recipient_permission="elm.leave.review").exists()

    def test_nothing_is_routed_before_the_substitute_accepts(self):
        app = apply(substitute_user_id=PEER)
        with pytest.raises(ConflictError):
            decisions.unit_head_decide(unit_head(), app.pk, decision="approve")

    def test_a_decline_sends_it_back_to_the_applicant(self):
        app = apply(substitute_user_id=PEER)
        done = decisions.respond_as_substitute(acting(PEER), app.pk, accept=False)
        assert done.status == "SUBSTITUTE_DECLINED"
        assert LeaveNotification.objects.filter(
            application=app, recipient_user_id=EMPLOYEE,
            topic="leave.substitute_declined").exists()

    def test_only_the_named_substitute_may_answer(self):
        app = apply(substitute_user_id=PEER)
        with pytest.raises(PermissionDeniedError):
            decisions.respond_as_substitute(acting(UNIT_HEAD), app.pk, accept=True)
        with pytest.raises(PermissionDeniedError):
            decisions.respond_as_substitute(acting(EMPLOYEE), app.pk, accept=True)

    def test_a_director_goes_on_to_self_sanction_after_acceptance(self):
        app = applications.submit(
            acting(DIRECTOR, DIRECTOR_PERMS), request(substitute_user_id=PEER))
        done = decisions.respond_as_substitute(acting(PEER), app.pk, accept=True)
        assert done.status == "AWAITING_DIRECTOR_SELF_SANCTION"


class TestUnitHeadFinalAuthority:
    """BR-EL-016: for CL and RH the Unit Head decides."""

    def test_approval_deducts_the_balance(self):
        app = apply(end=D(2026, 3, 4))
        done = decisions.unit_head_decide(unit_head(), app.pk, decision="approve")
        assert done.status == "APPROVED"
        assert used() == Decimal(3)
        assert done.decided_by == UNIT_HEAD and done.decided_at

    def test_rejection_deducts_nothing(self):
        app = apply()
        done = decisions.unit_head_decide(unit_head(), app.pk, decision="reject", reason="Exams")
        assert (done.status, done.decision_reason) == ("REJECTED", "Exams")
        assert used() == 0

    def test_the_ledger_records_the_deduction(self):
        app = apply()
        decisions.unit_head_decide(unit_head(), app.pk, decision="approve")
        row = LeaveBalance.objects.get(user_id=EMPLOYEE, leave_type="CL").transactions.get()
        assert (row.kind, row.days, row.application_id) == ("DEDUCT", 1, app.pk)

    def test_the_applicant_is_told(self):
        app = apply()
        decisions.unit_head_decide(unit_head(), app.pk, decision="approve")
        assert LeaveNotification.objects.filter(
            application=app, recipient_user_id=EMPLOYEE, topic="leave.approved").exists()

    def test_recommend_is_not_available_for_cl(self):
        app = apply()
        with pytest.raises(RuleBroken):
            decisions.unit_head_decide(unit_head(), app.pk, decision="recommend")

    def test_a_double_approval_is_refused(self):
        app = apply()
        decisions.unit_head_decide(unit_head(), app.pk, decision="approve")
        with pytest.raises(ConflictError):
            decisions.unit_head_decide(unit_head(), app.pk, decision="approve")
        assert used() == Decimal(1)


class TestWhoMayDecide:
    def test_nobody_decides_their_own_application(self):
        app = applications.submit(
            acting(UNIT_HEAD, UNIT_HEAD_PERMS), request(start=D(2026, 3, 3)))
        with pytest.raises(PermissionDeniedError):
            decisions.unit_head_decide(unit_head(), app.pk, decision="approve")

    def test_a_unit_head_only_decides_for_their_own_unit(self):
        app = apply()
        with pytest.raises(PermissionDeniedError):
            decisions.unit_head_decide(unit_head(OTHER_UNIT_HEAD), app.pk, decision="approve")
        app.refresh_from_db()
        assert app.status == "AWAITING_UNIT_HEAD"

    def test_the_permission_is_required(self):
        app = apply()
        with pytest.raises(PermissionDeniedError):
            decisions.unit_head_decide(acting(UNIT_HEAD), app.pk, decision="approve")

    def test_approval_checks_the_balance_again_under_lock(self):
        app = apply(end=D(2026, 3, 4))
        LeaveBalance.objects.filter(user_id=EMPLOYEE, leave_type="CL").update(used=7)
        with pytest.raises(RuleBroken):
            decisions.unit_head_decide(unit_head(), app.pk, decision="approve")
        app.refresh_from_db()
        assert app.status == "AWAITING_UNIT_HEAD"
        assert used() == Decimal(7)


class TestHigherSanction:
    """BR-EL-017/018/019: SCL, EL, COL and VL are recommended, routed, then sanctioned."""

    def scl(self):
        return apply("SCL", D(2026, 3, 2), D(2026, 3, 4))

    def test_the_unit_head_can_only_recommend(self):
        app = self.scl()
        for decision in ("approve", "reject"):
            with pytest.raises(RuleBroken) as caught:
                decisions.unit_head_decide(unit_head(), app.pk, decision=decision)
            assert caught.value.details == [{"rule": "BR-EL-017"}]

    def test_recommendation_goes_to_establishment_then_final_sanction(self):
        app = self.scl()
        step = decisions.unit_head_decide(unit_head(), app.pk, decision="recommend")
        assert (step.status, step.unit_head_recommendation) == (
            "AWAITING_ESTABLISHMENT", "RECOMMENDED")
        step = decisions.establishment_route(establishment(), app.pk)
        assert step.status == "AWAITING_FINAL_SANCTION"
        assert used("SCL") == 0

    def test_not_recommended_still_goes_on_for_a_decision(self):
        app = self.scl()
        step = decisions.unit_head_decide(unit_head(), app.pk, decision="not_recommend")
        assert step.unit_head_recommendation == "NOT_RECOMMENDED"
        assert step.status == "AWAITING_ESTABLISHMENT"

    def test_establishment_step_can_be_switched_off(self, settings):
        settings.ELM_ESTABLISHMENT_STEP = False
        app = self.scl()
        step = decisions.unit_head_decide(unit_head(), app.pk, decision="recommend")
        assert step.status == "AWAITING_FINAL_SANCTION"

    def test_final_sanction_needs_the_admissibility_ruling_for_scl(self):
        app = self.scl()
        decisions.unit_head_decide(unit_head(), app.pk, decision="recommend")
        decisions.establishment_route(establishment(), app.pk)
        with pytest.raises(RuleBroken) as caught:
            decisions.final_decide(sanctioner(), app.pk, approve=True)
        assert caught.value.details == [{"rule": "BR-EL-004"}]
        done = decisions.final_decide(
            sanctioner(), app.pk, approve=True, special_purpose_admissible=True)
        assert done.status == "APPROVED"
        assert done.special_purpose_admissible is True
        assert used("SCL") == Decimal(3)

    def test_the_authority_may_reject(self):
        app = self.scl()
        decisions.unit_head_decide(unit_head(), app.pk, decision="recommend")
        decisions.establishment_route(establishment(), app.pk)
        done = decisions.final_decide(
            sanctioner(), app.pk, approve=False, reason="Not admissible",
            special_purpose_admissible=False)
        assert (done.status, done.special_purpose_admissible) == ("REJECTED", False)
        assert used("SCL") == 0

    def test_establishment_cannot_skip_the_unit_head(self):
        app = self.scl()
        with pytest.raises(ConflictError):
            decisions.establishment_route(establishment(), app.pk)

    def test_the_sanctioner_cannot_act_before_routing(self):
        app = self.scl()
        decisions.unit_head_decide(unit_head(), app.pk, decision="recommend")
        with pytest.raises(ConflictError):
            decisions.final_decide(sanctioner(), app.pk, approve=True,
                                   special_purpose_admissible=True)

    def test_each_step_needs_its_own_permission(self):
        app = self.scl()
        decisions.unit_head_decide(unit_head(), app.pk, decision="recommend")
        with pytest.raises(PermissionDeniedError):
            decisions.establishment_route(sanctioner(), app.pk)
        decisions.establishment_route(establishment(), app.pk)
        with pytest.raises(PermissionDeniedError):
            decisions.final_decide(establishment(), app.pk, approve=True)


class TestDirector:
    def test_the_director_sanctions_their_own_leave_on_the_record(self):
        director = acting(DIRECTOR, DIRECTOR_PERMS)
        app = applications.submit(director, request())
        done = decisions.director_self_sanction(director, app.pk, reason="Conference")
        assert done.status == "APPROVED"
        assert done.decided_by == DIRECTOR
        assert LeaveBalance.objects.get(user_id=DIRECTOR, leave_type="CL").used == 1
        assert done.events.last().action == "approved"

    def test_nobody_else_can_use_the_self_sanction_path(self):
        app = applications.submit(acting(DIRECTOR, DIRECTOR_PERMS), request())
        other = acting(SANCTIONER, (*SANCTION_PERMS, "elm.leave.self_sanction"))
        with pytest.raises(ConflictError):
            decisions.director_self_sanction(other, app.pk)

    def test_the_self_sanction_permission_is_required(self):
        app = applications.submit(acting(DIRECTOR, DIRECTOR_PERMS), request())
        with pytest.raises(PermissionDeniedError):
            decisions.director_self_sanction(acting(DIRECTOR), app.pk)


@freeze_time("2026-03-01")
def test_decisions_are_stamped_with_the_time():
    app = apply()
    done = decisions.unit_head_decide(unit_head(), app.pk, decision="approve")
    assert done.decided_at.date().isoformat() == "2026-03-01"
    assert LeaveApplication.objects.get(pk=app.pk).waiting_since.date().isoformat() == "2026-03-01"
