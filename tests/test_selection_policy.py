#!/usr/bin/env python3
import unittest
from scripts.selection_policy import SelectionInput, choose_variant


class TestSelectionPolicy(unittest.TestCase):
    def test_mapper_wins_on_latch_and_positive_sample(self):
        si = SelectionInput(mapper_header_match=0.15, mapper_sample_cons=0.01, him_sample_cons=0.00)
        out = choose_variant(si)
        self.assertEqual(out["extraction_variant"], "mapper")
        self.assertFalse(out["semantic_uncertain"]) 

    def test_mapper_zero_sample_him_low_delta_uncertain(self):
        si = SelectionInput(mapper_header_match=0.30, mapper_sample_cons=0.0, him_sample_cons=0.05)
        out = choose_variant(si)
        self.assertEqual(out["extraction_variant"], "him")
        self.assertTrue(out["semantic_uncertain"])  # Δ < 0.10 → uncertain

    def test_mapper_precedence_even_if_him_delta_ge_threshold(self):
        si = SelectionInput(mapper_header_match=0.5, mapper_sample_cons=0.10, him_sample_cons=0.21)
        out = choose_variant(si)
        # Alignment Locks: mapper wins when latch met and mapper sample_cons > 0
        self.assertEqual(out["extraction_variant"], "mapper")
        self.assertFalse(out["semantic_uncertain"]) 

    def test_uncertain_when_delta_small(self):
        si = SelectionInput(mapper_header_match=0.10, mapper_sample_cons=0.00, him_sample_cons=0.05)
        out = choose_variant(si)
        self.assertEqual(out["extraction_variant"], "him")
        self.assertTrue(out["semantic_uncertain"]) 

    def test_uncertain_tie(self):
        si = SelectionInput(mapper_header_match=0.14, mapper_sample_cons=0.04, him_sample_cons=0.04)
        out = choose_variant(si)
        self.assertEqual(out["extraction_variant"], "him")
        self.assertTrue(out["semantic_uncertain"]) 

    def test_negative_samples_behavior(self):
        si = SelectionInput(mapper_header_match=0.2, mapper_sample_cons=-0.01, him_sample_cons=-0.01)
        out = choose_variant(si)
        self.assertEqual(out["extraction_variant"], "him")
        self.assertTrue(out["semantic_uncertain"]) 


if __name__ == '__main__':
    unittest.main()
