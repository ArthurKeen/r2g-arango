"""Cross-source shared-key inference (PRD P6.7).

Single-schema FK inference (P6.6, ``r2g.fk_inference``) is *name-anchored to an
in-schema parent table*: RSA's ``_candidates_for_column`` turns ``account_id``
into the prefix ``account`` and then looks for a **table named** ``account`` /
``accounts`` carrying a single-column PK. When the referenced entity lives in a
*different* source, that lookup finds nothing and the join key is invisible —
the motivating case being the ClickHouse connector (WP-CH2), where
``account_id`` is shared by ``clickhouse.query_events`` / ``usage_metrics`` but
the ``Account`` entity itself lives in the PostgreSQL CRM source.

This module adds the cross-source pass *alongside* P6.6 rather than replacing
it. Given two or more introspected schemas keyed by source name, it groups
key-shaped columns that co-occur across sources, gates them on JSON-level type
compatibility (:func:`r2g.config.pg_type_to_json_type`), and proposes a **hub**:

``entity`` hub
    Some source owns the key as a real table — either as that table's sole
    primary key, or as a column on a table whose name matches the key's entity
    prefix (``account_id`` → ``accounts``). The hub is that table, and each
    referencing table in *other* sources becomes a shared-key relationship.

``virtual`` hub
    No source owns the key as a table. The candidate proposes a *concept* (the
    federation join key) and deliberately **does not invent a table** — the
    discipline P6.7 requires.

Everything here is **confirm-to-accept**: the engine only ever proposes. Nothing
is written to a mapping, and no edge collection is materialized, until a caller
persists an accepted candidate (see :class:`r2g.types.SharedKey`). A project
maps exactly one source, so an accepted cross-source key is recorded as a
declared *join key* in the CSI / R2RML export for the federated executor to
bind-join on — not as an ArangoDB edge collection.

Nothing is dropped silently. Every column that was grouped but excluded from a
candidate is returned in :attr:`SharedKeyResult.suppressed` with a machine-
readable reason, so "no candidates" is always distinguishable from "candidates
were filtered out and you cannot see why".
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from r2g.config import pg_type_to_json_type
from r2g.log import get_logger
from r2g.types import Schema

logger = get_logger(__name__)


# Column names that carry no cross-source meaning on their own. A bare ``id``
# co-occurs in essentially every relational schema ever written, so grouping on
# it would propose a join between every pair of tables in the catalog.
DEFAULT_GENERIC_KEY_NAMES: frozenset[str] = frozenset(
    {"id", "key", "code", "uuid", "guid", "pk", "rowid", "no", "num", "seq", "hash"}
)

# Suffixes that make a column look like a *reference to* an entity rather than a
# value. P6.7 specifies ``{entity}_id``; ``_uuid`` / ``_guid`` / ``_key`` are the
# same shape under warehouse conventions.
#
# ``_code`` / ``_no`` / ``_num`` are deliberately NOT here. They match value
# columns far more often than reference columns — ``postal_code`` co-occurs
# across almost every address-bearing schema and would be proposed as a
# "Postal" hub entity, which is simply wrong: a postal code is a value, not an
# entity someone joins to. Callers who want them can extend this per-call.
_KEY_SUFFIXES: Tuple[str, ...] = ("_id", "_uuid", "_guid", "_key")

_HUB_SOLE_PK_BASE = 0.90
_HUB_NAMED_TABLE_BASE = 0.70
_HUB_VIRTUAL_BASE = 0.50


# ── Public models ────────────────────────────────────────────────────


class SharedKeyReference(BaseModel):
    """One table that carries the shared key (the *referencing* side)."""

    source: str
    table: str
    column: str
    data_type: str = ""
    json_type: str = ""
    is_nullable: bool = True
    #: Fraction of this column's sampled distinct values also present in the
    #: hub column, when a cross-source value probe ran. ``None`` when no probe
    #: was possible (no sampler for one of the two sources).
    overlap: Optional[float] = None


class HubProposal(BaseModel):
    """The entity the shared key points at.

    ``kind="entity"`` means a real table in ``source`` owns the key.
    ``kind="virtual"`` means no source owns it: the hub is a *concept* only and
    P6.7 explicitly forbids inventing a table for it.
    """

    kind: Literal["entity", "virtual"]
    #: Conceptual name (singular PascalCase), e.g. ``"Account"``.
    concept: str
    source: Optional[str] = None
    table: Optional[str] = None
    column: Optional[str] = None
    #: True when the hub column is that table's sole primary key.
    is_primary_key: bool = False
    #: Distinct-value ratio of the hub column when sampled; ``1.0`` means the
    #: column is a candidate key even though it is not the declared PK.
    distinct_ratio: Optional[float] = None


class SuppressedReference(BaseModel):
    """A grouped column that did **not** become part of a candidate.

    Reported rather than dropped so a caller can always explain the absence of a
    candidate. ``reason`` is one of ``single_source``, ``type_mismatch``,
    ``generic_name``, ``hub_table`` or ``same_source_as_hub``.
    """

    source: str
    table: str
    column: str
    key: str
    reason: str
    detail: str = ""


class SharedKeyCandidate(BaseModel):
    """A proposed cross-source shared-key relationship."""

    #: The normalized key name the group formed on, e.g. ``"account_id"``.
    key: str
    hub: HubProposal
    references: List[SharedKeyReference] = Field(default_factory=list)
    json_type: str = ""
    confidence: float = 0.0
    method: str = ""
    evidence: List[str] = Field(default_factory=list)

    @property
    def source_names(self) -> List[str]:
        """Every source involved, hub included, sorted."""
        names = {r.source for r in self.references}
        if self.hub.source:
            names.add(self.hub.source)
        return sorted(names)


class SharedKeyOptions(BaseModel):
    """Tuning knobs for :func:`infer_shared_keys`."""

    min_confidence: float = 0.4
    generic_key_names: frozenset[str] = DEFAULT_GENERIC_KEY_NAMES
    #: Require the key to appear in at least this many *distinct sources*. Two
    #: is the point of the feature; one source is P6.6's job.
    min_sources: int = 2
    #: Run bounded cross-source value probes when samplers are supplied.
    sample_overlap: bool = False
    #: Distinct values pulled per side when probing overlap.
    sample_limit: int = 1000

    model_config = {"arbitrary_types_allowed": True}


class SharedKeyResult(BaseModel):
    """Ranked candidates plus a full account of what was excluded."""

    candidates: List[SharedKeyCandidate] = Field(default_factory=list)
    suppressed: List[SuppressedReference] = Field(default_factory=list)
    sources_considered: List[str] = Field(default_factory=list)
    sampled_sources: List[str] = Field(default_factory=list)


# ── Name helpers ─────────────────────────────────────────────────────


def entity_of(column_name: str) -> Optional[str]:
    """The entity prefix a key-shaped column refers to, or ``None``.

    ``account_id`` → ``account``; ``accountId`` → ``account``; ``account`` →
    ``None`` (not key-shaped). The bare generic names are rejected by the caller
    via :attr:`SharedKeyOptions.generic_key_names`, not here.
    """
    low = column_name.strip().lower()
    if not low:
        return None
    for suffix in _KEY_SUFFIXES:
        if low.endswith(suffix) and len(low) > len(suffix):
            return low[: -len(suffix)].strip("_") or None
    # ``accountid`` / ``accountId`` (no separator).
    if low.endswith("id") and len(low) > 3:
        return low[:-2].strip("_") or None
    return None


def concept_name(entity: str) -> str:
    """Singular PascalCase concept name for an entity prefix (``account`` → ``Account``)."""
    parts = [p for p in entity.replace("-", "_").split("_") if p]
    if not parts:
        return "Entity"
    parts[-1] = _singularize(parts[-1])
    return "".join(p[:1].upper() + p[1:] for p in parts)


def _singularize(word: str) -> str:
    """Crude English singularization — enough for identifier naming."""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("ses"):
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _table_matches_entity(table_name: str, entity: str) -> bool:
    """True when ``table_name`` plausibly *is* the entity's table.

    Matches the singular and common plural forms, ignoring case and separators,
    so ``account_id`` finds ``account``, ``accounts``, or ``Account``.
    """
    norm = table_name.strip().lower().replace("-", "_")
    ent = entity.strip().lower()
    if norm == ent:
        return True
    if _singularize(norm) == _singularize(ent):
        return True
    # Schema-qualified or prefixed physical names (``crm_accounts``, ``dim_account``).
    tail = norm.rsplit("_", 1)[-1] if "_" in norm else norm
    return _singularize(tail) == _singularize(ent)


# ── Engine ───────────────────────────────────────────────────────────


class _ColumnRef(BaseModel):
    source: str
    table: str
    column: str
    data_type: str
    json_type: str
    is_nullable: bool
    is_sole_pk: bool


def infer_shared_keys(
    schemas: Mapping[str, Schema],
    *,
    options: Optional[SharedKeyOptions] = None,
    samplers: Optional[Mapping[str, Any]] = None,
) -> SharedKeyResult:
    """Propose cross-source shared-key relationships across ``schemas``.

    Args:
        schemas: ``{source_name: Schema}`` for every source to consider. At
            least two are required for any candidate to be possible.
        options: tuning knobs; see :class:`SharedKeyOptions`.
        samplers: optional ``{source_name: sampler}`` built by
            :func:`r2g.fk_inference.create_value_sampler`. Used for two bounded
            probes: hub-column uniqueness (``distinct_ratio``) and cross-source
            value overlap (``sample_values`` on each side, intersected in
            Python — a cross-database ``LEFT JOIN`` is not possible).

    Returns:
        A :class:`SharedKeyResult`. Candidates are ranked by confidence
        descending then key name; every excluded column is accounted for in
        ``suppressed``.
    """
    opts = options or SharedKeyOptions()
    samplers = samplers or {}
    suppressed: List[SuppressedReference] = []

    groups = _group_key_columns(schemas, opts, suppressed)

    candidates: List[SharedKeyCandidate] = []
    for key in sorted(groups):
        refs = groups[key]
        candidate = _build_candidate(key, refs, opts, samplers, suppressed)
        if candidate is not None and candidate.confidence >= opts.min_confidence:
            candidates.append(candidate)

    candidates.sort(key=lambda c: (-c.confidence, c.key))

    sampled = sorted(n for n in samplers if samplers.get(n) is not None) if opts.sample_overlap else []
    logger.info(
        "xsk_inference_complete",
        sources=len(schemas),
        candidates=len(candidates),
        suppressed=len(suppressed),
    )
    return SharedKeyResult(
        candidates=candidates,
        suppressed=sorted(suppressed, key=lambda s: (s.key, s.source, s.table, s.column)),
        sources_considered=sorted(schemas),
        sampled_sources=sampled,
    )


def _group_key_columns(
    schemas: Mapping[str, Schema],
    opts: SharedKeyOptions,
    suppressed: List[SuppressedReference],
) -> Dict[str, List[_ColumnRef]]:
    """Index every key-shaped column across all sources by normalized name."""
    groups: Dict[str, List[_ColumnRef]] = {}
    for source in sorted(schemas):
        schema = schemas[source]
        for table_name in sorted(schema.tables):
            table = schema.tables[table_name]
            pk = list(table.primary_key or [])
            for col in table.columns:
                key = col.name.strip().lower()
                if key in opts.generic_key_names:
                    # Reported, not silently skipped: a bare ``id`` really is a
                    # key column, it just cannot anchor a cross-source group.
                    if entity_of(col.name) is None:
                        suppressed.append(
                            SuppressedReference(
                                source=source,
                                table=table_name,
                                column=col.name,
                                key=key,
                                reason="generic_name",
                                detail="bare generic key name carries no cross-source meaning",
                            )
                        )
                    continue
                entity = entity_of(col.name)
                if entity is None or entity in opts.generic_key_names:
                    continue
                groups.setdefault(key, []).append(
                    _ColumnRef(
                        source=source,
                        table=table_name,
                        column=col.name,
                        data_type=col.data_type,
                        json_type=pg_type_to_json_type(col.data_type),
                        is_nullable=bool(col.is_nullable),
                        is_sole_pk=(len(pk) == 1 and pk[0] == col.name),
                    )
                )
    return groups


def _build_candidate(
    key: str,
    refs: Sequence[_ColumnRef],
    opts: SharedKeyOptions,
    samplers: Mapping[str, Any],
    suppressed: List[SuppressedReference],
) -> Optional[SharedKeyCandidate]:
    """Turn one grouped key into a candidate, or ``None`` with reasons recorded."""
    distinct_sources = {r.source for r in refs}
    if len(distinct_sources) < opts.min_sources:
        for r in refs:
            suppressed.append(
                SuppressedReference(
                    source=r.source,
                    table=r.table,
                    column=r.column,
                    key=key,
                    reason="single_source",
                    detail=(
                        f"'{key}' appears in only {len(distinct_sources)} source"
                        " — single-schema FK inference (P6.6) covers this case"
                    ),
                )
            )
        return None

    kept, json_type = _filter_type_compatible(key, refs, suppressed)
    if len({r.source for r in kept}) < opts.min_sources:
        return None

    entity = entity_of(key) or key
    hub, hub_ref = _choose_hub(entity, kept, samplers, opts)

    references: List[SharedKeyReference] = []
    for r in kept:
        if hub_ref is not None and r.source == hub_ref.source and r.table == hub_ref.table:
            suppressed.append(
                SuppressedReference(
                    source=r.source, table=r.table, column=r.column, key=key,
                    reason="hub_table",
                    detail="this table is the proposed hub, not a referencing side",
                )
            )
            continue
        if hub.kind == "entity" and hub.source is not None and r.source == hub.source:
            suppressed.append(
                SuppressedReference(
                    source=r.source, table=r.table, column=r.column, key=key,
                    reason="same_source_as_hub",
                    detail=(
                        f"same source as the hub ('{hub.source}') — an intra-source"
                        " FK, which P6.6 inference already proposes"
                    ),
                )
            )
            continue
        references.append(
            SharedKeyReference(
                source=r.source,
                table=r.table,
                column=r.column,
                data_type=r.data_type,
                json_type=r.json_type,
                is_nullable=r.is_nullable,
            )
        )

    if not references:
        return None

    if opts.sample_overlap:
        _probe_overlap(hub, references, samplers, opts)

    confidence, method, evidence = _score(key, hub, references, json_type)
    return SharedKeyCandidate(
        key=key,
        hub=hub,
        references=sorted(references, key=lambda r: (r.source, r.table)),
        json_type=json_type,
        confidence=confidence,
        method=method,
        evidence=evidence,
    )


def _filter_type_compatible(
    key: str,
    refs: Sequence[_ColumnRef],
    suppressed: List[SuppressedReference],
) -> Tuple[List[_ColumnRef], str]:
    """Keep the largest JSON-type-compatible cluster; report the rest.

    Ties break toward the type held by the most *sources* (not the most
    columns), so one source with many tables cannot outvote the consensus.
    """
    by_type: Dict[str, List[_ColumnRef]] = {}
    for r in refs:
        by_type.setdefault(r.json_type, []).append(r)
    if len(by_type) == 1:
        return list(refs), next(iter(by_type))

    def rank(item: Tuple[str, List[_ColumnRef]]) -> Tuple[int, int, str]:
        jtype, group = item
        return (len({g.source for g in group}), len(group), jtype)

    winner, kept = max(by_type.items(), key=rank)
    for jtype, group in by_type.items():
        if jtype == winner:
            continue
        for r in group:
            suppressed.append(
                SuppressedReference(
                    source=r.source, table=r.table, column=r.column, key=key,
                    reason="type_mismatch",
                    detail=f"JSON type '{jtype}' is incompatible with the group's '{winner}'",
                )
            )
    return kept, winner


def _choose_hub(
    entity: str,
    refs: Sequence[_ColumnRef],
    samplers: Mapping[str, Any],
    opts: SharedKeyOptions,
) -> Tuple[HubProposal, Optional[_ColumnRef]]:
    """Pick the hub for a key group.

    Preference order, each tier resolved deterministically by (source, table):

    1. the key is a table's **sole primary key** — an owned entity;
    2. a table whose **name matches the entity prefix** carries the key —
       an owned entity, optionally confirmed unique by a bounded sample;
    3. otherwise a **virtual** hub: a concept, with no table invented for it.
    """
    sole_pk = sorted(
        (r for r in refs if r.is_sole_pk), key=lambda r: (r.source, r.table)
    )
    named = sorted(
        (r for r in refs if _table_matches_entity(r.table, entity)),
        key=lambda r: (r.source, r.table),
    )
    # A sole-PK match on the entity's own table is the strongest possible signal.
    best_pk_named = next((r for r in sole_pk if _table_matches_entity(r.table, entity)), None)
    chosen = best_pk_named or (sole_pk[0] if sole_pk else None) or (named[0] if named else None)

    if chosen is None:
        return (
            HubProposal(kind="virtual", concept=concept_name(entity)),
            None,
        )

    ratio: Optional[float] = None
    if opts.sample_overlap and not chosen.is_sole_pk:
        # A non-PK hub column is only a credible entity key if it is actually
        # unique; a bounded distinct-ratio probe answers that cheaply.
        ratio = _distinct_ratio(samplers.get(chosen.source), chosen.table, chosen.column)

    return (
        HubProposal(
            kind="entity",
            concept=concept_name(entity),
            source=chosen.source,
            table=chosen.table,
            column=chosen.column,
            is_primary_key=chosen.is_sole_pk,
            distinct_ratio=ratio,
        ),
        chosen,
    )


def _distinct_ratio(sampler: Any, table: str, column: str) -> Optional[float]:
    """``COUNT(DISTINCT col) / COUNT(*)`` via the sampler, or ``None``."""
    if sampler is None or not hasattr(sampler, "distinct_ratio"):
        return None
    try:
        return sampler.distinct_ratio(table, column)
    except Exception as err:  # noqa: BLE001
        logger.warning("xsk_distinct_ratio_failed", table=table, column=column, error=str(err))
        return None


def _probe_overlap(
    hub: HubProposal,
    references: Iterable[SharedKeyReference],
    samplers: Mapping[str, Any],
    opts: SharedKeyOptions,
) -> None:
    """Fill in ``reference.overlap`` from bounded per-side value samples.

    A cross-*database* ``LEFT JOIN`` is impossible, so this pulls up to
    ``sample_limit`` distinct values from each side and intersects them in
    Python. That makes the probe **asymmetric evidence**: a high overlap is
    strong support, but a low overlap on truncated samples is *not* evidence of
    absence, so :func:`_score` only ever treats it as a boost — never a veto.
    """
    if hub.kind != "entity" or hub.source is None or hub.table is None or hub.column is None:
        return
    hub_sampler = samplers.get(hub.source)
    hub_values = _sample_values(hub_sampler, hub.table, hub.column, opts.sample_limit)
    if hub_values is None:
        return
    for ref in references:
        ref_values = _sample_values(
            samplers.get(ref.source), ref.table, ref.column, opts.sample_limit
        )
        if not ref_values:
            continue
        hits = sum(1 for v in ref_values if v in hub_values)
        ref.overlap = round(hits / len(ref_values), 4)


def _sample_values(sampler: Any, table: str, column: str, limit: int) -> Optional[set]:
    if sampler is None or not hasattr(sampler, "sample_values"):
        return None
    try:
        values = sampler.sample_values(table, column, limit=limit)
    except Exception as err:  # noqa: BLE001
        logger.warning("xsk_sample_values_failed", table=table, column=column, error=str(err))
        return None
    # Compare as text: the two sides come from different engines, so a
    # ClickHouse String and a PostgreSQL varchar must compare equal.
    return {str(v) for v in values if v is not None}


def _score(
    key: str,
    hub: HubProposal,
    references: Sequence[SharedKeyReference],
    json_type: str,
) -> Tuple[float, str, List[str]]:
    """Confidence, method label, and human-readable evidence for a candidate."""
    evidence: List[str] = []
    if hub.kind == "virtual":
        base = _HUB_VIRTUAL_BASE
        method = "virtual_hub"
        evidence.append(
            f"no source owns '{key}' as a table — proposing the concept "
            f"'{hub.concept}' as a federation join key (no table invented)"
        )
    elif hub.is_primary_key:
        base = _HUB_SOLE_PK_BASE
        method = "hub_primary_key"
        evidence.append(f"'{hub.source}.{hub.table}.{hub.column}' is the table's sole primary key")
    else:
        base = _HUB_NAMED_TABLE_BASE
        method = "hub_named_table"
        evidence.append(
            f"'{hub.source}.{hub.table}' matches the entity name for '{key}' "
            f"and carries the column"
        )

    if hub.distinct_ratio is not None:
        if hub.distinct_ratio >= 0.99:
            base += 0.10
            evidence.append(
                f"hub column is unique in sample (distinct ratio {hub.distinct_ratio:.3f})"
            )
        else:
            base -= 0.15
            evidence.append(
                f"hub column is NOT unique in sample (distinct ratio "
                f"{hub.distinct_ratio:.3f}) — weak entity key"
            )

    source_count = len({r.source for r in references} | ({hub.source} if hub.source else set()))
    if source_count >= 2:
        evidence.append(f"key spans {source_count} sources; JSON type '{json_type}' on all sides")

    overlaps = [r.overlap for r in references if r.overlap is not None]
    if overlaps:
        best = max(overlaps)
        if best >= 0.9:
            base += 0.08
            evidence.append(f"value overlap with hub up to {best:.0%} (sampled)")
        elif best >= 0.5:
            base += 0.04
            evidence.append(f"value overlap with hub up to {best:.0%} (sampled)")
        else:
            # Deliberately not a penalty: see _probe_overlap on why a low
            # overlap over truncated samples is not evidence of absence.
            evidence.append(
                f"value overlap with hub only {best:.0%} on a truncated sample "
                "— inconclusive, not counted against the candidate"
            )

    return (round(min(base, 0.99), 4), method, evidence)
