from app.faithfulness import _tokens, is_refusal, score_faithfulness

# Test 1: No-answer/refusal
refusal_answer = "I don't know"
tokens = _tokens(refusal_answer)
print(f'Refusal answer: "{refusal_answer}"')
print(f'  Tokens: {tokens}')
print(f'  is_refusal: {is_refusal(refusal_answer)}')
result = score_faithfulness(refusal_answer, ["some context"])
print(f'  score_faithfulness: {result}')

# Test 2: Normal answer
normal_answer = 'The capital of France is Paris.'
tokens2 = _tokens(normal_answer)
print(f'Normal answer: "{normal_answer}"')
print(f'  Tokens: {tokens2}')
print(f'  is_refusal: {is_refusal(normal_answer)}')
result2 = score_faithfulness(normal_answer, ["Paris is in France"])
print(f'  score_faithfulness: {result2}')

# Test 3: Empty answer
tokens3 = _tokens('')
print(f'Empty answer tokens: {tokens3}')
print(f'  is_refusal: {is_refusal("")}')
result3 = score_faithfulness("", ["some context"])
print(f'  score_faithfulness: {result3}')