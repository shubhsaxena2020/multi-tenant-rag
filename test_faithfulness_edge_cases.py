"""Edge case diagnostics for faithfulness module — no-answer and hallucination."""

from app.faithfulness import score_faithfulness, token_overlap, is_refusal

# Edge case: answer with only stopwords
print("=== Only stopwords in answer ===")
score, answerable = score_faithfulness("the a an and or but", ["context with real content"])
print("score=%s, answerable=%s" % (score, answerable))

# Edge case: partial token overlap
print("=== Partial token overlap ===")
score, answerable = score_faithfulness("the cat sat", ["the cat sat on the mat"])
print("score=%s, answerable=%s" % (score, answerable))

# Edge case: hallucination — answer contradicts context but has some overlap
print("=== Hallucination with partial overlap ===")
score, answerable = score_faithfulness("The sky is green", ["The sky is blue"])
print("score=%s, answerable=%s" % (score, answerable))

# Edge case: entities not in context
print("=== Entities not in context ===")
score, answerable = score_faithfulness("Apple released iPhone 15", ["Microsoft announced Surface"])
print("score=%s, answerable=%s" % (score, answerable))

# is_refusal edge cases
print("=== is_refusal edge cases ===")
print("is_refusal(\"I cannot help\") = %s" % is_refusal("I cannot help"))
print("is_refusal(\"I can't help\") = %s" % is_refusal("I can't help"))
print("is_refusal(\"I am sorry\") = %s" % is_refusal("I am sorry"))
print("is_refusal(\"No information available\") = %s" % is_refusal("No information available"))
print("is_refusal(\"I have no idea\") = %s" % is_refusal("I have no idea"))
print("is_refusal(None) = %s" % is_refusal(None))
print("is_refusal(\"\") = %s" % is_refusal(""))

# Zero token overlap with empty context
print("=== Zero token overlap with empty context ===")
score, answerable = score_faithfulness("something", [])
print("score=%s, answerable=%s" % (score, answerable))