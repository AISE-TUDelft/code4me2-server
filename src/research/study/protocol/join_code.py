"""Human-shareable study join codes (researcher -> participant onboarding).

A join code is a short, random, study-scoped handle that a researcher shares
with prospective participants. It is bound to exactly one *published* revision:
redeeming it enrolls the caller in that revision's study without ever letting
the client choose a revision, an eligibility verdict, or a consent document.

Design constraints:

* **Opaque and random.** The code is drawn from a CSPRNG and carries no study,
  revision or account semantics. It can therefore be printed, spoken or typed
  without leaking anything beyond "this study exists".
* **Human-typable.** The alphabet is Crockford base32 (no ``I``/``L``/``O``/``U``)
  so an 8-character code is unambiguous to read aloud and to type. Normalization
  folds the common look-alikes back to their canonical character.
* **Unique per published revision.** The database enforces uniqueness; the
  in-process allocator retries on the astronomically unlikely collision.
* **Published-only.** Drafts never get a code, so an unpublished protocol can
  never be joined.
"""

from __future__ import annotations

import re
import secrets

__all__ = [
    "JOIN_CODE_ALPHABET",
    "JOIN_CODE_LENGTH",
    "generate_join_code",
    "is_well_formed_join_code",
    "normalize_join_code",
]

#: Crockford base32: 0-9 and A-Z minus I, L, O and U.
JOIN_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Default code length. 32**8 == 2**40 possible codes.
JOIN_CODE_LENGTH = 8

_NON_CODE_CHARS = re.compile(r"[^0-9A-Z]")

# Common look-alikes a human may type instead of the canonical character.
_LOOKALIKES = str.maketrans({"I": "1", "L": "1", "O": "0", "U": "V"})


def generate_join_code(length: int = JOIN_CODE_LENGTH) -> str:
    """Return a fresh random join code.

    The code is drawn from :data:`JOIN_CODE_ALPHABET` using a CSPRNG. It is not
    derived from a study, revision or account identifier.
    """
    if length <= 0:
        raise ValueError("join code length must be positive")
    return "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(length))


def normalize_join_code(code: str) -> str:
    """Return the canonical form of a user-supplied join code.

    Whitespace, separators and lower case are folded away, and Crockford
    look-alikes (``I``/``L`` -> ``1``, ``O`` -> ``0``, ``U`` -> ``V``) are mapped
    to their canonical character. An empty/garbage input normalizes to the empty
    string, which never resolves.
    """
    if not code:
        return ""
    upper = code.strip().upper().translate(_LOOKALIKES)
    return _NON_CODE_CHARS.sub("", upper)


def is_well_formed_join_code(code: str, length: int = JOIN_CODE_LENGTH) -> bool:
    """Whether ``code`` (already normalized) is exactly a join code."""
    return len(code) == length and all(char in JOIN_CODE_ALPHABET for char in code)
