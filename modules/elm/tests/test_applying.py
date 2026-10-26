"""WF-EL-01 up to the first decision, and WF-EL-02: apply, modify, withdraw."""
from decimal import Decimal

import pytest

from core.api.exceptions import ConflictError, NotFoundError
from modules.elm.models import (
    ClosedHoliday,
    LeaveApplication,
    LeaveBalance,
    LeaveNotification,
    RestrictedHoliday,
    VacationPeriod,
)
from modules.elm.services import applications
from modules.elm.services.common import RuleBroken
from modules.elm.tests.support import (
    DIRECTOR,
    DIRECTOR_PERMS,
    EMPLOYEE,
    PEER,
    STAFF_MEMBER,
    STUDENT,
    UNIT_HEAD,
    D,
    acting,
    request,
)

pytestmark = pytest.mark.django_db


def submit(user=EMPLOYEE, **kwargs):
    return applications.submit(acting(user), request(**kwargs))


class TestSubmit:
    def test_a_valid_cl_waits_for_the_unit_head(self):
        app = submit()
        assert app.status == "AWAITING_UNIT_HEAD"
        assert app.days == Decimal(1)
        assert app.applicant_kind == "faculty"
        assert app.events.get().action == "submitted"

    def test_the_unit_heads_queue_is_told(self):
        app = submit()
        note = LeaveNotification.objects.get(application=app)
        assert note.recipient_permission == "elm.leave.review"

    def test_balances_are_created_for_the_kind_of_employee(self):
        submit()
        types = set(LeaveBalance.objects.filter(user_id=EMPLOYEE).values_list(
            "leave_type", flat=True))
        assert types == {"CL", "RH", "SCL", "EL", "COL", "VL"}
        staff = set(applications.balances.ensure_balances(STAFF_MEMBER, 2026, "staff"))
        assert {b.leave_type for b in staff} == {"CL", "RH", "SCL", "EL", "COL"}

    def test_only_employees_may_apply(self):
        with pytest.raises(RuleBroken) as caught:
            applications.submit(acting(STUDENT), request())
        assert caught.value.details == [{"rule": "BR-EL-001"}]

    def test_staff_cannot_take_vacation_leave(self):
        with pytest.raises(RuleBroken):
            applications.submit(acting(STAFF_MEMBER), request("VL"))

    def test_the_director_goes_to_self_sanction(self):
        app = applications.submit(acting(DIRECTOR, DIRECTOR_PERMS), request())
        assert app.status == "AWAITING_DIRECTOR_SELF_SANCTION"
        assert app.is_director

    def test_cl_cannot_cross_a_calendar_year(self):
        with pytest.raises(RuleBroken):
            submit(start=D(2026, 12, 30), end=D(2027, 1, 2))

    def test_station_leave_needs_an_address(self):
        with pytest.raises(RuleBroken):
            submit(station_leave=True)
        app = submit(station_leave=True, station_address="Home, Pune")
        assert app.station_leave

    def test_a_reversed_range_is_refused(self):
        with pytest.raises(RuleBroken):
            submit(start=D(2026, 3, 6), end=D(2026, 3, 2))


class TestBalanceLimits:
    def test_cl_cannot_exceed_eight_days(self):
        with pytest.raises(RuleBroken):
            submit(start=D(2026, 3, 2), end=D(2026, 3, 13))

    def test_requests_still_waiting_count_against_the_balance(self):
        submit(start=D(2026, 3, 2), end=D(2026, 3, 6))
        with pytest.raises(RuleBroken):
            submit(start=D(2026, 3, 9), end=D(2026, 3, 13))

    def test_a_withdrawn_request_frees_its_days(self):
        first = submit(start=D(2026, 3, 2), end=D(2026, 3, 6))
        applications.withdraw(acting(EMPLOYEE), first.pk)
        submit(start=D(2026, 3, 9), end=D(2026, 3, 13))

    def test_staff_get_thirty_el_and_faculty_none(self):
        applications.submit(acting(STAFF_MEMBER), request("EL", D(2026, 4, 1), D(2026, 4, 30)))
        with pytest.raises(RuleBroken):
            submit(leave_type="EL", start=D(2026, 4, 1), end=D(2026, 4, 2))

    def test_el_counts_weekends_but_cl_does_not(self):
        el = applications.submit(
            acting(STAFF_MEMBER), request("EL", D(2026, 3, 6), D(2026, 3, 9)))
        cl = submit(start=D(2026, 3, 6), end=D(2026, 3, 9))
        assert (el.days, cl.days) == (Decimal(4), Decimal(2))

    def test_closed_holidays_do_not_count_for_cl(self):
        ClosedHoliday.objects.create(date=D(2026, 3, 4), name="Holi")
        assert submit(start=D(2026, 3, 2), end=D(2026, 3, 6)).days == Decimal(4)

    def test_scl_stops_at_fifteen_days_a_year(self):
        submit(leave_type="SCL", start=D(2026, 3, 2), end=D(2026, 3, 13))
        with pytest.raises(RuleBroken) as caught:
            submit(leave_type="SCL", start=D(2026, 4, 6), end=D(2026, 4, 13))
        assert caught.value.details == [{"rule": "BR-EL-004"}]

    def test_scl_is_never_marked_admissible_by_applying(self):
        app = submit(leave_type="SCL")
        assert app.special_purpose_admissible is None


class TestCalendarRules:
    def test_rh_needs_a_published_date(self):
        with pytest.raises(RuleBroken):
            submit(leave_type="RH", start=D(2026, 3, 4))
        RestrictedHoliday.objects.create(
            date=D(2026, 3, 4), year=2026, name="Holi", is_published=False)
        with pytest.raises(RuleBroken):
            submit(leave_type="RH", start=D(2026, 3, 4))
        RestrictedHoliday.objects.filter(date=D(2026, 3, 4)).update(is_published=True)
        assert submit(leave_type="RH", start=D(2026, 3, 4)).days == Decimal(1)

    def test_only_two_rh_a_year(self):
        for day in (4, 5, 6):
            RestrictedHoliday.objects.create(
                date=D(2026, 3, day), year=2026, name=f"RH{day}", is_published=True)
        submit(leave_type="RH", start=D(2026, 3, 4))
        submit(leave_type="RH", start=D(2026, 3, 5))
        with pytest.raises(RuleBroken):
            submit(leave_type="RH", start=D(2026, 3, 6))

    def test_vl_needs_a_published_vacation(self):
        with pytest.raises(RuleBroken):
            submit(leave_type="VL", start=D(2026, 5, 4), end=D(2026, 5, 8))
        VacationPeriod.objects.create(
            name="Summer", start_date=D(2026, 5, 1), end_date=D(2026, 7, 31))
        assert submit(leave_type="VL", start=D(2026, 5, 4), end=D(2026, 5, 8)).days == 5

    def test_col_needs_a_medical_reference(self):
        with pytest.raises(RuleBroken):
            submit(leave_type="COL", start=D(2026, 3, 2), end=D(2026, 3, 4))
        app = submit(leave_type="COL", start=D(2026, 3, 2), end=D(2026, 3, 4),
                     medical_reference="MC-14")
        assert app.medical_reference == "MC-14"


class TestOverlap:
    def test_overlapping_leave_is_refused(self):
        submit(start=D(2026, 3, 2), end=D(2026, 3, 4))
        with pytest.raises(RuleBroken) as caught:
            submit(start=D(2026, 3, 4), end=D(2026, 3, 5))
        assert caught.value.details == [{"rule": "BR-EL-012"}]

    def test_two_half_days_in_different_halves_are_fine(self):
        submit(half_day="FIRST")
        submit(half_day="SECOND")

    def test_the_same_half_twice_is_refused(self):
        submit(half_day="FIRST")
        with pytest.raises(RuleBroken):
            submit(half_day="FIRST")

    def test_a_full_day_over_a_half_day_is_refused(self):
        submit(half_day="FIRST")
        with pytest.raises(RuleBroken):
            submit()

    def test_finished_leave_does_not_block_new_leave(self):
        first = submit()
        applications.withdraw(acting(EMPLOYEE), first.pk)
        submit()

    def test_other_peoples_leave_is_irrelevant(self):
        submit()
        submit(user=PEER)


class TestSubstitute:
    def test_a_named_substitute_must_accept_first(self):
        app = submit(substitute_user_id=PEER)
        assert app.status == "AWAITING_SUBSTITUTE"
        note = LeaveNotification.objects.get(application=app)
        assert note.recipient_user_id == PEER
        assert note.topic == "leave.substitute_requested"

    def test_cannot_name_oneself(self):
        with pytest.raises(RuleBroken):
            submit(substitute_user_id=EMPLOYEE)

    def test_cannot_name_a_student_or_a_stranger(self):
        with pytest.raises(RuleBroken):
            submit(substitute_user_id=STUDENT)
        with pytest.raises(RuleBroken):
            submit(substitute_user_id=424242)

    def test_substitute_with_leave_in_the_period_is_refused(self):
        submit(user=PEER, start=D(2026, 3, 2))
        with pytest.raises(RuleBroken):
            submit(substitute_user_id=PEER)


class TestModify:
    def test_modifying_revalidates_and_resets_the_state(self):
        app = submit(substitute_user_id=PEER)
        changed = applications.modify(
            acting(EMPLOYEE), app.pk, request(start=D(2026, 3, 3), substitute_user_id=None))
        assert changed.start_date == D(2026, 3, 3)
        assert changed.status == "AWAITING_UNIT_HEAD"
        assert [e.action for e in changed.events.all()] == ["submitted", "modified"]

    def test_an_invalid_change_leaves_the_original_untouched(self):
        app = submit()
        with pytest.raises(RuleBroken):
            applications.modify(acting(EMPLOYEE), app.pk,
                                request(start=D(2026, 3, 2), end=D(2026, 3, 20)))
        app.refresh_from_db()
        assert app.end_date == D(2026, 3, 2)

    def test_a_change_may_overlap_only_with_itself(self):
        app = submit()
        applications.modify(acting(EMPLOYEE), app.pk, request(end=D(2026, 3, 3)))

    def test_cannot_modify_once_a_higher_authority_has_it(self):
        app = submit()
        LeaveApplication.objects.filter(pk=app.pk).update(status="AWAITING_FINAL_SANCTION")
        with pytest.raises(ConflictError):
            applications.modify(acting(EMPLOYEE), app.pk, request())

    def test_cannot_modify_someone_elses_application(self):
        app = submit()
        with pytest.raises(NotFoundError):
            applications.modify(acting(PEER), app.pk, request())

    def test_a_declined_substitute_can_be_replaced(self):
        app = submit(substitute_user_id=PEER)
        LeaveApplication.objects.filter(pk=app.pk).update(status="SUBSTITUTE_DECLINED")
        changed = applications.modify(
            acting(EMPLOYEE), app.pk, request(substitute_user_id=STAFF_MEMBER))
        assert changed.status == "AWAITING_SUBSTITUTE"
        assert changed.substitute_user_id == STAFF_MEMBER


class TestWithdraw:
    def test_withdrawal_is_immediate_and_closes_the_task(self):
        app = submit()
        done = applications.withdraw(acting(EMPLOYEE), app.pk)
        assert done.status == "WITHDRAWN"
        topics = set(LeaveNotification.objects.filter(application=app).values_list(
            "topic", flat=True))
        assert "leave.withdrawn" in topics

    def test_the_substitute_is_told(self):
        app = submit(substitute_user_id=PEER)
        applications.withdraw(acting(EMPLOYEE), app.pk)
        assert LeaveNotification.objects.filter(
            application=app, recipient_user_id=PEER, topic="leave.withdrawn").exists()

    def test_only_the_owner_can_withdraw(self):
        app = submit()
        with pytest.raises(NotFoundError):
            applications.withdraw(acting(UNIT_HEAD), app.pk)

    def test_an_approved_leave_cannot_be_withdrawn(self):
        app = submit()
        LeaveApplication.objects.filter(pk=app.pk).update(status="APPROVED")
        with pytest.raises(ConflictError):
            applications.withdraw(acting(EMPLOYEE), app.pk)

    def test_withdrawing_twice_is_refused(self):
        app = submit()
        applications.withdraw(acting(EMPLOYEE), app.pk)
        with pytest.raises(ConflictError):
            applications.withdraw(acting(EMPLOYEE), app.pk)
