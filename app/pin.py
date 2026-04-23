"""
PIN hashing, validation and rate-limit constants for v2 mooring PIN
protection.

Design notes
------------

- A PIN is exactly six numeric digits (000000..999999). Format is
  validated server-side in is_valid_pin_format() and should also be
  enforced by the UI.

- Hashes are SHA-256 over (salt + pin), where salt is the site-wide
  PIN_HASH_SALT from config. The hash is deterministic: given a salt
  and a PIN, hash_pin() always produces the same output. This is a
  deliberate choice - it lets an admin precompute a hash for a known
  PIN (e.g. "000000") and write it directly to the database as a
  reset mechanism, without needing per-row salts.

- The constants below encode the rate-limit policy. They are read by
  app.main's PIN dependency and passed through to
  app.database.record_failed_pin_attempt, rather than being duplicated
  in the database module. The database module treats them as pure
  parameters.

- Constant-time comparison is used in verify_pin(). This is mostly
  defensive - the threat model does not include timing-attack-capable
  adversaries - but it costs nothing and removes a foot-gun.

Security caveats
----------------

A six-digit PIN has 1,000,000 possible values. Any scheme - salted or
not, fast or slow hash - is brute-forceable by an attacker with
filesystem access to the database AND knowledge of the salt. On
commodity hardware, SHA-256 over one million candidates completes in
under a second. PIN protection here is intended to deter casual
misuse (one user accidentally editing another's mooring through the
web UI), not to resist a determined attacker with server access. See
README for a fuller statement of the threat model.
"""

import hashlib
import hmac

from app.config import PIN_HASH_SALT

# --- Rate limit policy ---
# How many failed PIN attempts are permitted against a single mooring
# within PIN_ATTEMPT_WINDOW_MINUTES before the mooring is locked for
# PIN_LOCKOUT_MINUTES. Successful PIN verification resets the counter.
MAX_PIN_ATTEMPTS = 5
PIN_ATTEMPT_WINDOW_MINUTES = 10
PIN_LOCKOUT_MINUTES = 15


class PinConfigError(RuntimeError):
    """Raised when PIN operations are attempted without a configured salt."""


def _require_salt() -> str:
    """Return the configured salt, or raise if it is empty."""
    if not PIN_HASH_SALT:
        raise PinConfigError(
            "PIN_HASH_SALT is not configured. Set it in .env before "
            "performing any PIN operation."
        )
    return PIN_HASH_SALT


def is_valid_pin_format(pin) -> bool:
    """
    Return True iff pin is exactly six numeric digits, no leading or
    trailing whitespace, no other characters. Non-string inputs are
    rejected.
    """
    if not isinstance(pin, str):
        return False
    if len(pin) != 6:
        return False
    return pin.isdigit()


def hash_pin(pin: str) -> str:
    """
    Compute SHA-256 over (salt + pin) and return the hex digest as a
    lowercase 64-character string.

    Raises PinConfigError if the site salt is not configured, and
    ValueError if pin is not in the expected 6-digit format. Callers
    are expected to have validated format before calling, but this
    second check prevents accidentally hashing malformed input.
    """
    if not is_valid_pin_format(pin):
        raise ValueError("PIN must be exactly six numeric digits")
    salt = _require_salt()
    h = hashlib.sha256()
    h.update(salt.encode("utf-8"))
    h.update(pin.encode("utf-8"))
    return h.hexdigest()


def verify_pin(pin: str, stored_hash: str) -> bool:
    """
    Return True iff hash_pin(pin) matches stored_hash, using a
    constant-time comparison to avoid leaking timing information
    about how many leading characters matched.

    Returns False rather than raising if either input is malformed
    (invalid PIN format, empty or non-string stored hash) - the
    intent is that verify_pin(user_input, stored) is safe to call
    on any user input without pre-validation. Format errors in the
    stored hash itself indicate a corrupt database and are still
    caught here as a simple mismatch.
    """
    if not isinstance(stored_hash, str) or not stored_hash:
        return False
    if not is_valid_pin_format(pin):
        return False
    try:
        computed = hash_pin(pin)
    except (ValueError, PinConfigError):
        return False
    return hmac.compare_digest(computed, stored_hash)
