#!/usr/bin/env python3
"""Group alerts about the same event across outlets, and compute first-mover lag.

This is the money view from the brief: when five outlets push the same story,
who went first and by how many minutes?

ALGORITHM
---------
Single-link agglomerative clustering constrained by a time window:

  1. Sort alerts by posted_at.
  2. For each alert, compare it only against clusters whose span it would
     keep within `window` minutes (default 90), measured from the cluster's
     FIRST alert. Measuring from the first alert rather than the most recent
     is deliberate: it bounds the total span of any cluster at `window`, which
     is what "the same event within a ~90 minute window" actually means, and
     it prevents single-link chaining (A near B, B near C, C near D ... until
     one "cluster" spans a whole day of unrelated morning briefings).
  3. If similarity >= threshold, join that cluster. Otherwise start a new one.
  4. One alert per outlet per cluster: if an outlet pushes an update on the
     same story, the earliest one counts for lag purposes and the rest are
     recorded as follow-ups.

SIMILARITY IS PLUGGABLE
-----------------------
`SimilarityBackend` is the interface. Two implementations ship:

  TfidfBackend      (default) -- pure stdlib. TF-IDF cosine over character-
                    and word-level tokens, blended with a proper-noun /
                    number overlap score. No API key, no model download,
                    works offline, deterministic.
  EmbeddingBackend  -- stub wired for a future embeddings call. Raises a
                    clear NotImplementedError with instructions rather than
                    silently degrading.

Swap with `--backend embedding` or `set_backend()`.

WHY TF-IDF PLUS ENTITY OVERLAP, NOT TF-IDF ALONE
------------------------------------------------
Push copy is short (5-15 words) and stylistically divergent across outlets.
"SCOTUS DEALS BLOW TO TARIFF PLAN" and "Supreme Court rejects emergency
tariffs" share almost no tokens. What they do share is the salient entities.
Weighting rare capitalised tokens and numbers separately recovers exactly
those matches, which is where the interesting cross-outlet pairs live.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Protocol, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

DEFAULT_WINDOW_MIN = 90
DEFAULT_THRESHOLD = 0.30

# Words that carry no discriminating signal in push copy.
STOPWORDS = frozenset("""
a an the and or but if then than that this these those of in on at to for from
with without by as is are was were be been being it its it's his her their our
your my he she they we you i has have had do does did will would can could
should may might must not no nor so such own same too very just now new news
breaking update live latest report says said after before over under about
into more most out up down off again once here there when where why how all
any both each few other some only own s t don now watch read see live
""".split())

_WORD = re.compile(r"[A-Za-z0-9']+")
_PROPER = re.compile(r"\b([A-Z][a-zA-Z]{2,})\b")
_NUMBER = re.compile(r"\b(\d[\d,.]*)\b")
_ALLCAPS = re.compile(r"\b([A-Z]{2,})\b")


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    """Lowercased content words, stopwords removed, light suffix stripping."""
    if not text:
        return []
    toks = []
    for w in _WORD.findall(text.lower()):
        w = w.strip("'")
        if len(w) < 2 or w in STOPWORDS:
            continue
        # Crude stemming: enough to match plural/singular and -ed/-ing forms
        # without pulling in a dependency.
        for suf in ("'s", "es", "s", "ed", "ing"):
            if len(w) > 4 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        toks.append(w)
    return toks


def _stem(w: str) -> str:
    for suf in ("'s", "es", "s", "ed", "ing"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def entities(text: str) -> set[str]:
    """Salient entity-ish tokens: proper nouns, ALLCAPS, and numbers.

    Deliberately naive -- no NER model. The first word of a sentence is
    capitalised too, but the stopword filter removes most of those, and the
    remaining noise is symmetric across outlets so it does not bias the lag.

    Entities are stemmed with the same rule as `tokenize`, so that Fox's
    "TARIFF PLAN" and the NYT's "Emergency Tariffs" register as the same
    entity. Without this, headline-case and all-caps variants of the same
    noun never match.
    """
    if not text:
        return set()
    out: set[str] = set()
    for m in _PROPER.findall(text):
        low = m.lower()
        if low not in STOPWORDS:
            out.add(_stem(low))
    for m in _ALLCAPS.findall(text):
        low = m.lower()
        if low not in STOPWORDS and len(low) > 1:
            out.add(_stem(low))
    out.update(_NUMBER.findall(text))
    return out


# ---------------------------------------------------------------------------
# Similarity backends
# ---------------------------------------------------------------------------

class SimilarityBackend(Protocol):
    name: str

    def prepare(self, docs: Sequence[str]) -> None:
        """Called once with the whole corpus, so IDF can be computed."""

    def similarity(self, i: int, j: int) -> float:
        """Similarity in [0,1] between corpus documents i and j."""


class TfidfBackend:
    """TF-IDF cosine blended with entity overlap. Pure stdlib, deterministic."""

    name = "tfidf"

    def __init__(self, entity_weight: float = 0.45):
        self.entity_weight = entity_weight
        self._vecs: list[dict[str, float]] = []
        self._norms: list[float] = []
        self._ents: list[set[str]] = []

    def prepare(self, docs: Sequence[str]) -> None:
        tokenised = [tokenize(d) for d in docs]
        self._ents = [entities(d) for d in docs]
        n = max(len(docs), 1)

        df: Counter[str] = Counter()
        for toks in tokenised:
            df.update(set(toks))
        # Smoothed IDF; +1 keeps a term appearing in every doc from going to 0.
        idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}

        self._vecs = []
        self._norms = []
        for toks in tokenised:
            tf = Counter(toks)
            total = sum(tf.values()) or 1
            vec = {t: (c / total) * idf.get(t, 1.0) for t, c in tf.items()}
            self._vecs.append(vec)
            self._norms.append(math.sqrt(sum(v * v for v in vec.values())) or 1.0)

    def _cosine(self, i: int, j: int) -> float:
        a, b = self._vecs[i], self._vecs[j]
        if len(a) > len(b):
            a, b = b, a
        dot = sum(v * b.get(t, 0.0) for t, v in a.items())
        return dot / (self._norms[i] * self._norms[j])

    def _entity_overlap(self, i: int, j: int) -> float:
        a, b = self._ents[i], self._ents[j]
        if not a or not b:
            return 0.0
        # Overlap coefficient, not Jaccard: a short alert that is a strict
        # subset of a longer one should score high, and Jaccard punishes that.
        return len(a & b) / min(len(a), len(b))

    def similarity(self, i: int, j: int) -> float:
        if i == j:
            return 1.0
        cos = self._cosine(i, j)
        ent = self._entity_overlap(i, j)
        return (1 - self.entity_weight) * cos + self.entity_weight * ent


class EmbeddingBackend:
    """Placeholder for a future embedding-based backend.

    Intentionally raises rather than falling back, so that a run configured
    for embeddings never silently produces TF-IDF numbers that get reported as
    embedding results.

    To implement: embed each doc once in `prepare()` (Voyage, or any local
    sentence-transformer), store the unit vectors, and make `similarity()` a
    dot product. The rest of this module needs no changes.
    """

    name = "embedding"

    def prepare(self, docs: Sequence[str]) -> None:
        raise NotImplementedError(
            "EmbeddingBackend is a stub. Implement prepare()/similarity() with your "
            "embedding provider, or run with --backend tfidf (the default, no API key "
            "needed)."
        )

    def similarity(self, i: int, j: int) -> float:  # pragma: no cover
        raise NotImplementedError


BACKENDS = {"tfidf": TfidfBackend, "embedding": EmbeddingBackend}


def get_backend(name: str = "tfidf") -> SimilarityBackend:
    try:
        return BACKENDS[name]()
    except KeyError:
        raise SystemExit(f"unknown backend {name!r}; choose from {sorted(BACKENDS)}")


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

@dataclass
class AlertRef:
    id: int
    outlet: str
    title: str
    body: str
    posted_at: datetime
    text: str


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt


def build_clusters(
    alerts: Sequence[AlertRef],
    backend: SimilarityBackend | None = None,
    window_min: int = DEFAULT_WINDOW_MIN,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[list[int]]:
    """Return a list of clusters, each a list of indices into `alerts`."""
    if not alerts:
        return []
    backend = backend or TfidfBackend()
    backend.prepare([a.text for a in alerts])

    order = sorted(range(len(alerts)), key=lambda i: alerts[i].posted_at)
    window = timedelta(minutes=window_min)

    clusters: list[list[int]] = []
    # cluster index -> earliest posted_at in it. The window is measured from
    # here, so no cluster can ever span more than `window` minutes.
    first_at: list[datetime] = []

    for idx in order:
        a = alerts[idx]
        best_c, best_score = -1, 0.0
        for ci, members in enumerate(clusters):
            if a.posted_at - first_at[ci] > window:
                continue
            # Single-link: the best match against any member wins.
            score = max(backend.similarity(idx, m) for m in members)
            if score >= threshold and score > best_score:
                best_c, best_score = ci, score
        if best_c >= 0:
            clusters[best_c].append(idx)
        else:
            clusters.append([idx])
            first_at.append(a.posted_at)

    return _merge_pass(alerts, clusters, backend, window_min, threshold)


def _merge_pass(
    alerts: Sequence[AlertRef],
    clusters: list[list[int]],
    backend: SimilarityBackend,
    window_min: int,
    threshold: float,
) -> list[list[int]]:
    """Second pass: merge clusters that should have been one.

    WHY THIS EXISTS. The greedy first pass processes alerts in time order and
    can only join a cluster that already exists. Consider an illustrative
    case: Outlet A pushes at 14:57, Outlet B at 14:59, Outlet C at 15:00.
    Outlet B's copy is all-caps tabloid style and scores only 0.20 against
    Outlet A -- below threshold -- so Outlet B opens its own cluster.
    Outlet C then arrives and matches Outlet A at 0.62, joining it. But
    Outlet B and Outlet C score 0.36 against each other, above threshold.
    Outlet B is stranded in a singleton purely because it arrived before the
    alert it matches best.

    That is not a modelling opinion, it is an artefact of arrival order, and
    it would systematically under-count follow-on outlets whose house style
    diverges from the first mover -- exactly the outlets the analysis cares
    about. So we run single-link agglomerative merging to a fixed point.
    """
    window = timedelta(minutes=window_min)
    merged = True
    while merged and len(clusters) > 1:
        merged = False
        for ci in range(len(clusters)):
            for cj in range(ci + 1, len(clusters)):
                span = [alerts[i].posted_at for i in clusters[ci] + clusters[cj]]
                # The MERGED cluster must still fit inside the window. Testing
                # the total span (not the gap between the two clusters) is what
                # stops merges from chaining into ever-widening clusters.
                if max(span) - min(span) > window:
                    continue
                best = max(
                    backend.similarity(i, j)
                    for i in clusters[ci]
                    for j in clusters[cj]
                )
                if best >= threshold:
                    clusters[ci].extend(clusters[cj])
                    del clusters[cj]
                    merged = True
                    break
            if merged:
                break

    return clusters


def cluster_label(alerts: Sequence[AlertRef], members: Sequence[int]) -> str:
    """A short human label: the earliest alert's title, trimmed."""
    first = min(members, key=lambda i: alerts[i].posted_at)
    title = (alerts[first].title or alerts[first].body or "").strip()
    return (title[:110] + "…") if len(title) > 110 else title


def first_mover_table(
    alerts: Sequence[AlertRef], members: Sequence[int]
) -> list[dict]:
    """Per-outlet lag in minutes behind the first mover in this cluster.

    An outlet that pushed the same story twice is counted once, at its
    earliest push; later pushes are marked as follow-ups.
    """
    by_outlet: dict[str, list[int]] = defaultdict(list)
    for i in members:
        by_outlet[alerts[i].outlet].append(i)

    firsts = {
        outlet: min(idxs, key=lambda i: alerts[i].posted_at)
        for outlet, idxs in by_outlet.items()
    }
    t0 = min(alerts[i].posted_at for i in firsts.values())

    rows = []
    for outlet, i in firsts.items():
        rows.append(
            {
                "outlet": outlet,
                "alert_id": alerts[i].id,
                "title": alerts[i].title,
                "posted_at": alerts[i].posted_at.isoformat(),
                "lag_min": round((alerts[i].posted_at - t0).total_seconds() / 60, 1),
                "followups": len(by_outlet[outlet]) - 1,
            }
        )
    rows.sort(key=lambda r: r["lag_min"])
    return rows


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_alerts(conn, since: str | None = None, include_synthetic: bool = True) -> list[AlertRef]:
    sql = "SELECT id, outlet, title, body, posted_at FROM alerts WHERE posted_at IS NOT NULL"
    params: list = []
    if since:
        sql += " AND posted_at >= ?"
        params.append(since)
    if not include_synthetic:
        sql += " AND synthetic = 0"
    sql += " ORDER BY posted_at"

    out = []
    for r in conn.execute(sql, params):
        ts = _parse_ts(r["posted_at"])
        if ts is None:
            continue
        title = r["title"] or ""
        body = r["body"] or ""
        out.append(
            AlertRef(
                id=r["id"],
                outlet=r["outlet"] or "unknown",
                title=title,
                body=body,
                posted_at=ts,
                # Title is weighted double: it is the part editors actually
                # write for the alert, and bodies are often boilerplate.
                text=f"{title} {title} {body}".strip(),
            )
        )
    return out


def run(
    db_path: str | None = None,
    window_min: int = DEFAULT_WINDOW_MIN,
    threshold: float = DEFAULT_THRESHOLD,
    backend_name: str = "tfidf",
    since: str | None = None,
    include_synthetic: bool = True,
) -> dict:
    db.init_db(db_path)
    backend = get_backend(backend_name)

    with db.connect(db_path) as conn:
        alerts = load_alerts(conn, since=since, include_synthetic=include_synthetic)
        if not alerts:
            return {"alerts": 0, "clusters": 0, "multi_outlet": 0}

        clusters = build_clusters(alerts, backend, window_min, threshold)
        now = db.utcnow()
        multi = 0

        conn.execute("DELETE FROM clusters")
        for n, members in enumerate(clusters):
            outlets = {alerts[i].outlet for i in members}
            first_i = min(members, key=lambda i: alerts[i].posted_at)
            cid = f"c{alerts[first_i].posted_at.strftime('%Y%m%dT%H%M')}-{n:04d}"
            if len(outlets) > 1:
                multi += 1
            for i in members:
                conn.execute("UPDATE alerts SET cluster_id=? WHERE id=?", (cid, alerts[i].id))
            conn.execute(
                """INSERT OR REPLACE INTO clusters
                   (cluster_id, label, first_outlet, first_at, size, computed_at, method)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    cid,
                    cluster_label(alerts, members),
                    alerts[first_i].outlet,
                    alerts[first_i].posted_at.isoformat(),
                    len(members),
                    now,
                    f"{backend.name}/w{window_min}/t{threshold}",
                ),
            )

    return {
        "alerts": len(alerts),
        "clusters": len(clusters),
        "multi_outlet": multi,
        "backend": backend.name,
    }


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cluster alerts and compute first-mover lag")
    ap.add_argument("--db")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW_MIN, help="minutes")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--backend", default="tfidf", choices=sorted(BACKENDS))
    ap.add_argument("--since", help="ISO timestamp lower bound")
    ap.add_argument("--exclude-synthetic", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)

    res = run(
        db_path=args.db,
        window_min=args.window,
        threshold=args.threshold,
        backend_name=args.backend,
        since=args.since,
        include_synthetic=not args.exclude_synthetic,
    )
    print(
        f"clustered {res['alerts']} alerts into {res['clusters']} clusters "
        f"({res['multi_outlet']} span more than one outlet)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
