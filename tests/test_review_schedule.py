import unittest
from datetime import date

from cuoti_app.services.review_schedule import advance_review, initial_review_date, reopen_review


class ReviewScheduleTests(unittest.TestCase):
    def test_fixed_four_stage_cycle(self):
        day = date(2028, 2, 29)
        state = advance_review(result="pass", review_stage=0, review_cycle=1, reviewed_on=day)
        self.assertEqual((state.review_stage, state.next_review), (1, "2028-03-05"))
        state = advance_review(result="pass", review_stage=1, review_cycle=1, reviewed_on="2028-03-05")
        self.assertEqual((state.review_stage, state.next_review), (2, "2028-03-15"))
        state = advance_review(result="pass", review_stage=2, review_cycle=1, reviewed_on="2028-03-15")
        self.assertEqual((state.review_stage, state.next_review), (3, "2028-03-30"))
        state = advance_review(result="pass", review_stage=3, review_cycle=1, reviewed_on="2028-03-30")
        self.assertEqual((state.status, state.correct_streak, state.next_review), ("已掌握", 4, None))

    def test_again_and_manual_reopen_start_new_cycle_after_three_days(self):
        reset = advance_review(result="again", review_stage=2, review_cycle=4, reviewed_on="2026-12-31")
        self.assertEqual((reset.review_stage, reset.review_cycle, reset.next_review), (0, 5, "2027-01-03"))
        reopened = reopen_review(review_cycle=5, reopened_on="2027-01-31")
        self.assertEqual((reopened.review_stage, reopened.review_cycle, reopened.next_review), (0, 6, "2027-02-03"))

    def test_new_mistake_starts_three_days_after_local_date(self):
        self.assertEqual(initial_review_date("2026-12-30"), "2027-01-02")


if __name__ == "__main__":
    unittest.main()
