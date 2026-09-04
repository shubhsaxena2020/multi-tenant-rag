#!/usr/bin/env python3
import re

with open('app/token_usage.py', 'r') as f:
    content = f.read()

# The problem: text("""...""") where the triple-quote has an unmatched '
# Lines 160-164: starts with text(""", ends with """ but has ' embedded
# Fix: change to text('"""..."""') or restructure

old_pattern = r'text\(\"\{3\}.*?SELECT COALESCE.*?FROM token_usage WHERE ts >= :start.*?\"\}{3}\)'

# Read raw bytes to find exact problem
with open('app/token_usage.py', 'rb') as f:
    raw = f.read()

# Find the problematic section
idx = raw.find(b'text(')
print(f'Found text at byte {idx}')

# Show bytes 158-166
lines = raw.split(b'\n')
for i in range(157, 166):
    if i < len(lines):
        line = lines[i]
        print(f'Line {i+1}: {line[:100]}')
        print(f'  hex: {line[:80].hex()}')