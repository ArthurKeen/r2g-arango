"""Bidirectional label dictionary — label name <-> bit position.

Standalone prototype. Imports nothing from r2g.

Two invariants make this a deployment artifact rather than a runtime detail:

* **Bit assignments are permanent.** A stored mask is meaningless without the
  dictionary that produced it, so reassigning a bit silently reinterprets every
  row ever written. Adding a label is safe; renumbering is a full rewrite. The
  class refuses to renumber rather than trusting callers to remember.
* **Both sides need the same version.** ETL encodes with it and query generation
  compiles label names into masks with it. A query built under v2 run against
  data written under v1 returns wrong rows with no error, so the version is
  stamped and checked rather than assumed.
"""

from __future__ import annotations

import json
from pathlib import Path


class LabelDictionaryError(RuntimeError):
    pass


class LabelDictionary:
    #: ArangoDB's BIT_* functions are 32-bit: BIT_AND(2**32, 2**32) returns null,
    #: silently. A vocabulary that crosses this needs mask chunking, which costs
    #: the single-scalar index property, so the boundary is enforced loudly here.
    MAX_BITS = 32

    def __init__(self, name: str, version: int = 1) -> None:
        self.name = name
        self.version = version
        self._to_bit: dict[str, int] = {}
        self._to_label: dict[int, str] = {}

    # -- construction -------------------------------------------------

    def add(self, label: str) -> int:
        """Assign the next free bit. Idempotent for an existing label."""
        if label in self._to_bit:
            return self._to_bit[label]
        pos = len(self._to_bit)
        if pos >= self.MAX_BITS:
            raise LabelDictionaryError(
                f"vocabulary exceeds {self.MAX_BITS} labels ({label!r} would be "
                f"bit {pos}); beyond this a single scalar mask cannot hold the "
                f"set and AQL's BIT_* functions return null silently"
            )
        self._to_bit[label] = pos
        self._to_label[pos] = label
        return pos

    def extend(self, labels) -> None:
        for lab in labels:
            self.add(lab)

    # -- encode / decode ----------------------------------------------

    def mask(self, labels) -> int:
        """Label set -> integer mask."""
        m = 0
        for lab in labels:
            if lab not in self._to_bit:
                raise LabelDictionaryError(
                    f"{lab!r} is not in dictionary {self.name!r} v{self.version}; "
                    f"encoding it now would assign a bit the stored data does not use"
                )
            m |= 1 << self._to_bit[lab]
        return m

    def labels(self, mask: int) -> list[str]:
        """Integer mask -> label set (the reverse direction)."""
        return [self._to_label[b] for b in range(self.MAX_BITS)
                if mask & (1 << b) and b in self._to_label]

    def bit(self, label: str) -> int:
        return 1 << self._to_bit[label]

    # -- query compilation --------------------------------------------

    def masks_containing(self, label: str) -> list[int]:
        """Every mask value in this vocabulary that includes ``label``.

        This is what makes a subset test index-servable: `bits IN [...]` is an
        equality set on an indexed scalar, unlike BIT_AND which is a function of
        the column and can only post-filter. Enumeration is 2**(n-1) values, so
        it is practical for a small vocabulary and impossible for a large one —
        `enumerable()` is the check a query planner should make.
        """
        if not self.enumerable():
            raise LabelDictionaryError(
                f"vocabulary of {len(self)} labels would enumerate to "
                f"{2 ** (len(self) - 1)} masks; use BIT_AND (post-filter) instead"
            )
        bit = self.bit(label)
        return [m for m in range(1 << len(self)) if m & bit]

    def enumerable(self, budget: int = 4096) -> bool:
        """Whether `masks_containing` stays within a sane bind-parameter size."""
        return len(self) > 0 and (1 << (len(self) - 1)) <= budget

    # -- persistence ---------------------------------------------------

    def save(self, path) -> None:
        Path(path).write_text(json.dumps({
            "name": self.name, "version": self.version,
            "bits": self._to_bit,
        }, indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path) -> "LabelDictionary":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        obj = cls(d["name"], d["version"])
        for lab, pos in sorted(d["bits"].items(), key=lambda kv: kv[1]):
            if pos != len(obj._to_bit):
                raise LabelDictionaryError(
                    f"non-contiguous bit assignment for {lab!r} (expected "
                    f"{len(obj._to_bit)}, found {pos}) — dictionary is corrupt"
                )
            obj.add(lab)
        return obj

    def __len__(self) -> int:
        return len(self._to_bit)

    def __repr__(self) -> str:
        return f"<LabelDictionary {self.name!r} v{self.version} {len(self)} labels>"
