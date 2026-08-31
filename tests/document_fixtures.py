#!/usr/bin/env python3
"""Sample fixtures for tricky document classes (Task 9).

These fixtures cover document types that present challenges for the
ingestion/chunking pipeline, including nested structures, mixed content,
and edge cases. Each fixture is a markdown string that can be passed
to chunk_structured() for testing.
"""

# 1. Nested headings — deep heading hierarchy
NARROW_HEADING_FIXTURE = """# Level 1

## Level 2

### Level 3

Text under level 3.

## Back to level 2

Text under level 2 back.

# Back to level 1

Text under level 1 again."""

# 2. Mixed content: headings, tables, code fences, lists, definitions
MIXED_CONTENT_FIXTURE = """# Document with mixed content

Some intro text.

## Section A

### Subsection A1

| A | B |
|---|---|
| 1 | 2 |

CODE:python:def foo():
    return 1 + 2

A list:
- item1
- item2
- item3

## Section B

### Subsection B1

CODE:javascript:var x = 1;

Definition:
term1: Definition 1
term2: Definition 2

More text here.

# Conclusion

Final thoughts."""

# 3. Wide table with many columns
WIDE_TABLE_FIXTURE = """# Wide table document

| Col A | Col B | Col C | Col D | Col E |
|-------|-------|-------|-------|-------|
| Row1-ColA | Row1-ColB | Row1-ColC | Row1-ColD | Row1-ColE |
| Row2-ColA | Row2-ColB | Row2-ColC | Row2-ColD | Row2-ColE |
| Row3-ColA | Row3-ColB | Row3-ColC | Row3-ColD | Row3-ColE |

More content after the table.

## Next section

More content here."""

# 4. Code fences with different languages - using ~~~ to avoid quote issues
CODE_FENCES_FIXTURE = """# Code examples

## Python

~~~python
def calculate_mean(numbers):
    if not numbers:
        return 0
    return sum(numbers) / len(numbers)

result = calculate_mean([1, 2, 3, 4, 5])
print(f"Mean: {result}")
~~~

## Bash

~~~bash
#!/bin/bash
echo "Hello, world!"
for i in {1..5}; do
    echo "Iteration: $i"
done
~~~

## HTML

~~~html
<!DOCTYPE html>
<html>
<head><title>Test</title></head>
<body><p>Hello world</p></body>
</html>
~~~"""

# 5. Document with only tables (no headings)
TABLE_ONLY_FIXTURE = """| Name | Age | Occupation |
|------|-----|-------------|
| Alice | 30 | Engineer |
| Bob | 25 | Designer |
| Carol | 35 | Manager |
| David | 42 | Analyst |

Inline text after the table.

| City | Country |
|------|---------|
| NY | USA |
| SF | USA |
| LA | USA"""

# 6. Definition list heavy document
DEFINITIONS_FIXTURE = """# Glossary

## Terms

api: A software interface that allows two applications to communicate with each other.

http: The foundation of data communication for the World Wide Web.

json: A lightweight data-interchange format that is easy for humans to read and write.

cpu: The central processing unit of a computer, executing instructions.

memory: Computer data storage (RAM) that is accessed electronically.

## More terms

algorithm: A step-by-step procedure for solving a problem or performing a computation.

datatype: A classification that specifies which type of value a variable can have.

framework: A supported platform for developing software applications."""

# 7. Document with headings, code, and table combined
COMBINED_FIXTURE = """# Combined document

This document combines all tricky elements.

## Features

- Heading hierarchy
- Code blocks
- Tables
- Definitions

## Table of features

| Feature | Description |
|---------|-------------|
| Headings | Document structure |
| Code | Programming examples |
| Tables | Data representation |
| Definitions | Glossary terms |

~~~python
def complex_function(x):
    if x > 0:
        return x * 2
    return 0
~~~

More content following the code block.

## Summary

All tricky elements tested successfully."""

# 8. Very long single heading document
LONG_HEADING_FIXTURE = """# Overview

This is a very long document overview that spans multiple paragraphs
and contains significant content. The purpose of this document is to
test how the chunking infrastructure handles long-form technical writing
with multiple sections, subsections, and various content types interspersed
throughout the text body.

## Introduction

The introduction section provides context for what follows. It may contain
multiple paragraphs of explanatory text, definitions of key terms, and
an outline of the document structure that will be covered in subsequent
sections.

### Sub-section introduction

Some sub-section content under the introduction.

## Main content

The main content area covers the primary topics of the document. This
includes detailed explanations, examples, and illustrations of the core
concepts being documented.

### Sub-section main content

Detailed sub-section content under main.

## Conclusions

The conclusions section summarizes the key findings and takeaways from
the document. It may restate important points, provide final thoughts,
and suggest areas for further research or exploration.

### Sub-section conclusions

Sub-final thoughts and summary points."""

# 9. Document with embedded images (markdown syntax)
IMAGES_FIXTURE = """# Document with images

![Diagram 1](diagram1.png)

![Diagram 2](diagram2.png)

Some text referencing images above.

## Section with image

![Captioned diagram](diagram3.png{width=500})

More text."""

# 10. Empty and minimal documents
MINIMAL_FIXTURES = {
    "empty": "",
    "single_paragraph": "Just a single paragraph of text.",
    "single_heading": "# Just a heading",
    "single_table": "| A | B |\n|---|---|\n| 1 | 2 |",
    "single_code": "~~~python\nprint('hello')\n~~~",
}

# 11. Document with horizontal rules
HR_FIXTURE = """# Document with horizontal rules

Text before the horizontal rule.

---

Text between horizontal rules.

---

Text after the second horizontal rule.

## New section after rules

Content following the rules."""

# 12. Document with blockquotes
BLOCKQUOTE_FIXTURE = """# Document with blockquotes

> This is a blockquote
> spanning multiple lines.

> It can contain markdown formatting **bold** and *italic*.

## Following section

Normal content after the blockquote."""

# 13. Nested lists with different levels
NESTED_LISTS_FIXTURE = """# Document with nested lists

## Ordered list

1. First item
2. Second item
   a. Nested sub-item 1
   b. Nested sub-item 2
3. Third item

## Unordered list

- Item A
- Item B
  - Sub-item B1
  - Sub-item B2

## Mixed list

1. Item one
2. Item two
   - Sub-item under two
3. Item three"""

# 14. Document with math (LaTeX style)
MATH_FIXTURE = """# Document with math

Some text with inline math: $E = mc^2$ and $a^2 + b^2 = c^2$.

## Section with display math

$$\int_{-\infty}^{\infty} e^{-x^2} dx = \sqrt{\pi}$$

More text discussing the mathematical results.

### Inline math continuation

The formula $F = ma$ is Newton's second law.

## Summary

Mathematical notation handled inline and in display mode."""

# 15. Unicode and special characters document
UNICODE_FIXTURE = """# Document with unicode

## International text

This document contains text in multiple languages:
- English: The quick brown fox jumps over the lazy dog.
- Spanish: El perro rápido marrones sobre el perro perezoso.
- Français: Le renard brun rapide saute par-dessus le chien paresseux.
- 日本語: 速い茶色の狐が lazy 犬の上に飛び跳ねます。

## Symbols

Common symbols: © ® ™ § ¶ † ‡ •

## Emoji

 smile: 😊 laugh: 😄 cry: 😢"""

# 16. Document with metadata-rich table
METADATA_TABLE_FIXTURE = """# Document with metadata table

| id | name | type | status | created | version |
|----|------|------|--------|---------|---------|
| 001 | Alice | user | active | 2024-01-15 | 1.0.0 |
| 002 | Bob | admin | pending | 2024-02-20 | 1.1.0 |
| 003 | Carol | user | active | 2024-03-10 | 1.2.0 |
| 004 | Dave | guest | revoked | 2024-04-05 | 2.0.0 |

## Notes

Table above contains sample metadata records.

~~~python
# Sample code referencing table data
records = get_records("metadata_table")
for r in records:
    print(f"{r['name']}: {r['status']}")
~~~"""

# 17. Document with footnote-style references
FOOTNOTE_FIXTURE = """# Document with footnotes

Text with a footnote reference^[This is a footnote.].

More text with another reference^[Another footnote.].

## Section with more footnotes

Text^[First] more text^[Second].

## Final section

Final paragraph with no footnotes."""

# 18. Very wide table (many columns)
WIDE_COLUMNS_FIXTURE = """# Wide column table

| Col1 | Col2 | Col3 | Col4 | Col5 | Col6 | Col7 | Col8 | Col9 | Col10 |
|------|------|------|------|------|------|------|------|------|-------|
| Val1 | Val2 | Val3 | Val4 | Val5 | Val6 | Val7 | Val8 | Val9 | Val10 |
| A | B | C | D | E | F | G | H | I | J |
| 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |

## End of wide table section."""

# 19. Document starting with table (no heading before)
TABLE_FIRST_FIXTURE = """| A | B | C |
|---|---|---|
| 1 | 2 | 3 |
| 4 | 5 | 6 |

# Heading after table

Content after the table and heading.

## Subsection

More content."""

# 20. Document with citation-style references
CITATIONS_FIXTURE = """# Document with citations

As stated in [Ref1], the theory holds. More details in [Ref2] and [Ref3].

## Related work

[Ref1]: Author A. (2020). Title A. Journal A.

[Ref2]: Author B. (2021). Title B. Journal B.

[Ref3]: Author C. (2022). Title C. Journal C.

## Conclusion

The citations above support the main claims."""

# Fixtures dictionary for easy access
DOCUMENT_FIXTURES = {
    "narrow_heading": NARROW_HEADING_FIXTURE,
    "mixed_content": MIXED_CONTENT_FIXTURE,
    "wide_table": WIDE_TABLE_FIXTURE,
    "code_fences": CODE_FENCES_FIXTURE,
    "table_only": TABLE_ONLY_FIXTURE,
    "definitions": DEFINITIONS_FIXTURE,
    "combined": COMBINED_FIXTURE,
    "long_heading": LONG_HEADING_FIXTURE,
    "images": IMAGES_FIXTURE,
    "minimal": MINIMAL_FIXTURES,
    "horizontal_rules": HR_FIXTURE,
    "blockquotes": BLOCKQUOTE_FIXTURE,
    "nested_lists": NESTED_LISTS_FIXTURE,
    "math": MATH_FIXTURE,
    "unicode": UNICODE_FIXTURE,
    "metadata_table": METADATA_TABLE_FIXTURE,
    "footnotes": FOOTNOTE_FIXTURE,
    "wide_columns": WIDE_COLUMNS_FIXTURE,
    "table_first": TABLE_FIRST_FIXTURE,
    "citations": CITATIONS_FIXTURE,
}