"""Inspect no-answer and hallucination edge cases in the current harness."""

from app.faithfulness import _tokens, is_refusal, score_faithfulness, token_overlap

print("=== _tokens edge cases ===")
print("Normal:", _tokens("The capital of France is Paris"))
print("Empty:", _tokens(""))
print("Stopwords only:", _tokens("the a an"))
print("Possessive:", _tokens("dog's"))
print("Contractions:", _tokens("don't know"))

print()
print("=== is_refusal edge cases ===")
print("I don't know:", is_refusal("I don't know"))
print("I do not know:", is_refusal("I do not know"))
print("Empty:", is_refusal(""))
print("Normal:", is_refusal("The answer"))

print()
print("=== score_faithfulness edge cases ===")
print("Refusal:", score_faithfulness("I don't know", ["context"]))
print("Empty answer:", score_faithfulness("", ["context"]))
print("Normal with context:", score_faithfulness("Paris is in France", ["France"]))
print("Normal with no overlap:", score_faithfulness("Quantum physics", ["Biology text"]))

print()
print("=== token_overlap edge cases ===")
print("Full overlap:", token_overlap("Paris", "Paris is capital"))
print("No overlap:", token_overlap("Quantum", "Biology text"))
print("Empty answer:", token_overlap("", "context"))