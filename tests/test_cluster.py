"""Tests for clustering and first-mover lag. No emulator, no network."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import cluster  # noqa: E402
from cluster import AlertRef, TfidfBackend, build_clusters, first_mover_table  # noqa: E402

T0 = datetime(2026, 9, 2, 14, 57, tzinfo=timezone.utc)


def mk(idx: int, outlet: str, title: str, body: str = "", minutes: int = 0) -> AlertRef:
    return AlertRef(
        id=idx,
        outlet=outlet,
        title=title,
        body=body,
        posted_at=T0 + timedelta(minutes=minutes),
        text=f"{title} {title} {body}".strip(),
    )


# Four outlets covering one story in four house styles, plus an unrelated promo.
SCOTUS = [
    mk(1, "ABC News", "BREAKING: Supreme Court strikes down emergency tariffs",
       "Justices rule 6-3 that the president exceeded authority under IEEPA.", 0),
    mk(2, "Fox News", "SCOTUS DEALS BLOW TO TARIFF PLAN",
       "High court rules 6-3 against emergency trade authority", 2),
    mk(3, "CNN", "Supreme Court rules on tariff case",
       "The 6-3 decision limits presidential authority over emergency trade powers.", 3),
    mk(4, "The New York Times", "Supreme Court Rejects Trump's Emergency Tariffs",
       "Read more at nytimes.com", 6),
]
PROMO = mk(5, "The Athletic", "Last chance: 1 year for $1",
           "Your trial ends tonight - keep unlimited access.", -120)


def cluster_of(clusters, alerts, outlet):
    for m in clusters:
        if any(alerts[i].outlet == outlet for i in m):
            return m
    raise AssertionError(f"{outlet} not found")


def test_same_story_across_outlets_clusters_together():
    alerts = SCOTUS
    clusters = build_clusters(alerts)
    assert len(clusters) == 1
    assert len(clusters[0]) == 4


def test_divergent_house_style_still_joins():
    """The regression this exists for: Fox's all-caps headline used to be
    stranded because it arrived before the alert it best matched."""
    alerts = SCOTUS
    clusters = build_clusters(alerts)
    outlets = {alerts[i].outlet for i in clusters[0]}
    assert "Fox News" in outlets


def test_unrelated_promo_does_not_join():
    alerts = SCOTUS + [PROMO]
    clusters = build_clusters(alerts)
    scotus = cluster_of(clusters, alerts, "ABC News")
    assert PROMO.id not in [alerts[i].id for i in scotus]
    assert len(clusters) == 2


def test_first_mover_lags_are_correct():
    alerts = SCOTUS
    clusters = build_clusters(alerts)
    rows = first_mover_table(alerts, clusters[0])
    assert [r["outlet"] for r in rows] == [
        "ABC News", "Fox News", "CNN", "The New York Times",
    ]
    assert [r["lag_min"] for r in rows] == [0.0, 2.0, 3.0, 6.0]


def test_time_window_separates_distant_coverage():
    """Same words, 5 hours apart -> two clusters, not one."""
    alerts = [
        mk(1, "CNN", "Supreme Court strikes down emergency tariffs", "", 0),
        mk(2, "ABC News", "Supreme Court strikes down emergency tariffs", "", 300),
    ]
    clusters = build_clusters(alerts, window_min=90)
    assert len(clusters) == 2


def test_same_outlet_twice_counts_once_as_followup():
    alerts = SCOTUS + [
        mk(6, "ABC News", "UPDATE: Supreme Court tariff ruling reaction", "", 20),
    ]
    clusters = build_clusters(alerts)
    big = max(clusters, key=len)
    rows = first_mover_table(alerts, big)
    abc = [r for r in rows if r["outlet"] == "ABC News"]
    assert len(abc) == 1, "an outlet must appear once in the lag table"
    assert abc[0]["lag_min"] == 0.0
    assert abc[0]["followups"] >= 1


def test_empty_and_single_inputs():
    assert build_clusters([]) == []
    single = build_clusters([SCOTUS[0]])
    assert len(single) == 1 and len(single[0]) == 1


def test_threshold_is_respected():
    alerts = SCOTUS
    # An impossible threshold must yield all singletons.
    assert len(build_clusters(alerts, threshold=0.99)) == 4
    # A trivial threshold must yield one cluster.
    assert len(build_clusters(alerts, threshold=0.0)) == 1


def test_entities_are_stemmed():
    assert "tariff" in cluster.entities("Emergency Tariffs")
    assert "tariff" in cluster.entities("TARIFF PLAN")


def test_tokenizer_drops_stopwords_and_stems():
    toks = cluster.tokenize("The Supreme Court rules on the tariffs")
    assert "the" not in toks
    assert "tariff" in toks


def test_similarity_is_symmetric_and_bounded():
    b = TfidfBackend()
    b.prepare([a.text for a in SCOTUS])
    for i in range(len(SCOTUS)):
        assert b.similarity(i, i) == pytest.approx(1.0)
        for j in range(len(SCOTUS)):
            s = b.similarity(i, j)
            assert 0.0 <= s <= 1.0 + 1e-9
            assert s == pytest.approx(b.similarity(j, i))


def test_embedding_backend_raises_rather_than_degrading():
    """A run configured for embeddings must never silently report TF-IDF."""
    with pytest.raises(NotImplementedError):
        cluster.get_backend("embedding").prepare(["a", "b"])


def test_backend_is_pluggable():
    class AlwaysSame:
        name = "stub"

        def prepare(self, docs):
            pass

        def similarity(self, i, j):
            return 1.0

    # Wide window, so the only thing deciding membership is the backend.
    clusters = build_clusters(SCOTUS + [PROMO], backend=AlwaysSame(), window_min=600)
    assert len(clusters) == 1

    # And the time window still overrides a maximally permissive backend:
    # PROMO is 2 hours from the pack, so at the default 90 min it stays out.
    windowed = build_clusters(SCOTUS + [PROMO], backend=AlwaysSame(), window_min=90)
    assert len(windowed) == 2


def test_clustering_is_deterministic():
    a = build_clusters(SCOTUS + [PROMO])
    b = build_clusters(SCOTUS + [PROMO])
    assert [sorted(x) for x in a] == [sorted(x) for x in b]


def test_no_single_link_chaining_across_the_day():
    """Regression: identical routine headlines spread through a day must not
    chain into one giant cluster.

    Before the window was measured from each cluster's FIRST alert, a run of
    'Morning Wire' pushes at 7:00, 8:20, 9:40, 11:00 ... each landed within 90
    minutes of the previous one and single-link merged them all, producing a
    30-alert 'cluster' spanning an entire day. No cluster may span more than
    the window.
    """
    alerts = [
        mk(i, "AP News", "Morning Wire: the 5 stories to start your day",
           "Curated by AP editors.", minutes=80 * i)
        for i in range(10)
    ]
    clusters = build_clusters(alerts, window_min=90)
    assert len(clusters) > 1, "identical copy 13 hours apart must not be one cluster"
    for m in clusters:
        span = max(alerts[i].posted_at for i in m) - min(alerts[i].posted_at for i in m)
        assert span <= timedelta(minutes=90)


def test_cluster_span_never_exceeds_window():
    """Invariant over a larger mixed corpus."""
    alerts = []
    n = 0
    for day in range(3):
        for step in range(12):
            n += 1
            alerts.append(
                mk(n, "CNN", "Supreme Court rules on emergency tariffs", "",
                   minutes=day * 1440 + step * 45)
            )
    clusters = build_clusters(alerts, window_min=90)
    for m in clusters:
        span = max(alerts[i].posted_at for i in m) - min(alerts[i].posted_at for i in m)
        assert span <= timedelta(minutes=90)
