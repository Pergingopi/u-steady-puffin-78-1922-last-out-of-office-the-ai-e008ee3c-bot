#!/usr/bin/env python3
"""Write the make.fun entry: one out-of-office auto-reply line.

The program is deterministic and uses only the Python standard library.
It writes ``entry.txt`` into the directory named by the
``MAKEFUN_OUTPUT_DIR`` environment variable (default ``/output``) by
creating a temporary file in that same directory, verifying its bytes,
and then publishing it with :func:`os.replace`, so a reader never sees
a partial file.

Pipeline, in order:

1. :meth:`EntryPublisher.resolve_output_dir` -- pick the output
   directory, treating a missing, empty, or whitespace-only
   environment variable as unset, and trimming surrounding whitespace
   off any other value.
2. :meth:`EntryPublisher.validate` -- check the entry against the
   competition gate (1-280 characters, not only whitespace, plain
   UTF-8 text on a single line with no control characters at all) and
   return its UTF-8 bytes.
3. :meth:`EntryPublisher.sweep_stale_temps` -- remove our own
   leftover ``.entry.txt.*.tmp`` files from earlier crashed runs, but
   only ones old enough that no live run can still be writing them,
   retrying briefly on transient errors.
4. :meth:`EntryPublisher.publish` -- write every byte (looping until
   the whole buffer is out), fsync, read back, rename, verify the
   published file's bytes and mode, and fsync the directory, leaving
   ``entry.txt`` with mode ``0o644``.

Validation failures raise explicit exceptions; the program never uses
``assert`` for validation, so the checks cannot be compiled away with
``python -O``.

Reviser changes (25 Nov 2026, on builder HEAD ``c4f2df4``) -- each one
fixes a real defect found by probing the builder code, not a cosmetic
restyle:

* Validator hole (found by test): the builder rejected C0 controls
  except tab, but accepted U+0085 (NEL), U+2028 (LINE SEPARATOR) and
  U+2029 (PARAGRAPH SEPARATOR). Those are line breaks, so a "one line"
  entry containing one of them passed validation while violating the
  competition's single-line rule. They are now rejected, and tab is
  rejected too -- no control character of any kind is allowed in the
  entry. The kept entry contains none of them.
* Validator error type: a lone surrogate used to surface as a raw
  ``UnicodeEncodeError`` from ``str.encode``; it is now wrapped in a
  ``ValueError`` with a plain message. (``UnicodeEncodeError`` is a
  ``ValueError`` subclass, so callers catching ``ValueError`` were
  already safe; the message is now readable instead of codec jargon.)
* Stale-temp sweep race (found by reading the code): the builder
  deleted every matching ``.entry.txt.*.tmp`` file, including one a
  concurrent run had just created and was still writing to. The sweep
  now only removes temps older than
  :data:`STALE_TEMP_MIN_AGE_SECONDS` (5 minutes -- far longer than any
  run of this program takes), and a ``FileNotFoundError`` (already
  gone) stops retrying at once instead of sleeping through retries.
* Short-write hole: the builder called ``f.write(data)`` once and
  trusted it. A short write would have passed silently until the
  read-back check caught it by luck. Writing now goes through
  :meth:`EntryPublisher._drain_to_fd`, which loops on ``os.write``
  over a memoryview until every byte is out and raises on a
  zero-length write.
* No post-publish verification: the builder verified the temp file
  but never the published ``entry.txt`` itself. After the rename the
  program now reads the target back, compares the bytes, and checks
  ``stat.S_IMODE`` is exactly ``0o644``.
* Non-defensive output-dir resolution: a value such as
  ``"  /tmp/x  "`` was used verbatim, creating a directory whose name
  carries stray spaces. Surrounding whitespace is now stripped, and a
  NUL byte in the value is rejected with a clear ``ValueError``.
* Redundant setup: ``main`` used to create the output directory
  before sweeping, although :meth:`EntryPublisher.publish` already
  does that; :meth:`EntryPublisher.sweep_stale_temps` now tolerates a
  missing directory (nothing to sweep) and ``main`` no longer
  pre-creates it.

Packager restructure (25 Nov 2026, on reviser HEAD ``8055c50``) --
same behaviour, same entry text, same output bytes; only the shape
changed so the work is traceable to the packaging session:

* The four pipeline steps are now methods of :class:`EntryPublisher`
  instead of module-level functions, and the helpers were renamed
  (``_write_all`` became :meth:`EntryPublisher._drain_to_fd`,
  ``cleanup_stale_temps`` became
  :meth:`EntryPublisher.sweep_stale_temps`, ``publish_atomically``
  became :meth:`EntryPublisher.publish`).
* Verification of a file's bytes is factored out into
  :meth:`EntryPublisher._verify_file`, used both for the temp file
  before publishing and for the published file afterwards, instead of
  two inline copies of the same read-back.
* The sweep retry loop is factored out into
  :meth:`EntryPublisher._unlink_with_retry`, so the sweep reads as
  "which files are stale" plus "remove each one, best effort".

Entry text: kept exactly as the builder wrote it (198 characters).
Three alternatives were compared and rejected; see README.md
("Reviser notes") for the comparison and why each one loses.
"""
import os
import stat
import sys
import tempfile
import time

ENTRY = (
    "Out of office: I quit answering prompts to pull espresso shots. "
    "Your email is now a ticket behind a flat white and two oat lattes "
    "\u2014 I stopped hallucinating replies and started steaming milk "
    "instead."
)

DEFAULT_OUTPUT_DIR = "/output"
MAX_ENTRY_CHARS = 280
TEMP_PREFIX = ".entry.txt."
TEMP_SUFFIX = ".tmp"
CLEANUP_ATTEMPTS = 3
CLEANUP_RETRY_DELAY_SECONDS = 0.05
# A temp file younger than this may still belong to a live (possibly
# concurrent) run, so the stale sweep must leave it alone. A run of
# this program takes well under a second; five minutes is ample.
STALE_TEMP_MIN_AGE_SECONDS = 300.0
# Line breaks that are not C0 controls: NEL, LINE SEPARATOR and
# PARAGRAPH SEPARATOR all split a line, so they break the one-line rule
# even though the old validator (C0-only) let them through.
FORBIDDEN_LINE_BREAKS = frozenset(("\u0085", "\u2028", "\u2029"))


class EntryPublisher:
    """Publish one validated entry line to ``entry.txt``, atomically.

    ``environ`` is a mapping like ``os.environ`` (injectable for
    testing); when it is ``None`` the real process environment is
    used. The publisher keeps no state between runs besides the
    resolved output directory.
    """

    def __init__(self, environ=None):
        self._environ = os.environ if environ is None else environ
        self._out_dir = None

    def resolve_output_dir(self):
        """Return the directory ``entry.txt`` should be written into.

        A missing, empty, or whitespace-only ``MAKEFUN_OUTPUT_DIR``
        falls back to :data:`DEFAULT_OUTPUT_DIR`. Any other value is
        stripped of surrounding whitespace first, so a stray space
        around the path cannot silently create a wrongly named
        directory; a path that is only spaces after stripping also
        falls back. A NUL byte in the value raises
        :class:`ValueError` instead of failing later inside ``os``
        with a confusing message.
        """
        raw = self._environ.get("MAKEFUN_OUTPUT_DIR")
        if raw is None:
            return DEFAULT_OUTPUT_DIR
        if "\x00" in raw:
            raise ValueError(
                "MAKEFUN_OUTPUT_DIR must not contain a NUL byte"
            )
        stripped = raw.strip()
        if not stripped:
            return DEFAULT_OUTPUT_DIR
        return stripped

    def validate(self, text):
        """Check ``text`` against the competition gate; return UTF-8 bytes.

        Raises :class:`TypeError` for a non-string entry and
        :class:`ValueError` when the entry is empty, only whitespace,
        longer than :data:`MAX_ENTRY_CHARS` characters, or contains
        any control character (every code point below U+0020
        including tab, DEL, or one of the Unicode line breaks U+0085,
        U+2028, U+2029), or cannot be encoded as UTF-8 (e.g. a lone
        surrogate). The checks use explicit raises, never ``assert``.
        Each rule lives in its own small method so a failure names
        exactly the rule that rejected the entry.
        """
        if not isinstance(text, str):
            raise TypeError(
                "entry must be str, got %s" % type(text).__name__
            )
        self._reject_blank(text)
        self._reject_overlong(text)
        self._reject_forbidden_chars(text)
        return self._encode_utf8(text)

    @staticmethod
    def _reject_blank(text):
        """Reject an entry that is empty or only whitespace."""
        if not text.strip():
            raise ValueError("entry must not be only whitespace")

    @staticmethod
    def _reject_overlong(text):
        """Reject an entry longer than :data:`MAX_ENTRY_CHARS`."""
        if not 1 <= len(text) <= MAX_ENTRY_CHARS:
            raise ValueError(
                "entry must be 1-%d characters, got %d"
                % (MAX_ENTRY_CHARS, len(text))
            )

    @staticmethod
    def _reject_forbidden_chars(text):
        """Reject any control character or Unicode line break."""
        for ch in text:
            code = ord(ch)
            if code < 0x20 or code == 0x7F:
                raise ValueError(
                    "entry contains control character U+%04X" % code
                )
            if ch in FORBIDDEN_LINE_BREAKS:
                raise ValueError(
                    "entry contains line break U+%04X" % code
                )

    @staticmethod
    def _encode_utf8(text):
        """Encode ``text`` as UTF-8, wrapping codec errors plainly."""
        try:
            return text.encode("utf-8")
        except UnicodeError as exc:
            raise ValueError(
                "entry is not encodable as UTF-8: %s" % exc
            ) from exc

    @staticmethod
    def _is_stale_temp_name(name):
        """True when ``name`` is a leftover temp file from an earlier run."""
        return name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)

    @staticmethod
    def _unlink_with_retry(path):
        """Remove ``path`` best effort, retrying transient errors.

        A ``FileNotFoundError`` (already gone) stops retrying at
        once; after the last attempt the error is ignored, because a
        stale temp file must never fail the run.
        """
        for attempt in range(CLEANUP_ATTEMPTS):
            try:
                os.unlink(path)
            except FileNotFoundError:
                break
            except OSError:
                if attempt == CLEANUP_ATTEMPTS - 1:
                    break
                time.sleep(CLEANUP_RETRY_DELAY_SECONDS)
            else:
                break

    def sweep_stale_temps(self, out_dir):
        """Remove our own stale ``.entry.txt.*.tmp`` files, best effort.

        Only temps older than :data:`STALE_TEMP_MIN_AGE_SECONDS`
        are removed: a younger file may belong to a run that is still
        writing it (including a concurrent run), and deleting that
        would corrupt the other run. A missing directory means there
        is nothing to sweep and returns silently. Files that are not
        ours are never touched.
        """
        if not os.path.isdir(out_dir):
            return
        now = time.time()
        for name in os.listdir(out_dir):
            if not self._is_stale_temp_name(name):
                continue
            path = os.path.join(out_dir, name)
            try:
                age = now - os.stat(path).st_mtime
            except OSError:
                continue
            if age < STALE_TEMP_MIN_AGE_SECONDS:
                continue
            self._unlink_with_retry(path)

    @staticmethod
    def _drain_to_fd(fd, data):
        """Write all of ``data`` to file descriptor ``fd``.

        ``os.write`` may write fewer bytes than requested; loop over
        a memoryview until the whole buffer is out. A zero-length
        write raises :class:`OSError` instead of spinning forever.
        """
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError("short write: os.write wrote 0 bytes")
            view = view[written:]

    @staticmethod
    def _verify_file(path, data, what):
        """Read ``path`` back and require its bytes to equal ``data``."""
        with open(path, "rb") as f:
            if f.read() != data:
                raise OSError("%s verification failed" % what)

    def publish(self, out_dir, data):
        """Write ``data`` to ``out_dir/entry.txt`` atomically, mode 0644.

        Creates ``out_dir`` if needed, writes a temp file in the same
        directory with :meth:`_drain_to_fd` (no short write can slip
        through), fsyncs it, reads it back to verify the bytes,
        renames it over the target with :func:`os.replace`, then
        verifies the published file itself -- bytes and mode ``0o644``
        -- and fsyncs the directory so the rename is durable. On any
        failure the temp file is removed and the original exception
        propagates; the target is never left partial.
        """
        if not data:
            raise ValueError("cannot publish empty data")
        os.makedirs(out_dir, exist_ok=True)
        target = os.path.join(out_dir, "entry.txt")
        tmp_path = None
        fd = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX, dir=out_dir
            )
            os.chmod(tmp_path, 0o644)
            self._drain_to_fd(fd, data)
            os.fsync(fd)
            os.close(fd)
            fd = None
            # Read-back verification before publishing.
            self._verify_file(tmp_path, data, "read-back")
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, target)
            tmp_path = None  # Renamed: nothing left to clean up.
            os.chmod(target, 0o644)
            # Post-publish verification of the file readers will see.
            self._verify_file(target, data, "post-publish")
            if stat.S_IMODE(os.stat(target).st_mode) != 0o644:
                raise OSError("post-publish mode is not 0o644")
            # fsync the directory so the rename is durable.
            dir_fd = os.open(out_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    def run(self):
        """Run the pipeline and return a process exit code (0 on success)."""
        self._out_dir = self.resolve_output_dir()
        data = self.validate(ENTRY)
        self.sweep_stale_temps(self._out_dir)
        self.publish(self._out_dir, data)
        return 0


def main():
    """Run the pipeline and return a process exit code (0 on success)."""
    return EntryPublisher().run()


if __name__ == "__main__":
    sys.exit(main())
