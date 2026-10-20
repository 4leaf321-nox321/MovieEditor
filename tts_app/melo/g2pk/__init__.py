"""Korean grapheme-to-phoneme rules, vendored from g2pkk 0.1.2 (Apache License 2.0, see LICENSE).

https://github.com/harmlessman/g2pkk, itself a fork of g2pK by Kyubyong Park.
Changes from upstream: package-relative imports, and G2p takes the MeCab tagger and the
CMU dictionary as arguments instead of pip-installing a tagger and downloading cmudict at
runtime; four regex literals became raw strings (same patterns, no SyntaxWarning). The\npronunciation rules and their data files are unchanged.
"""

from .g2pkk import G2p

__all__ = ["G2p"]
