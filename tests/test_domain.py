import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from jjz.domain import DomainError, normalize, plan, record_type


TZ = ZoneInfo("Asia/Shanghai")


def now(day="2026-10-25", clock="09:00"):
    return datetime.fromisoformat(f"{day}T{clock}:00").replace(tzinfo=TZ)


def record(**changes):
    value = {
        "applyId": "application-old", "hphm": "冀ATEST01", "vId": "vehicle-1",
        "jjzzl": "02", "jjzzlmc": "六环外", "blztmc": "审核通过(生效中)",
        "yxqs": "2026-10-19", "yxqz": "2026-10-25", "sqsj": "2026-10-18 09:00:00",
    }
    value.update(changes)
    return value


class NormalizeTests(unittest.TestCase):
    def response(self, vehicles):
        return {"code": 200, "data": {"bzclxx": vehicles}}

    def test_exact_vehicle_and_both_arrays(self):
        other = record(hphm="冀BOTHER")
        chosen = record()
        records = normalize(self.response([
            {"hphm": "冀BOTHER", "ecbzxx": [other]},
            {"hphm": "冀ATEST01", "ecbzxx": [chosen], "bzxx": [chosen.copy()]},
        ]), "冀atest01")
        self.assertEqual(1, len(records))
        self.assertEqual("冀ATEST01", records[0]["hphm"])

    def test_parent_plate_can_be_inherited(self):
        chosen = record()
        chosen.pop("hphm")
        records = normalize(self.response([{"hphm": "冀ATEST01", "ecbzxx": [chosen]}]), "冀ATEST01")
        self.assertEqual("冀ATEST01", records[0]["hphm"])

    def test_parent_can_omit_plate(self):
        self.assertEqual(1, len(normalize(self.response([{"ecbzxx": [record()]}]), "冀ATEST01")))

    def test_null_record_arrays(self):
        self.assertEqual([], normalize(self.response([{"hphm": "冀ATEST01", "ecbzxx": None, "bzxx": None}]), "冀ATEST01"))

    def test_unknown_vehicle(self):
        with self.assertRaises(DomainError):
            normalize(self.response([]), "冀ATEST01")

    def test_invalid_shapes(self):
        for payload in ({}, {"data": None}, {"data": {"bzclxx": {}}}, self.response([None]), self.response([{"ecbzxx": {}}])):
            with self.subTest(payload=payload), self.assertRaises(DomainError):
                normalize(payload, "冀ATEST01")

    def test_parent_record_identity_conflict(self):
        with self.assertRaises(DomainError):
            normalize(self.response([{"hphm": "冀ATEST01", "ecbzxx": [record(hphm="冀BOTHER")]}]), "冀ATEST01")

    def test_conflicting_duplicate_application(self):
        with self.assertRaises(DomainError):
            normalize(self.response([{"ecbzxx": [record()], "bzxx": [record(yxqz="2026-10-26")]}]), "冀ATEST01")

    def test_same_plate_different_vehicle_ids(self):
        with self.assertRaises(DomainError):
            normalize(self.response([{"ecbzxx": [record(), record(applyId="other", vId="vehicle-2")]}]), "冀ATEST01")


class PermitTypeTests(unittest.TestCase):
    def test_code_and_label(self):
        self.assertEqual("outside", record_type({"jjzzl": "02"}))
        self.assertEqual("inside", record_type({"jjzzlmc": "进京通行证（六环内）"}))

    def test_unknown_or_conflicting_type(self):
        for value in ({}, {"jjzzl": []}, {"jjzzl": {}}, {"jjzzl": "99"},
                      {"jjzzl": "01", "jjzzlmc": "六环外"},
                      {"jjzzlmc": "六环内及六环外"},
                      {"jjzzl": "01", "jjzzlmc": "六环内及六环外"}):
            with self.subTest(value=value), self.assertRaises(DomainError):
                record_type(value)


class PlanTests(unittest.TestCase):
    def test_outside_last_day_and_year_boundary(self):
        result = plan([record(yxqs="2026-12-25", yxqz="2026-12-31")], "outside", now("2026-12-31"))
        self.assertEqual("renew", result.action)
        self.assertEqual(date(2027, 1, 1), result.apply_date)

    def test_outside_not_early_and_expired_starts_today(self):
        self.assertEqual("wait", plan([record()], "outside", now("2026-10-24")).action)
        self.assertEqual(date(2026, 10, 26), plan([record()], "outside", now("2026-10-26")).apply_date)

    def test_outside_optional_one_day_lead(self):
        result = plan([record()], "outside", now("2026-10-24"), outside_lead_days=1)
        self.assertEqual("renew", result.action)
        self.assertEqual(date(2026, 10, 26), result.apply_date)

    def test_earliest_time(self):
        self.assertEqual("wait", plan([record()], "outside", now(clock="07:59")).action)
        self.assertEqual("renew", plan([record()], "outside", now(clock="08:00")).action)

    def test_timezone_is_converted_to_shanghai(self):
        utc_now = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)
        self.assertEqual("renew", plan([record()], "outside", utc_now).action)

    def test_naive_time_is_rejected(self):
        with self.assertRaises(DomainError):
            plan([record()], "outside", datetime(2026, 10, 25, 9))

    def test_pending_of_either_type_even_with_missing_dates(self):
        pending = record(applyId="pending", jjzzl="01", jjzzlmc="六环内", blztmc="审核中", yxqs=None, yxqz=None)
        self.assertEqual("wait", plan([record(), pending], "outside", now()).action)

    def test_unknown_status_or_type_blocks(self):
        for value in (record(blztmc="新状态"), record(blztmc=[]), record(jjzzl="99", jjzzlmc="未知")):
            self.assertEqual("blocked", plan([value], "outside", now()).action)

    def test_active_missing_or_bad_validity_blocks(self):
        for changes in ({"yxqz": None}, {"yxqs": None}, {"yxqz": "garbage"}, {"yxqs": "2026-10-27"}):
            with self.subTest(changes=changes):
                self.assertEqual("blocked", plan([record(**changes)], "outside", now()).action)

    def test_latest_by_dates_not_array_order(self):
        future_expiry = record(applyId="newer", yxqs="2026-10-23", yxqz="2026-10-29", sqsj="2026-10-22 09:00:00")
        for records in ([record(), future_expiry], [future_expiry, record()]):
            result = plan(records, "outside", now())
            self.assertEqual("wait", result.action)
            self.assertEqual("newer", result.record["applyId"])

    def test_current_inside_prevents_switching_to_outside(self):
        inside = record(applyId="inside", jjzzl="01", jjzzlmc="六环内", yxqs="2026-10-23", yxqz="2026-10-29")
        self.assertEqual("wait", plan([record(), inside], "outside", now()).action)

    def test_last_day_inside_can_revert_using_outside_history(self):
        inside = record(applyId="inside", jjzzl="01", jjzzlmc="六环内")
        old_outside = record(yxqs="2026-10-01", yxqz="2026-10-07", blztmc="已过期")
        result = plan([inside, old_outside], "outside", now())
        self.assertEqual("renew", result.action)
        self.assertEqual(date(2026, 10, 26), result.apply_date)
        self.assertEqual("02", result.record["jjzzl"])

    def test_multiple_active_applications_block_when_renewal_is_due(self):
        variants = (
            record(applyId="second"),
            record(applyId="second", yxqs="2026-10-20", sqsj="2026-10-19 09:00:00"),
            record(applyId="second", jjzzl="01", jjzzlmc="六环内"),
        )
        for second in variants:
            with self.subTest(second=second):
                result = plan([record(), second], "outside", now())
                self.assertEqual("blocked", result.action)

    def test_past_active_history_is_not_a_current_conflict(self):
        historical = record(applyId="historical", yxqs="2026-10-12", yxqz="2026-10-18", sqsj="2026-10-11 09:00:00")
        self.assertEqual("renew", plan([historical, record()], "outside", now()).action)

    def test_same_application_repeated_does_not_count_as_multiple_active_permits(self):
        self.assertEqual("renew", plan([record(), record()], "outside", now()).action)

    def test_no_type_history_does_not_reuse_other_apply_id(self):
        self.assertEqual("blocked", plan([record()], "inside", now()).action)
        inside = record(jjzzl="01", jjzzlmc="六环内")
        self.assertEqual("blocked", plan([inside], "outside", now()).action)

    def test_inside_last_day_only(self):
        inside = record(jjzzl="01", jjzzlmc="六环内")
        self.assertEqual("wait", plan([inside], "inside", now("2026-10-24")).action)
        result = plan([inside], "inside", now())
        self.assertEqual(date(2026, 10, 26), result.apply_date)

    def test_inside_expired_noon_cutoff(self):
        inside = record(jjzzl="01", jjzzlmc="六环内", blztmc="已过期")
        self.assertEqual(date(2026, 10, 26), plan([inside], "inside", now("2026-10-26", "11:59")).apply_date)
        self.assertEqual(date(2026, 10, 27), plan([inside], "inside", now("2026-10-26", "12:00")).apply_date)

    def test_latest_failed_does_not_automatically_retry(self):
        failed = record(applyId="failed", blztmc="失败(审核不通过)", yxqs=None, yxqz=None, sqsj="2026-10-25 08:00:00")
        self.assertEqual("blocked", plan([record(), failed], "outside", now()).action)

    def test_provably_old_failed_record_does_not_permanently_block(self):
        failed = record(applyId="failed", blztmc="已取消", yxqs=None, yxqz=None, sqsj="2026-10-01 08:00:00")
        self.assertEqual("renew", plan([failed, record()], "outside", now()).action)

    def test_undated_failed_history_is_ambiguous(self):
        failed = record(applyId="failed", blztmc="已取消", yxqs=None, yxqz=None, sqsj=None)
        self.assertEqual("blocked", plan([failed, record()], "outside", now()).action)

    def test_empty_records_need_first_app_application(self):
        self.assertEqual("blocked", plan([], "outside", now()).action)

    def test_tied_conflicting_records_block(self):
        conflict = record(applyId="other", blztmc="已取消")
        self.assertEqual("blocked", plan([record(), conflict], "outside", now()).action)


if __name__ == "__main__":
    unittest.main()
