import unittest

from radar import preprocessing as pp

HEADER = ("student_number,first_name,last_name,academic_year,academic_outcome,gpa,"
          "attendance_percentage,login_frequency,date_of_birth,faculty\n")


def csv_text(*lines):
    return HEADER + "\n".join(lines) + "\n"


class PreprocessingTests(unittest.TestCase):
    def test_valid_file_is_accepted_and_typed(self):
        result = pp.prepare_csv(csv_text("S1,Ada,One,2024,0,3.2,88.5,12,2004-03-01,Informatics"))
        self.assertTrue(result["ok"], result)
        record = result["records"][0]
        self.assertEqual(record["gpa"], 3.2)
        self.assertEqual(record["academic_outcome"], 0)
        self.assertEqual(record["academic_year"], 2024)
        self.assertEqual(record["date_of_birth"], "2004-03-01")

    def test_south_african_date_format_is_normalised(self):
        result = pp.prepare_csv(csv_text("S1,Ada,One,2024,,3.2,88,12,01/03/2004,Informatics"))
        self.assertEqual(result["records"][0]["date_of_birth"], "2004-03-01")

    def test_missing_required_column_is_reported(self):
        result = pp.prepare_csv("student_number,first_name,last_name\nS1,Ada,One\n")
        self.assertFalse(result["ok"])
        self.assertIn("academic_year", result["error"])

    def test_out_of_range_values_are_listed_with_line_numbers(self):
        result = pp.prepare_csv(csv_text(
            "S1,Ada,One,2024,0,3.2,88,12,,Informatics",
            "S2,Ben,Two,2024,0,9.9,88,12,,Informatics",
        ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["records"], [])
        self.assertEqual(result["issues"][0]["row"], 3)
        self.assertEqual(result["issues"][0]["column"], "gpa")

    def test_non_binary_outcome_is_rejected(self):
        result = pp.prepare_csv(csv_text("S1,Ada,One,2024,2,3.2,88,12,,Informatics"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"][0]["column"], "academic_outcome")

    def test_exact_duplicates_are_removed_and_counted(self):
        line = "S1,Ada,One,2024,0,3.2,88,12,,Informatics"
        result = pp.prepare_csv(csv_text(line, line))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["duplicateRowsRemoved"], 1)

    def test_conflicting_rows_for_one_student_year_are_rejected(self):
        result = pp.prepare_csv(csv_text(
            "S1,Ada,One,2024,0,3.2,88,12,,Informatics",
            "S1,Ada,One,2024,1,2.0,80,9,,Informatics",
        ))
        self.assertFalse(result["ok"])
        self.assertIn("duplicates a different row", result["issues"][0]["message"])

    def test_student_profile_must_be_consistent_across_years(self):
        result = pp.prepare_csv(csv_text(
            "S1,Ada,One,2024,0,3.2,88,12,,Informatics",
            "S1,Ada,One,2025,1,2.0,80,9,,Education",
        ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"][0]["column"], "student_number")

    def test_empty_and_header_only_files(self):
        self.assertIn("empty", pp.prepare_csv("   \n")["error"])
        self.assertIn("no data rows", pp.prepare_csv(HEADER)["error"])

    def test_non_utf8_bytes_are_rejected(self):
        result = pp.prepare_csv(b"\xff\xfe\x00bad")
        self.assertFalse(result["ok"])

    def test_row_limit_is_enforced(self):
        rows = [f"S{i},A,B,2024,0,3.0,80,10,,Faculty" for i in range(pp.MAX_ROWS + 1)]
        result = pp.prepare_csv(csv_text(*rows))
        self.assertFalse(result["ok"])
        self.assertIn("limit", result["error"])

    def test_size_limit_is_enforced(self):
        result = pp.prepare_csv(b"a" * (pp.MAX_UPLOAD_BYTES + 1))
        self.assertIn("5 MB", result["error"])

    def test_blank_optional_cells_are_counted_not_guessed(self):
        result = pp.prepare_csv(csv_text(
            "S1,Ada,One,2024,,,88,12,,Informatics",
            "S2,Ben,Two,2024,0,3.0,,8,,Informatics",
        ))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["records"][0]["gpa"])
        self.assertEqual(result["missingValues"]["gpa"], 1)
        self.assertEqual(result["missingValues"]["attendance_percentage"], 1)


if __name__ == "__main__":
    unittest.main()
