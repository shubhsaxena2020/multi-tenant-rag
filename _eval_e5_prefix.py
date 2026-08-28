"""Real before/after retrieval-quality eval for the E5 query:/passage: prefix fix (v8 #4).

Uses the ACTUAL intfloat/multilingual-e5-large model via fastembed (CPU). We build a
CONFUSABLE corpus: each question has a correct passage plus a near-duplicate distracter in
the SAME domain (this is where E5 prefixing matters most — without query:/passage: the
question and passage embeddings are not in the proper paired space and confusable items
collapse). We measure whether the correct passage outranks the distracter, with and without
prefixes. This is the empirical proof the goal demands.

Run: USE_REAL_EMBEDDER=1 .venv/bin/python _eval_e5_prefix.py
"""
from __future__ import annotations

import numpy as np

from app.embed import FastEmbedEmbedder
from app.config import get_settings


def _cos(a, b):
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def main():
    s = get_settings()
    s.use_real_embedder = True
    s.embed_model = "intfloat/multilingual-e5-large"
    s.embed_sparse_model = "prithivida/Splade_PP_en_v1"

    emb = FastEmbedEmbedder(s.embed_model, s.embed_sparse_model, "cpu", s.vector_size)

    # (question, correct_passage, confusable_distracter_in_same_domain)
    pairs = [
        ("How do I reset my account password?",
         "To reset your password, open Settings and click 'Forgot password', then follow the email link.",
         "To change your email address, open Settings and click 'Edit profile', then save the new address."),
        ("What is the refund policy for digital goods?",
         "Digital goods are refunded within 14 days if unopened, via the Billing → Refunds page.",
         "Physical goods are refunded within 30 days of delivery, via the Billing → Returns page."),
        ("How do I enable two-factor authentication?",
         "Enable 2FA under Security → Two-factor, scan the QR code with an authenticator app.",
         "Enable login alerts under Security → Notifications to get emailed on new sign-ins."),
        ("Where can I download my invoices?",
         "Download invoices from Billing → Invoices; the latest 12 months are available as PDF.",
         "Download receipts from Billing → Receipts; only the current year is retained."),
    ]

    def score(prefixed: bool):
        correct_wins = 0
        margins = []
        for q, correct, distractor in pairs:
            qt = f"query: {q}" if prefixed else q
            ct = f"passage: {correct}" if prefixed else correct
            dt = f"passage: {distractor}" if prefixed else distractor
            qv = emb.embed([qt])[0].dense
            cv = emb.embed([ct])[0].dense
            dv = emb.embed([dt])[0].dense
            sc, sd = _cos(qv, cv), _cos(qv, dv)
            if sc > sd:
                correct_wins += 1
            margins.append(sc - sd)
        return correct_wins, margins

    cw_no, m_no = score(False)
    cw_yes, m_yes = score(True)

    print("== E5 prefix eval (confusable distracters) ==")
    print(f"NO prefix   : correct ranked above distracter {cw_no}/{len(pairs)}  mean_margin={np.mean(m_no):+.3f}")
    print(f"WITH prefix : correct ranked above distracter {cw_yes}/{len(pairs)}  mean_margin={np.mean(m_yes):+.3f}")
    improved = cw_yes >= cw_no and np.mean(m_yes) > np.mean(m_no)
    print("RESULT:", "IMPROVED" if improved else "NO CHANGE",
          "| script_exit=0" if improved else "| script_exit=1")
    return 0 if improved else 1


if __name__ == "__main__":
    raise SystemExit(main())
