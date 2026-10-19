import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tass import TassClient, TassError, api_root, protect, to_students, unprotect  # noqa: E402
from tests.fake_tass import COMPANY, KEY, SECRET, FakeTass  # noqa: E402


class Tass(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTass().start()
        self.addCleanup(self.fake.stop)

    def client(self, **kw):
        args = dict(url=self.fake.url, client_key=KEY, client_secret=SECRET, company="")
        args.update(kw)
        return TassClient(**args)

    def test_address_forms(self):
        self.assertEqual(api_root("school.edu.au"), "https://school.edu.au/api")
        self.assertEqual(api_root("https://school.edu.au/"), "https://school.edu.au/api")
        self.assertEqual(api_root("https://school.edu.au/api"), "https://school.edu.au/api")

    def test_sign_in_picks_only_company(self):
        c = self.client()
        companies = c.sign_in()
        self.assertEqual(companies[0]["name_text"], "Example School")
        self.assertEqual(c.company, COMPANY)

    def test_wrong_secret(self):
        with self.assertRaises(TassError) as e:
            self.client(client_secret="nope").sign_in()
        self.assertIn("didn't accept", str(e.exception))

    def test_wrong_company(self):
        c = self.client(company="99")
        with self.assertRaises(TassError) as e:
            c.students()
        self.assertIn("permission", str(e.exception))

    def test_unreachable(self):
        c = TassClient("http://127.0.0.1:1", KEY, SECRET)
        with self.assertRaises(TassError) as e:
            c.sign_in()
        self.assertIn("Can't reach TASS", str(e.exception))

    def test_pages_of_500(self):
        rows = self.client().students()
        self.assertEqual(len(rows), 1203)
        gets = [c for c in self.fake.calls if c.startswith("GET")]
        self.assertEqual(len(gets), 3)
        self.assertIn("%24top=500", gets[0].replace("$", "%24"))
        self.assertIn("surname%20asc", gets[0])

    def test_signs_in_again_when_token_expires(self):
        c = self.client()
        c.sign_in()
        self.fake.expire_next = True
        self.assertEqual(len(c.students()), 1203)
        self.assertEqual(sum(1 for x in self.fake.calls if x.startswith("POST")), 2)

    def test_mapping_and_filters(self):
        today = dt.date(2026, 10, 19)
        rows = [
            {"stud_code": "0001", "first_name": "Andrea", "preferred_name": "Andy", "surname": "Clark",
             "preferred_surname": None, "campus_code": "NTH", "pctut_grp": "7B", "form_cls": "F", "year_grp": 7,
             "house": "Banksia", "doe": "2024-01-29", "dol": None, "privacy_flg": True},
            {"stud_code": "0002", "first_name": "Left", "surname": "Already", "campus_code": "NTH",
             "pctut_grp": "8A", "year_grp": 8, "doe": "2020-01-01", "dol": "2026-06-30"},
            {"stud_code": "0003", "first_name": "Next", "surname": "Year", "campus_code": "STH",
             "pctut_grp": "7C", "year_grp": 7, "doe": "2027-01-27", "dol": None},
            {"stud_code": "0004", "first_name": "Sam", "surname": "Lee", "campus_code": "STH",
             "pctut_grp": None, "year_grp": 9, "doe": None, "dol": "2026-12-31"},
            {"stud_code": "", "first_name": "No", "surname": "Code"},
        ]
        s = to_students(rows, today=today)
        self.assertEqual([x.number for x in s], ["0001", "0004"])
        self.assertEqual((s[0].first, s[0].last, s[0].group, s[0].campus, s[0].privacy),
                         ("Andy", "Clark", "7B", "NTH", True))
        self.assertEqual(s[1].group, "")
        self.assertEqual(to_students(rows, preferred_names=False, today=today)[0].first, "Andrea")
        self.assertEqual(to_students(rows, class_source="Year group", today=today)[0].group, "7")
        self.assertEqual([x.number for x in to_students(rows, campuses=["sth"], today=today)], ["0004"])
        self.assertEqual([x.number for x in to_students(rows, years=["7"], today=today)], ["0001"])

    def test_only_current_students_leave_tass(self):
        rows = self.fake.students
        rows[0]["dol"] = "2020-12-31"                    # left
        rows[1]["doe"] = "2099-01-28"                    # hasn't started
        rows[2]["dol"] = "2099-12-31"                    # leaving later: still current
        got = self.client().students()
        self.assertEqual(len(got), 1201)
        self.assertEqual(self.fake.rows_sent, 1201)      # past/future students were never sent
        self.assertNotIn(rows[0]["stud_code"], {r["stud_code"] for r in got})
        self.assertIn("%24filter=", "".join(self.fake.calls).replace("$", "%24"))

    def test_falls_back_when_tass_cannot_filter(self):
        self.fake.students[0]["dol"] = "2020-12-31"
        self.fake.reject_filter = True
        c = self.client()
        got = c.students()
        self.assertFalse(c.filtered_by_tass)
        self.assertEqual(len(got), 1202)                 # still current students only

    def test_secret_round_trip(self):
        stored = protect("s3cret!")
        self.assertNotIn("s3cret", stored)
        self.assertEqual(unprotect(stored), "s3cret!")
        if sys.platform.startswith("win"):
            self.assertTrue(stored.startswith("dpapi:"))
        self.assertEqual(unprotect(""), "")


if __name__ == "__main__":
    unittest.main()
