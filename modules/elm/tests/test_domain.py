"""The pure rules. No database, no Django: each test names the BR it holds up."""
import datetime as dt
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from modules.elm.domain import balances, calculation, eligibility, sla, workflow
from modules.elm.domain import leave_types as lt
from modules.elm.domain.calculation import Period
from modules.elm.domain.errors import RuleViolation

D = dt.date
MON, FRI, NEXT_MON, SAT = D(2026, 3, 2), D(2026, 3, 6), D(2026, 3, 9), D(2026, 3, 14)


class TestLeaveDays:
    def test_cl_skips_weekends_inside_the_range(self):
        # BR-EL-011: weekends count only for EL, COL and VL.
        assert calculation.leave_days(lt.CL, FRI, NEXT_MON, lt.NO_HALF) == Decimal(2)

    def test_cl_skips_closed_holidays(self):
        days = calculation.leave_days(lt.CL, MON, FRI, lt.NO_HALF, closed_days=[D(2026, 3, 4)])
        assert days == Decimal(4)

    @pytest.mark.parametrize("kind", [lt.EL, lt.COL, lt.VL])
    def test_continuing_leave_counts_every_calendar_day(self, kind):
        assert calculation.leave_days(kind, FRI, NEXT_MON, lt.NO_HALF) == Decimal(4)

    def test_half_day_cl_costs_half(self):
        assert calculation.leave_days(lt.CL, MON, MON, lt.FIRST_HALF) == Decimal("0.5")

    @pytest.mark.parametrize("kind", [lt.SCL, lt.EL, lt.COL, lt.VL, lt.RH])
    def test_only_cl_may_be_a_half_day(self, kind):
        with pytest.raises(RuleViolation) as caught:
            calculation.leave_days(kind, MON, MON, lt.FIRST_HALF)
        assert caught.value.rule == "BR-EL-010"

    def test_half_day_must_be_one_date(self):
        with pytest.raises(RuleViolation):
            calculation.leave_days(lt.CL, MON, FRI, lt.SECOND_HALF)

    def test_half_day_cannot_fall_on_a_weekend(self):
        with pytest.raises(RuleViolation):
            calculation.leave_days(lt.CL, SAT, SAT, lt.FIRST_HALF)

    def test_end_before_start_is_refused(self):
        with pytest.raises(RuleViolation):
            calculation.leave_days(lt.CL, FRI, MON, lt.NO_HALF)

    def test_a_request_with_no_working_day_is_refused(self):
        with pytest.raises(RuleViolation):
            calculation.leave_days(lt.CL, SAT, D(2026, 3, 15), lt.NO_HALF)

    def test_rh_is_a_single_day(self):
        assert calculation.leave_days(lt.RH, MON, MON, lt.NO_HALF) == Decimal(1)
        with pytest.raises(RuleViolation):
            calculation.leave_days(lt.RH, MON, FRI, lt.NO_HALF)

    def test_a_request_cannot_span_more_than_a_year(self):
        with pytest.raises(RuleViolation):
            calculation.leave_days(lt.EL, D(2026, 1, 1), D(2027, 3, 1), lt.NO_HALF)


class TestOverlap:
    def test_different_halves_of_one_day_do_not_overlap(self):
        a, b = Period(MON, MON, lt.FIRST_HALF), Period(MON, MON, lt.SECOND_HALF)
        assert not calculation.overlaps(a, b)

    def test_same_half_overlaps(self):
        a = Period(MON, MON, lt.FIRST_HALF)
        assert calculation.overlaps(a, Period(MON, MON, lt.FIRST_HALF))

    def test_half_day_inside_a_full_range_overlaps(self):
        assert calculation.overlaps(Period(MON, MON, lt.FIRST_HALF), Period(MON, FRI))

    def test_adjacent_ranges_do_not_overlap(self):
        assert not calculation.overlaps(Period(MON, FRI), Period(D(2026, 3, 7), NEXT_MON))

    def test_shared_end_day_overlaps(self):
        assert calculation.overlaps(Period(MON, FRI), Period(FRI, NEXT_MON))

    def test_find_overlap_returns_the_clash(self):
        existing = [Period(MON, FRI)]
        assert calculation.find_overlap(Period(FRI, FRI), existing) == existing[0]
        assert calculation.find_overlap(Period(NEXT_MON, NEXT_MON), existing) is None


class TestEligibility:
    def test_faculty_may_take_vl_but_staff_may_not(self):
        eligibility.check_category(lt.FACULTY, lt.VL)
        with pytest.raises(RuleViolation) as caught:
            eligibility.check_category(lt.STAFF, lt.VL)
        assert caught.value.rule == "BR-EL-001"

    def test_students_are_not_eligible_for_anything(self):
        with pytest.raises(RuleViolation):
            eligibility.check_category("student", lt.CL)

    def test_rh_must_be_a_published_date(self):
        eligibility.check_restricted_holiday(MON, {MON})
        with pytest.raises(RuleViolation):
            eligibility.check_restricted_holiday(FRI, {MON})

    def test_vl_must_sit_inside_a_vacation(self):
        eligibility.check_vacation_periods(MON, FRI, [(D(2026, 3, 1), D(2026, 3, 31))])
        with pytest.raises(RuleViolation):
            eligibility.check_vacation_periods(MON, FRI, [(MON, D(2026, 3, 4))])

    def test_vl_may_span_two_adjacent_vacations(self):
        periods = [(MON, D(2026, 3, 4)), (D(2026, 3, 5), FRI)]
        eligibility.check_vacation_periods(MON, FRI, periods)

    def test_substitute_cannot_be_the_applicant(self):
        with pytest.raises(RuleViolation):
            eligibility.check_substitute(1, 1, substitute_is_employee=True,
                                         substitute_has_overlap=False)

    def test_substitute_must_be_an_employee_without_leave(self):
        with pytest.raises(RuleViolation):
            eligibility.check_substitute(1, 2, substitute_is_employee=False,
                                         substitute_has_overlap=False)
        with pytest.raises(RuleViolation):
            eligibility.check_substitute(1, 2, substitute_is_employee=True,
                                         substitute_has_overlap=True)
        eligibility.check_substitute(1, 2, substitute_is_employee=True,
                                     substitute_has_overlap=False)

    def test_col_needs_medical_evidence(self):
        with pytest.raises(RuleViolation):
            eligibility.check_medical_evidence(lt.COL, "  ")
        eligibility.check_medical_evidence(lt.COL, "MC-2026-14")
        eligibility.check_medical_evidence(lt.CL, "")


class TestBalances:
    def test_remaining_counts_carry_in_and_ignores_nothing(self):
        left = balances.remaining(Decimal(30), Decimal(5), Decimal(10), Decimal(2))
        assert left == Decimal(23)

    def test_cannot_ask_for_more_than_remains(self):
        balances.check_balance(Decimal(3), Decimal(3))
        with pytest.raises(RuleViolation):
            balances.check_balance(Decimal("3.5"), Decimal(3))

    def test_scl_ceiling_is_fifteen_a_year(self):
        balances.check_scl_ceiling(Decimal(5), Decimal(10))
        with pytest.raises(RuleViolation):
            balances.check_scl_ceiling(Decimal(6), Decimal(10))

    @pytest.mark.parametrize("kind", [lt.CL, lt.RH, lt.SCL])
    def test_cl_rh_scl_lapse(self, kind):
        plan = balances.plan_year_end(kind, Decimal(3))
        assert (plan.lapse, plan.carry_forward, plan.el_credit) == (3, 0, 0)

    @pytest.mark.parametrize("kind", [lt.EL, lt.COL])
    def test_el_and_col_carry_forward(self, kind):
        plan = balances.plan_year_end(kind, Decimal(12))
        assert (plan.lapse, plan.carry_forward) == (0, 12)

    def test_vl_converts_two_to_one_and_does_not_carry(self):
        plan = balances.plan_year_end(lt.VL, Decimal(25))
        assert plan.lapse == Decimal(25)
        assert plan.el_credit == Decimal("12.5")
        assert plan.carry_forward == 0

    def test_an_overdrawn_balance_plans_nothing_negative(self):
        assert balances.plan_year_end(lt.CL, Decimal(-1)).lapse == 0

    def test_yearly_credit_by_kind(self):
        assert lt.credit_for(lt.FACULTY)[lt.VL] == 60
        assert lt.credit_for(lt.FACULTY)[lt.EL] == 0
        assert lt.credit_for(lt.STAFF)[lt.EL] == 30
        assert lt.VL not in lt.credit_for(lt.STAFF)
        assert all(lt.credit_for(k)[lt.CL] == 8 for k in (lt.FACULTY, lt.STAFF))


class TestWorkflow:
    def test_first_state(self):
        assert workflow.first_state(substitute_required=True, is_director=False) \
            == workflow.AWAITING_SUBSTITUTE
        assert workflow.first_state(substitute_required=False, is_director=False) \
            == workflow.AWAITING_UNIT_HEAD
        assert workflow.first_state(substitute_required=False, is_director=True) \
            == workflow.AWAITING_DIRECTOR

    def test_substitute_gate_leads_to_the_right_next_step(self):
        assert workflow.after_substitute_accepts(False) == workflow.AWAITING_UNIT_HEAD
        assert workflow.after_substitute_accepts(True) == workflow.AWAITING_DIRECTOR

    def test_unit_head_finalises_only_cl_and_rh(self):
        assert workflow.unit_head_may_decide(lt.CL) and workflow.unit_head_may_decide(lt.RH)
        assert not any(workflow.unit_head_may_decide(t)
                       for t in (lt.SCL, lt.EL, lt.COL, lt.VL))

    def test_establishment_step_is_optional(self):
        assert workflow.after_unit_head_recommendation(establishment_step=True) \
            == workflow.AWAITING_ESTABLISHMENT
        assert workflow.after_unit_head_recommendation(establishment_step=False) \
            == workflow.AWAITING_FINAL

    def test_a_refused_cancellation_leaves_the_leave_standing(self):
        assert workflow.final_state(approved=False, cancelling=True) == workflow.APPROVED
        assert workflow.final_state(approved=True, cancelling=True) == workflow.CANCELLED
        assert workflow.final_state(approved=False, cancelling=False) == workflow.REJECTED

    def test_only_the_right_actor_can_make_a_move(self):
        workflow.check_move(workflow.AWAITING_UNIT_HEAD, workflow.APPROVED, workflow.UNIT_HEAD)
        with pytest.raises(RuleViolation):
            workflow.check_move(workflow.AWAITING_FINAL, workflow.REJECTED, workflow.UNIT_HEAD)
        with pytest.raises(RuleViolation):
            workflow.check_move(workflow.AWAITING_UNIT_HEAD, workflow.AWAITING_FINAL,
                                workflow.EMPLOYEE)
        with pytest.raises(RuleViolation):
            workflow.check_move(workflow.APPROVED, workflow.WITHDRAWN, workflow.EMPLOYEE)

    def test_every_pending_state_can_be_withdrawn_but_only_some_modified(self):
        for state in workflow.PENDING:
            workflow.check_move(state, workflow.WITHDRAWN, workflow.EMPLOYEE)
        assert workflow.AWAITING_FINAL not in workflow.MODIFIABLE
        assert workflow.AWAITING_UNIT_HEAD in workflow.MODIFIABLE

    def test_finished_states_have_no_exit(self):
        leaving = {m.frm for m in workflow.MOVES}
        assert not (leaving & {workflow.REJECTED, workflow.WITHDRAWN, workflow.CANCELLED,
                               workflow.COMPLETED})


class TestSla:
    SINCE = datetime(2026, 3, 2, 9, 0)
    REMIND, ESCALATE = timedelta(hours=48), timedelta(hours=96)

    def _due(self, hours, **flags):
        return sla.due_action(
            waiting_since=self.SINCE, now=self.SINCE + timedelta(hours=hours),
            remind_after=self.REMIND, escalate_after=self.ESCALATE,
            reminded=flags.get("reminded", False), escalated=flags.get("escalated", False))

    def test_nothing_before_the_reminder_threshold(self):
        assert self._due(47) is None

    def test_reminder_at_the_threshold_once(self):
        assert self._due(48) == sla.REMIND
        assert self._due(60, reminded=True) is None

    def test_escalation_after_its_threshold_once(self):
        assert self._due(96, reminded=True) == sla.ESCALATE
        assert self._due(120, reminded=True, escalated=True) is None

    def test_a_task_never_reminded_still_escalates(self):
        assert self._due(200) == sla.ESCALATE
