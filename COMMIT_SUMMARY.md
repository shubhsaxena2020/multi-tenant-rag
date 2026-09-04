# Fix: Factual-inversion/entity-substitution heuristic gap in faithfulness scoring

## Problem

The `score_faithfulness()` function in `app/faithfulness.py` was incorrectly penalizing natural question words (like "when", "did", "how") when computing token absence ratios. This caused queries like "When did the Phoenix project launch?" to have ~50% of tokens marked as absent (since "when" doesn't appear in retrieved context), triggering the 30%+ absence capping logic and reducing the faithfulness score deceptively.

This manifested as `test_query_faithfulness_surface` failing because `answerable` was incorrectly computed.

## Root Cause

The original code computed `absence_ratio = len(absent_tokens) / len(ans_tokens)` using ALL tokens including functional/question words. When a query naturally contains words like "when" or "did" that aren't in the context, they were counted as "absent" even though this is expected behavior for questions.

## Fix

Modified three sections in `app/faithfulness.py` to filter out functional words before computing absence ratios, consistent with the existing `_has_entity_substitution()` heuristic:

1. **Score capping penalty** (lines 263-284): Count only substantive (non-functional) tokens for the 30%+ absence threshold that caps the faithfulness score
2. **Answerability majority check** (lines 287-296): Count only substantive tokens for the >50% majority absence threshold that marks an answer as unanswerable
3. **Updated comments** to document the functional word exclusion

The functional words list matches the one already used in `_has_entity_substitution()`.

## Files Changed

- `app/faithfulness.py`: Modified `score_faithfulness()` function

## Test Results

All faithfulness-related tests pass with no regressions:
- `test_faithfulness.py`: 9/9 passed
- `test_no_answer_faithfulness.py`: 1/1 passed
- `test_adversarial_retrieval.py`: 12/12 passed
- Other retrieval and edge case tests pass