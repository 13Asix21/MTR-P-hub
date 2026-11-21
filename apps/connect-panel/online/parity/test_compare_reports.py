"""对拍比对器自身的反证：不得通过忽略业务字段制造通过。"""

from __future__ import annotations

import unittest

from compare_reports import EXPECTED_NIST, check_nist, compare, exempt


class CompareReportsTest(unittest.TestCase):
    def test_sha_path_and_size_not_exempt(self) -> None:
        self.assertFalse(exempt("listing.items[0].sha256"))
        self.assertFalse(exempt("listing.items[0].vault_relative_path"))
        self.assertFalse(exempt("listing.items[0].size_bytes"))
        self.assertTrue(exempt("listing.items[0].mtime"))

    def test_business_difference_detected(self) -> None:
        differences, _exemptions = compare({"sha256": "a"}, {"sha256": "b"})
        self.assertEqual(len(differences), 1)

    def test_missing_field_detected(self) -> None:
        differences, _exemptions = compare({"items": [{"tags": []}]}, {"items": [{}]})
        self.assertEqual(differences[0]["kind"], "missing-key")

    def test_json_numeric_zero_equivalent_but_value_not_relaxed(self) -> None:
        self.assertEqual(compare({"pass_rate": 0.0}, {"pass_rate": 0})[0], [])
        self.assertEqual(len(compare({"pass_rate": 0.5}, {"pass_rate": 0})[0]), 1)
        self.assertEqual(len(compare({"pass_rate": False}, {"pass_rate": 0})[0]), 1)

    def test_fixed_nist_one_mib_vector(self) -> None:
        self.assertEqual(EXPECTED_NIST["one_mib_zero"], "30e14955ebf1352266dc2ff8067e68104607e750abb9d3b36582b8af909fcb58")
        failures, count = check_nist({"nist_vectors": [{"name": "abc", "sha256": "0" * 64}]}, "test")
        self.assertTrue(failures)
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
