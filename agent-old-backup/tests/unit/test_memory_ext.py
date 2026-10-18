"""Unit tests for episodic, semantic, and vector memory.

SQLite/vector stores use temp directories — no network, no residue.
"""
import sys
import os
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from agent.memory.episodic import (
    EpisodicMemory, Episode, EpisodeGraph, RELATIONS,
)
from agent.memory.semantic import (
    SemanticMemory, Fact, InferenceEngine, FactHistory,
    combine_supporting, combine_contradicting,
    parse_simple_sentence, learn_from_text, dedup_facts,
    TRANSITIVE_PREDICATES,
)
from agent.memory.vector_store import (
    VectorStore, Document, SearchHit, BM25Ranker, HybridSearcher,
    MemoryIndex, SearchCache, tokenize, term_frequencies,
    reciprocal_rank_fusion, mmr_rerank, find_duplicates, dedup_store,
    did_you_mean, cluster_documents, summarize_document,
    keyword_in_context,
)


def tmpdb(name):
    d = tempfile.mkdtemp()
    return os.path.join(d, name)


# ---------------------------------------------------------------------------
# Episodic memory
# ---------------------------------------------------------------------------

class TestEpisodicStore(unittest.TestCase):
    def setUp(self):
        self.mem = EpisodicMemory(db_path=tmpdb("epi.db"))

    def test_store_and_get(self):
        eid = self.mem.store_episode(
            "deployed app", actors=["ahsan"], action="deploy",
            outcome="success", emotion_tags=["relief"],
            context={"env": "prod"}, importance=0.9)
        ep = self.mem.get_episode(eid)
        self.assertIsNotNone(ep)
        self.assertEqual(ep.event, "deployed app")
        self.assertEqual(ep.actors, ["ahsan"])
        self.assertEqual(ep.context, {"env": "prod"})
        self.assertAlmostEqual(ep.importance, 0.9)

    def test_importance_clamped(self):
        eid = self.mem.store_episode("x", importance=5.0)
        self.assertAlmostEqual(self.mem.get_episode(eid).importance, 1.0)

    def test_recall_by_time(self):
        base = 1_700_000_000.0
        self.mem.store_episode("first", timestamp=base)
        self.mem.store_episode("second", timestamp=base + 100)
        self.mem.store_episode("third", timestamp=base + 200)
        eps = self.mem.recall_by_time(base, base + 150)
        self.assertEqual([e.event for e in eps], ["first", "second"])

    def test_recall_recent_order(self):
        base = 1_700_000_000.0
        self.mem.store_episode("old", timestamp=base)
        self.mem.store_episode("new", timestamp=base + 10)
        eps = self.mem.recall_recent(limit=2)
        self.assertEqual(eps[0].event, "new")

    def test_recall_by_actor(self):
        self.mem.store_episode("e1", actors=["ahsan", "Sara"])
        self.mem.store_episode("e2", actors=["Bob"])
        eps = self.mem.recall_by_actor("AHSAN")  # case-insensitive
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0].event, "e1")

    def test_recall_by_tag(self):
        self.mem.store_episode("e1", emotion_tags=["urgent"])
        self.mem.store_episode("e2", emotion_tags=["calm"])
        eps = self.mem.recall_by_tag("urgent")
        self.assertEqual(len(eps), 1)

    def test_search_and_semantics(self):
        self.mem.store_episode("fixed the login bug",
                              action="patched auth.py",
                              outcome="all tests pass")
        hits = self.mem.search("login bug")
        self.assertEqual(len(hits), 1)
        self.assertEqual(self.mem.search(""), [])

    def test_recall_around(self):
        base = 1_700_000_000.0
        self.mem.store_episode("center", timestamp=base)
        self.mem.store_episode("far", timestamp=base + 100000)
        eps = self.mem.recall_around(base, window_s=60)
        self.assertEqual(len(eps), 1)

    def test_update_and_delete(self):
        eid = self.mem.store_episode("draft", outcome="pending")
        self.assertTrue(self.mem.update_episode(eid, outcome="done",
                                                importance=0.8))
        ep = self.mem.get_episode(eid)
        self.assertEqual(ep.outcome, "done")
        self.assertTrue(self.mem.delete_episode(eid))
        self.assertIsNone(self.mem.get_episode(eid))
        self.assertFalse(self.mem.delete_episode("nope"))

    def test_batch_store(self):
        eps = [Episode(event=f"e{i}", timestamp=1_700_000_000.0 + i)
               for i in range(5)]
        ids = self.mem.store_episodes(eps)
        self.assertEqual(len(ids), 5)
        self.assertEqual(self.mem.count(), 5)

    def test_actors_and_tags_list(self):
        self.mem.store_episode("e1", actors=["ahsan"],
                               emotion_tags=["happy"])
        self.mem.store_episode("e2", actors=["ahsan", "Sara"],
                               emotion_tags=["happy", "tired"])
        self.assertEqual(self.mem.actors_list(), ["Sara", "ahsan"])
        self.assertEqual(self.mem.tags_list(), ["happy", "tired"])

    def test_summarize_period(self):
        base = 1_700_000_000.0
        self.mem.store_episode("deploy", actors=["ahsan"],
                               outcome="success", timestamp=base)
        self.mem.store_episode("bug found", actors=["ahsan"],
                               outcome="test failed", timestamp=base + 10,
                               emotion_tags=["urgent"])
        s = self.mem.summarize_period(base, base + 3600)
        self.assertEqual(s["count"], 2)
        self.assertEqual(s["outcomes"]["success"], 1)
        self.assertEqual(s["outcomes"]["failure"], 1)
        self.assertIn("ahsan", s["actors"])
        self.assertIn("deploy", s["summary"])

    def test_summarize_empty(self):
        s = self.mem.summarize_period(0, 1)
        self.assertEqual(s["count"], 0)

    def test_daily_digest(self):
        base = 1_700_000_000.0
        self.mem.store_episode("thing", timestamp=base)
        d = self.mem.daily_digest(base)
        self.assertEqual(d["count"], 1)

    def test_forget_before(self):
        old = 1_000.0
        new = 1_700_000_000.0
        self.mem.store_episode("old low", timestamp=old, importance=0.2)
        self.mem.store_episode("old high", timestamp=old, importance=0.95)
        self.mem.store_episode("new", timestamp=new, importance=0.2)
        deleted = self.mem.forget_before(1_000_000.0, keep_important=0.8)
        self.assertEqual(deleted, 1)
        self.assertEqual(self.mem.count(), 2)

    def test_forget_by_tag(self):
        self.mem.store_episode("e1", emotion_tags=["spam"])
        self.mem.store_episode("e2", emotion_tags=["keep"])
        self.assertEqual(self.mem.forget_by_tag("spam"), 1)
        self.assertEqual(self.mem.count(), 1)

    def test_prune_to_size(self):
        for i in range(10):
            self.mem.store_episode(f"e{i}", importance=0.1,
                                   timestamp=float(i))
        self.mem.store_episode("important", importance=0.99,
                               timestamp=100.0)
        deleted = self.mem.prune_to_size(5)
        self.assertEqual(deleted, 6)
        self.assertEqual(self.mem.count(), 5)
        # important one survives
        self.assertEqual(len(self.mem.search("important")), 1)

    def test_recall_scored(self):
        self.mem.store_episode("gpu server running qwen model",
                               importance=0.9)
        self.mem.store_episode("unrelated fiverr gig note",
                               importance=0.1)
        scored = self.mem.recall_scored("qwen gpu model")
        self.assertGreater(len(scored), 0)
        self.assertIn("qwen", scored[0][0].event)
        self.assertEqual(self.mem.recall_scored(""), [])

    def test_recall_by_outcome(self):
        self.mem.store_episode("a", outcome="deployment succeeded")
        self.mem.store_episode("b", outcome="build failed with error")
        self.mem.store_episode("c", outcome="nothing notable")
        self.assertEqual(len(self.mem.recall_by_outcome("success")), 1)
        self.assertEqual(len(self.mem.recall_by_outcome("failure")), 1)
        self.assertEqual(len(self.mem.recall_by_outcome("neutral")), 1)
        with self.assertRaises(ValueError):
            self.mem.recall_by_outcome("bogus")

    def test_context_window(self):
        ids = [self.mem.store_episode(f"e{i}",
                                      timestamp=1_700_000_000.0 + i)
               for i in range(5)]
        window = self.mem.context_window(ids[2], before=1, after=1)
        self.assertEqual([e.event for e in window],
                         ["e1", "e2", "e3"])
        self.assertEqual(self.mem.context_window("nope"), [])

    def test_decay(self):
        old_ts = time.time() - 60 * 86400  # 60 days ago
        eid = self.mem.store_episode("old", importance=1.0,
                                     timestamp=old_ts)
        touched = self.mem.apply_decay(half_life_days=30.0, floor=0.1,
                                       now=time.time())
        self.assertEqual(touched, 1)
        ep = self.mem.get_episode(eid)
        self.assertAlmostEqual(ep.importance, 0.25, places=2)

    def test_boost(self):
        eid = self.mem.store_episode("e", importance=0.5)
        self.assertTrue(self.mem.boost_importance(eid, 0.3))
        self.assertAlmostEqual(self.mem.get_episode(eid).importance, 0.8)
        self.assertFalse(self.mem.boost_importance("nope"))

    def test_find_similar(self):
        a = self.mem.store_episode("deployed the web app to render")
        self.mem.store_episode("deployed the web app to render server")
        self.mem.store_episode("ate lunch with family")
        sim = self.mem.find_similar(a, threshold=0.4)
        self.assertEqual(len(sim), 1)
        self.assertGreaterEqual(sim[0][1], 0.4)

    def test_consolidate(self):
        self.mem.store_episode("server restarted cleanly",
                               actors=["ahsan"], importance=0.4)
        self.mem.store_episode("server restarted cleanly",
                               emotion_tags=["ok"], importance=0.6)
        removed = self.mem.consolidate(similarity_threshold=0.9)
        self.assertEqual(removed, 1)
        self.assertEqual(self.mem.count(), 1)
        ep = self.mem.recall_recent(1)[0]
        self.assertIn("ahsan", ep.actors)
        self.assertIn("ok", ep.emotion_tags)

    def test_export_import(self):
        self.mem.store_episode("e1", actors=["a"], importance=0.7)
        data = self.mem.export_json()
        import json
        json.dumps(data)
        mem2 = EpisodicMemory(db_path=tmpdb("epi2.db"))
        n = mem2.import_json(data)
        self.assertEqual(n, 1)
        self.assertEqual(mem2.count(), 1)

    def test_backup_restore_file(self):
        self.mem.store_episode("e1")
        self.mem.store_episode("e2")
        path = os.path.join(tempfile.mkdtemp(), "backup.jsonl")
        self.assertEqual(self.mem.backup_to_file(path), 2)
        mem2 = EpisodicMemory(db_path=tmpdb("epi3.db"))
        self.assertEqual(mem2.restore_from_file(path), 2)

    def test_merge_from(self):
        other = EpisodicMemory(db_path=tmpdb("other.db"))
        other.store_episode("from other")
        added = self.mem.merge_from(other.db_path)
        self.assertEqual(added, 1)

    def test_query_combined(self):
        base = 1_700_000_000.0
        self.mem.store_episode("deploy render", actors=["ahsan"],
                               emotion_tags=["happy"], timestamp=base,
                               importance=0.9)
        self.mem.store_episode("fix bug", actors=["ahsan"],
                               timestamp=base + 10, importance=0.2)
        res = self.mem.query(actors=["ahsan"], min_importance=0.5)
        self.assertEqual(len(res), 1)
        res = self.mem.query(text="render", tags=["happy"])
        self.assertEqual(len(res), 1)
        res = self.mem.query(start=base + 5)
        self.assertEqual(len(res), 1)

    def test_timeline(self):
        self.mem.store_episode("did thing", actors=["ahsan"],
                               outcome="ok",
                               timestamp=1_700_000_000.0)
        lines = self.mem.timeline()
        self.assertEqual(len(lines), 1)
        self.assertIn("did thing", lines[0])
        self.assertIn("ahsan", lines[0])

    def test_stats_and_vacuum(self):
        self.mem.store_episode("e1", outcome="success", actors=["a"])
        stats = self.mem.stats()
        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["outcomes"]["success"], 1)
        self.mem.vacuum()  # must not raise


class TestEpisodeGraph(unittest.TestCase):
    def setUp(self):
        self.mem = EpisodicMemory(db_path=tmpdb("epig.db"))
        self.graph = EpisodeGraph(self.mem)
        self.a = self.mem.store_episode("installed gpu driver")
        self.b = self.mem.store_episode("model loaded on gpu")

    def test_link_and_query(self):
        self.assertTrue(self.graph.link(self.a, self.b, "causes"))
        self.assertEqual(self.graph.outgoing(self.a), [(self.b, "causes")])
        self.assertEqual(self.graph.incoming(self.b), [(self.a, "causes")])

    def test_bad_relation_rejected(self):
        self.assertFalse(self.graph.link(self.a, self.b, "teleports"))

    def test_unlink(self):
        self.graph.link(self.a, self.b, "causes")
        self.assertEqual(self.graph.unlink(self.a, self.b, "causes"), 1)
        self.assertEqual(self.graph.outgoing(self.a), [])

    def test_causes_chain(self):
        c = self.mem.store_episode("serving traffic")
        self.graph.link(self.a, self.b, "causes")
        self.graph.link(self.b, c, "causes")
        chain = self.graph.causes_chain(self.a)
        self.assertEqual([e.id for e in chain], [self.b, c])

    def test_relations_constant(self):
        self.assertIn("causes", RELATIONS)
        self.assertIn("contradicts", RELATIONS)


# ---------------------------------------------------------------------------
# Semantic memory
# ---------------------------------------------------------------------------

class TestSemanticStore(unittest.TestCase):
    def setUp(self):
        self.mem = SemanticMemory(db_path=tmpdb("sem.db"))

    def test_store_and_query(self):
        f = self.mem.store_fact("Paris", "capital_of", "France",
                                confidence=0.9, source="test")
        self.assertEqual(f.subject, "paris")  # normalized
        self.assertEqual(f.observations, 1)
        res = self.mem.query_facts(subject="Paris")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].object, "france")

    def test_merge_confirms(self):
        self.mem.store_fact("Paris", "capital_of", "France",
                            confidence=0.5)
        f = self.mem.store_fact("Paris", "capital_of", "France",
                                confidence=0.5)
        self.assertEqual(f.observations, 2)
        # noisy-OR: 1-(1-.5)(1-.5) = 0.75
        self.assertAlmostEqual(f.confidence, 0.75)
        self.assertEqual(self.mem.count(), 1)

    def test_empty_triple_rejected(self):
        with self.assertRaises(ValueError):
            self.mem.store_fact("", "is", "x")

    def test_ask(self):
        self.mem.store_fact("Qwen", "is", "language model",
                            confidence=0.9)
        self.mem.store_fact("Qwen", "is", "banana", confidence=0.1)
        self.assertEqual(self.mem.ask("qwen", "is"), "language model")
        obj, conf = self.mem.ask_with_confidence("qwen", "is")
        self.assertEqual(obj, "language model")
        self.assertAlmostEqual(conf, 0.9)
        self.assertIsNone(self.mem.ask("nobody", "is"))

    def test_query_filters(self):
        self.mem.store_fact("a", "likes", "x", confidence=0.9)
        self.mem.store_fact("a", "likes", "y", confidence=0.2)
        self.mem.store_fact("b", "likes", "x", confidence=0.9)
        self.assertEqual(len(self.mem.query_facts(subject="a")), 2)
        self.assertEqual(len(self.mem.query_facts(predicate="likes")), 3)
        self.assertEqual(len(self.mem.query_facts(object="x")), 2)
        self.assertEqual(len(self.mem.query_facts(min_confidence=0.5)), 2)

    def test_search_text(self):
        self.mem.store_fact("Haley", "runs_on", "gpu server")
        self.assertEqual(len(self.mem.search_text("gpu")), 1)
        self.assertEqual(len(self.mem.search_text("zzz")), 0)

    def test_retract_and_delete(self):
        f = self.mem.store_fact("a", "is", "b")
        self.assertTrue(self.mem.retract_fact("a", "is", "b"))
        self.assertFalse(self.mem.retract_fact("a", "is", "b"))
        f2 = self.mem.store_fact("c", "is", "d")
        self.assertTrue(self.mem.delete_fact(f2.id))

    def test_update_confidence_support(self):
        f = self.mem.store_fact("a", "is", "b", confidence=0.5)
        updated = self.mem.update_confidence(f.id, 0.5, supports=True)
        self.assertAlmostEqual(updated.confidence, 0.75)
        self.assertEqual(updated.observations, 2)

    def test_update_confidence_contradict(self):
        f = self.mem.store_fact("a", "is", "b", confidence=0.8)
        updated = self.mem.update_confidence(f.id, 0.5, supports=False)
        self.assertAlmostEqual(updated.confidence, 0.4)

    def test_confirm_contradict_helpers(self):
        self.mem.store_fact("a", "is", "b", confidence=0.5)
        f = self.mem.confirm("a", "is", "b", 0.5)
        self.assertAlmostEqual(f.confidence, 0.75)
        f = self.mem.contradict("a", "is", "b", 0.5)
        self.assertAlmostEqual(f.confidence, 0.375)
        self.assertIsNone(self.mem.confirm("x", "is", "y"))

    def test_contradict_detect(self):
        self.mem.store_fact("Paris", "capital_of", "France",
                            confidence=0.9)
        self.mem.store_fact("Paris", "capital_of", "Lyon",
                            confidence=0.3)
        conflicts = self.mem.contradict_detect()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["subject"], "paris")
        self.assertEqual(conflicts[0]["winner"].object, "france")

    def test_contradict_detect_nonfunctional_ignored(self):
        self.mem.store_fact("ahsan", "likes", "mangoes")
        self.mem.store_fact("ahsan", "likes", "chai")
        self.assertEqual(self.mem.contradict_detect(), [])

    def test_resolve_conflict(self):
        self.mem.store_fact("Paris", "capital_of", "France",
                            confidence=0.6)
        self.mem.store_fact("Paris", "capital_of", "Lyon",
                            confidence=0.6)
        res = self.mem.resolve_conflict("paris", "capital_of", "France")
        self.assertEqual(res["kept"]["object"], "france")
        self.assertEqual(res["dropped"], ["lyon"])
        self.assertGreater(res["kept"]["confidence"], 0.6)

    def test_knowledge_graph(self):
        self.mem.store_fact("a", "is", "b", confidence=0.9)
        self.mem.store_fact("b", "is", "c", confidence=0.9)
        g = self.mem.export_knowledge_graph()
        self.assertEqual(len(g["nodes"]), 3)
        self.assertEqual(len(g["edges"]), 2)
        edge = g["edges"][0]
        self.assertIn("predicate", edge)
        self.assertIn("confidence", edge)

    def test_export_import_triples(self):
        self.mem.store_fact("a", "is", "b", confidence=0.8,
                            source="t")
        data = self.mem.export_triples()
        import json
        json.dumps(data)
        mem2 = SemanticMemory(db_path=tmpdb("sem2.db"))
        self.assertEqual(mem2.import_triples(data), 1)

    def test_describe_subject(self):
        self.mem.store_fact("Qwen", "is", "language model")
        text = self.mem.describe_subject("qwen")
        self.assertIn("language model", text)
        self.assertIn("Nothing known",
                      self.mem.describe_subject("nobody"))

    def test_prune_and_decay(self):
        self.mem.store_fact("a", "is", "b", confidence=0.05)
        self.mem.store_fact("c", "is", "d", confidence=0.9)
        self.assertEqual(self.mem.prune_low_confidence(0.15), 1)
        self.assertEqual(self.mem.count(), 1)

    def test_stats(self):
        self.mem.store_fact("Paris", "capital_of", "France",
                            confidence=0.9)
        self.mem.store_fact("Paris", "capital_of", "Lyon",
                            confidence=0.3)
        stats = self.mem.stats()
        self.assertEqual(stats["facts"], 2)
        self.assertEqual(stats["conflicts"], 1)

    def test_subjects_predicates(self):
        self.mem.store_fact("a", "is", "b")
        self.mem.store_fact("a", "has", "c")
        self.assertEqual(self.mem.subjects(), ["a"])
        self.assertEqual(sorted(self.mem.predicates()), ["has", "is"])


class TestConfidenceMath(unittest.TestCase):
    def test_combine_supporting(self):
        self.assertAlmostEqual(combine_supporting([0.5, 0.5]), 0.75)
        self.assertAlmostEqual(combine_supporting([]), 0.0)

    def test_combine_contradicting(self):
        self.assertAlmostEqual(combine_contradicting(0.8, [0.5]), 0.4)


class TestSentenceParsing(unittest.TestCase):
    def test_is_sentence(self):
        self.assertEqual(parse_simple_sentence("The sky is blue."),
                         ("The sky", "is", "blue"))

    def test_has_sentence(self):
        self.assertEqual(parse_simple_sentence("Qwen has 7B parameters"),
                         ("Qwen", "has", "7B parameters"))

    def test_empty_none(self):
        self.assertIsNone(parse_simple_sentence(""))
        self.assertIsNone(parse_simple_sentence("..."))

    def test_learn_from_text(self):
        mem = SemanticMemory(db_path=tmpdb("sem3.db"))
        facts = learn_from_text(mem, "Paris is the capital of France. "
                                    "Qwen has 7 billion parameters.",
                                source="t")
        self.assertEqual(len(facts), 2)
        # note: leading "the " is stripped by the sentence parser
        self.assertEqual(mem.ask("paris", "is"), "capital of france")


class TestInference(unittest.TestCase):
    def setUp(self):
        self.mem = SemanticMemory(db_path=tmpdb("sem4.db"))
        self.eng = InferenceEngine(self.mem)

    def test_transitive(self):
        self.mem.store_fact("Lahore", "located_in", "Punjab",
                            confidence=0.9)
        self.mem.store_fact("Punjab", "located_in", "Pakistan",
                            confidence=0.9)
        totals = self.eng.run()
        self.assertEqual(totals["transitive"], 1)
        # inferred with discount: 0.9 * 0.9 = 0.81
        obj, conf = self.mem.ask_with_confidence("lahore", "located_in")
        inferred = [f for f in self.mem.query_facts(
            subject="lahore", predicate="located_in")
            if f.object == "pakistan"]
        self.assertEqual(len(inferred), 1)
        self.assertAlmostEqual(inferred[0].confidence, 0.81)
        self.assertTrue(inferred[0].source.startswith("inferred:"))

    def test_fixpoint_no_dupes(self):
        self.mem.store_fact("a", "located_in", "b", confidence=0.9)
        self.mem.store_fact("b", "located_in", "c", confidence=0.9)
        self.eng.run()
        n1 = self.mem.count()
        self.eng.run()
        self.assertEqual(self.mem.count(), n1)

    def test_symmetric(self):
        self.mem.store_fact("a", "married_to", "b", confidence=0.8)
        totals = self.eng.run()
        self.assertEqual(totals["symmetric"], 1)
        self.assertEqual(self.mem.ask("b", "married_to"), "a")

    def test_inferred_facts_list(self):
        self.mem.store_fact("a", "located_in", "b", confidence=0.9)
        self.mem.store_fact("b", "located_in", "c", confidence=0.9)
        self.eng.run()
        self.assertEqual(len(self.eng.inferred_facts()), 1)

    def test_transitive_predicates_constant(self):
        self.assertIn("located_in", TRANSITIVE_PREDICATES)


class TestFactHistory(unittest.TestCase):
    def test_record_and_explain(self):
        mem = SemanticMemory(db_path=tmpdb("sem5.db"))
        hist = FactHistory(mem)
        f = mem.store_fact("a", "is", "b", confidence=0.5)
        hist.record(f.id, 0.5, 0.75, reason="confirmed by test")
        h = hist.history(f.id)
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["reason"], "confirmed by test")
        text = hist.explain(f.id)
        self.assertIn("0.500 -> 0.750", text)
        self.assertIn("No fact with id", hist.explain("nope"))


class TestDedup(unittest.TestCase):
    def test_dedup_facts(self):
        mem = SemanticMemory(db_path=tmpdb("sem6.db"))
        mem.store_fact("a", "is", "b", confidence=0.5, merge=False)
        mem.store_fact("a", "is", "b", confidence=0.9, merge=False)
        self.assertEqual(mem.count(), 2)
        removed = dedup_facts(mem)
        self.assertEqual(removed, 1)
        self.assertAlmostEqual(mem.ask_with_confidence("a", "is")[1], 0.9)


# ---------------------------------------------------------------------------
# Vector store
# ---------------------------------------------------------------------------

class TestTokenize(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(tokenize("Hello, World! 123"),
                         ["hello", "world", "123"])

    def test_stopwords_removed(self):
        self.assertEqual(tokenize("the cat sat on the mat"), ["cat", "sat", "mat"])

    def test_keep_stopwords(self):
        toks = tokenize("the cat", remove_stopwords=False)
        self.assertIn("the", toks)

    def test_term_frequencies(self):
        self.assertEqual(term_frequencies(["a", "b", "a"]),
                         {"a": 2, "b": 1})


class TestVectorStore(unittest.TestCase):
    def setUp(self):
        self.v = VectorStore()

    def test_add_and_len(self):
        did = self.v.add_document("hello world")
        self.assertEqual(len(self.v), 1)
        self.assertEqual(len(did), 16)

    def test_search_ranking(self):
        self.v.add_documents([
            "the qwen model runs on the gpu server",
            "fiverr gig delivery rules and pricing",
            "supabase postgres stores agent memory",
        ])
        hits = self.v.search("gpu model qwen", top_k=3)
        self.assertGreater(len(hits), 0)
        self.assertIn("qwen", hits[0].text)
        self.assertGreaterEqual(hits[0].score, hits[-1].score)

    def test_search_no_match(self):
        self.v.add_document("hello world")
        self.assertEqual(self.v.search("xylophone zebra"), [])

    def test_remove(self):
        did = self.v.add_document("hello world")
        self.assertTrue(self.v.remove_document(did))
        self.assertFalse(self.v.remove_document(did))
        self.assertEqual(len(self.v), 0)

    def test_update(self):
        did = self.v.add_document("hello world")
        self.assertTrue(self.v.update_document(did, "goodbye world"))
        hits = self.v.search("goodbye")
        self.assertEqual(len(hits), 1)
        self.assertFalse(self.v.update_document("nope", "x"))

    def test_cosine_known_values(self):
        a = {"x": 1.0, "y": 0.0}
        b = {"x": 1.0, "y": 0.0}
        self.assertAlmostEqual(VectorStore.cosine_similarity(a, b), 1.0)
        c = {"x": 1.0}
        d = {"z": 1.0}
        self.assertAlmostEqual(VectorStore.cosine_similarity(c, d), 0.0)
        self.assertAlmostEqual(VectorStore.cosine_similarity({}, c), 0.0)

    def test_most_similar_to_doc(self):
        a = self.v.add_document("gpu server hosts qwen model")
        b = self.v.add_document("qwen model runs on gpu")
        self.v.add_document("fiverr gig pricing rules")
        sim = self.v.most_similar_to_doc(a, top_k=2)
        self.assertEqual(sim[0].doc_id, b)

    def test_vocabulary(self):
        self.v.add_document("cat dog cat")
        self.assertIn("cat", self.v.vocabulary())
        self.assertEqual(self.v.vocabulary_size(), 2)
        stats = self.v.term_stats("cat")
        self.assertEqual(stats["document_frequency"], 1)
        self.assertGreater(stats["idf"], 0)

    def test_top_terms(self):
        did = self.v.add_document("qwen qwen model gpu server unique")
        top = self.v.top_terms(did, k=2)
        self.assertEqual(top[0][0], "qwen")

    def test_corpus_stats(self):
        # note: single-char tokens are filtered (min_length=2)
        self.v.add_documents(["alpha beta gamma", "delta epsilon"])
        stats = self.v.corpus_stats()
        self.assertEqual(stats["documents"], 2)
        self.assertGreater(stats["vocabulary_size"], 0)

    def test_save_load(self):
        path = os.path.join(tempfile.mkdtemp(), "vec.json")
        self.v.add_document("hello world", metadata={"k": "v"})
        self.v.save(path)
        v2 = VectorStore(persist_path=path)
        self.assertEqual(len(v2), 1)
        hits = v2.search("hello")
        self.assertEqual(hits[0].metadata["k"], "v")

    def test_save_without_path_raises(self):
        with self.assertRaises(ValueError):
            VectorStore().save()

    def test_chunk_text(self):
        chunks = VectorStore.chunk_text("w1 w2 w3 w4 w5", chunk_size=2,
                                        overlap=1)
        self.assertEqual(chunks, ["w1 w2", "w2 w3", "w3 w4", "w4 w5", "w5"])
        self.assertEqual(VectorStore.chunk_text(""), [])

    def test_index_text(self):
        ids = self.v.index_text("alpha beta gamma delta epsilon",
                                chunk_size=2, overlap=0,
                                base_metadata={"src": "t"})
        self.assertEqual(len(ids), 3)
        self.assertEqual(self.v.documents[ids[0]].metadata["chunk_index"],
                         0)

    def test_expanded_search(self):
        self.v.add_documents([
            "the graphics processor accelerates neural networks",
            "cooking recipes for dinner",
        ])
        hits = self.v.query_expanded_search("gpu neural", top_k=2)
        self.assertGreater(len(hits), 0)


class TestBM25(unittest.TestCase):
    def test_bm25_ranking(self):
        v = VectorStore()
        v.add_documents([
            "qwen model qwen model qwen",
            "unrelated text about cooking",
        ])
        r = BM25Ranker(v)
        hits = r.search("qwen model", top_k=2)
        self.assertIn("qwen", hits[0].text)
        self.assertEqual(r.score("qwen", "nonexistent"), 0.0)


class TestFusion(unittest.TestCase):
    def test_rrf(self):
        fused = reciprocal_rank_fusion([["a", "b"], ["b", "c"]], top_k=3)
        ids = [d for d, _ in fused]
        self.assertEqual(ids[0], "b")  # ranked in both lists
        self.assertEqual(len(fused), 3)

    def test_hybrid(self):
        v = VectorStore()
        v.add_documents(["qwen gpu model server", "fiverr gigs"])
        h = HybridSearcher(v).search("qwen gpu", top_k=2)
        self.assertIn("qwen", h[0].text)


class TestMMR(unittest.TestCase):
    def test_diversifies(self):
        v = VectorStore()
        v.add_documents([
            "qwen model on gpu server one",
            "qwen model on gpu server two",
            "qwen model helps fiverr gig work",
        ])
        hits = v.search("qwen model gpu", top_k=3)
        self.assertEqual(len(hits), 3)
        ranked = mmr_rerank("qwen model gpu", hits, v, top_k=2,
                            lambda_=0.1)
        texts = " ".join(h.text for h in ranked)
        # with low lambda, diversity wins: fiverr doc should appear
        self.assertIn("fiverr", texts)

    def test_empty(self):
        self.assertEqual(mmr_rerank("q", [], VectorStore()), [])


class TestDuplicates(unittest.TestCase):
    def test_find_duplicates(self):
        v = VectorStore()
        a = v.add_document("the quick brown fox jumps")
        b = v.add_document("the quick brown fox jumps")
        v.add_document("something entirely different here xyz")
        dups = find_duplicates(v, threshold=0.9)
        self.assertEqual(len(dups), 1)
        self.assertEqual({dups[0][0], dups[0][1]}, {a, b})

    def test_dedup_store(self):
        v = VectorStore()
        v.add_document("repeat repeat repeat")
        v.add_document("repeat repeat repeat")
        removed = dedup_store(v, threshold=0.9)
        self.assertEqual(removed, 1)
        self.assertEqual(len(v), 1)


class TestDidYouMean(unittest.TestCase):
    def test_suggests(self):
        v = VectorStore()
        v.add_document("the gpu server is fast")
        s = did_you_mean(v, "gp server")
        self.assertEqual(s.get("gp"), "gpu")

    def test_no_suggestion_for_known(self):
        v = VectorStore()
        v.add_document("the gpu server is fast")
        self.assertEqual(did_you_mean(v, "gpu server"), {})


class TestClustering(unittest.TestCase):
    def test_clusters(self):
        v = VectorStore()
        v.add_documents([
            "gpu server hosts qwen model",
            "qwen model runs on gpu",
            "fiverr gig delivery rules",
            "fiverr order management tips",
        ])
        clusters = cluster_documents(v, similarity_threshold=0.15)
        self.assertEqual(len(clusters), 2)
        for c in clusters:
            self.assertEqual(len(c), 2)

    def test_empty(self):
        self.assertEqual(cluster_documents(VectorStore()), [])


class TestSummarizeKwIc(unittest.TestCase):
    def test_summarize(self):
        v = VectorStore()
        did = v.add_document(
            "The qwen model is powerful. It runs on a gpu server. "
            "The weather is nice today. Fiverr gigs pay well.")
        s = summarize_document(v, did, max_sentences=2)
        self.assertIn("qwen", s.lower())
        self.assertLessEqual(len(s.split(". ")), 3)

    def test_kwic(self):
        v = VectorStore()
        did = v.add_document("the gpu server hosts the model")
        snippets = keyword_in_context(v, did, "gpu", window=5)
        self.assertEqual(len(snippets), 1)
        self.assertIn("gpu", snippets[0])


class TestSearchCache(unittest.TestCase):
    def test_hit_and_miss(self):
        v = VectorStore()
        v.add_document("hello world")
        cache = SearchCache(ttl_seconds=60)
        r1 = cache.cached_search(v, "hello", 5)
        r2 = cache.cached_search(v, "hello", 5)
        self.assertEqual(len(r1), len(r2))
        cache.invalidate()
        self.assertIsNone(cache.get("hello", 5))

    def test_ttl_expiry(self):
        cache = SearchCache(ttl_seconds=-1)  # already expired
        v = VectorStore()
        v.add_document("hello")
        cache.cached_search(v, "hello", 5)
        self.assertIsNone(cache.get("hello", 5))


class TestMemoryIndex(unittest.TestCase):
    def setUp(self):
        self.mi = MemoryIndex()

    def test_remember_recall(self):
        self.mi.remember("the gpu server hosts the qwen model",
                         kind="infra", importance=0.9)
        self.mi.remember("fiverr gigs need 1-day delivery",
                         kind="business", importance=0.5)
        hits = self.mi.recall("qwen gpu model", top_k=2)
        self.assertIn("qwen", hits[0].text)

    def test_kind_filter(self):
        self.mi.remember("gpu note", kind="infra")
        self.mi.remember("gpu business note", kind="business")
        hits = self.mi.recall("gpu", kind="business", diversify=False,
                              use_cache=False)
        self.assertTrue(all(h.metadata["kind"] == "business" for h in hits))

    def test_forget(self):
        did = self.mi.remember("temporary note")
        self.assertTrue(self.mi.forget(did))
        self.assertEqual(len(self.mi), 0)

    def test_recall_as_context(self):
        self.mi.remember("the sky is blue")
        ctx = self.mi.recall_as_context("sky color")
        self.assertIn("sky", ctx)

    def test_recall_empty(self):
        self.assertIn("no relevant memories",
                      self.mi.recall_as_context("zzz qqq"))

    def test_remember_chunked(self):
        ids = self.mi.remember_chunked("w1 w2 w3 w4 w5 w6", chunk_size=2)
        self.assertGreater(len(ids), 1)

    def test_stats(self):
        self.mi.remember("a", kind="infra")
        self.mi.remember("b", kind="business")
        stats = self.mi.stats()
        self.assertEqual(stats["kinds"], {"infra": 1, "business": 1})

    def test_save_without_path(self):
        self.assertIsNone(self.mi.save())


if __name__ == "__main__":
    unittest.main()
