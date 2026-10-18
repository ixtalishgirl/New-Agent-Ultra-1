"""Unit tests for agent.planning module."""
import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from agent.planning import (
    Task, Plan, TaskStatus, TaskPriority, TaskType,
    GoalDecomposer, PlanBuilder, PriorityScheduler,
    PlanOptimizer, PlanStore, PlanReporter,
    QueryGenerator, SourceScorer, FactExtractor, FactTriangulator,
    ResearchOrchestrator, Source, Fact, SearchQuery,
    GoalManager, GoalStatus, MilestoneStatus,
    ControlLoop, PlanRunner, LoopPhase,
    Planner, TaskDecomposer, DependencyGraph,
    RetryPolicy, RetryDecision,
)


class TestTaskPlanner(unittest.TestCase):
    def test_task_creation(self):
        t = Task(id="t1", title="Test", description="desc",
                 task_type=TaskType.SHELL, tool_name="shell")
        self.assertEqual(t.id, "t1")
        self.assertEqual(t.status, TaskStatus.PENDING)
        self.assertFalse(t.is_terminal)

    def test_task_serialization(self):
        t = Task(id="t1", title="Test", description="desc",
                 task_type=TaskType.PYTHON, tool_name="python")
        d = t.to_dict()
        t2 = Task.from_dict(d)
        self.assertEqual(t2.id, "t1")
        self.assertEqual(t2.task_type, TaskType.PYTHON)

    def test_goal_decomposer(self):
        d = GoalDecomposer()
        specs = d.decompose("Search for Python docs and then run the tests")
        self.assertGreater(len(specs), 0)

    def test_plan_builder(self):
        b = PlanBuilder()
        plan = b.build("Run the tests")
        self.assertGreater(len(plan.tasks), 0)
        # No cycles
        self.assertFalse(b._has_cycle(plan))

    def test_topological_order(self):
        b = PlanBuilder()
        plan = b.build("Do A then B then C")
        order = b.topological_order(plan)
        self.assertEqual(len(order), len(plan.tasks))

    def test_critical_path(self):
        b = PlanBuilder()
        plan = b.build("Step one then step two")
        cp = b.critical_path(plan)
        self.assertGreater(len(cp), 0)

    def test_cycle_detection(self):
        b = PlanBuilder()
        plan = Plan(id="p1", goal="test")
        t1 = Task(id="a", title="A", description="",
                  task_type=TaskType.SHELL, tool_name="shell",
                  dependencies=["b"])
        t2 = Task(id="b", title="B", description="",
                  task_type=TaskType.SHELL, tool_name="shell",
                  dependencies=["a"])
        plan.tasks = {"a": t1, "b": t2}
        self.assertTrue(b._has_cycle(plan))

    def test_scheduler(self):
        b = PlanBuilder()
        plan = b.build("First do X")
        sched = PriorityScheduler()
        decision = sched.next_task(plan)
        self.assertIsNotNone(decision.task_id)

    def test_scheduler_mark_completed(self):
        b = PlanBuilder()
        plan = b.build("Do X then Y")
        sched = PriorityScheduler()
        d1 = sched.next_task(plan)
        sched.mark_running(plan, d1.task_id)
        sched.mark_completed(plan, d1.task_id, "done")
        d2 = sched.next_task(plan)
        # Second task should now be ready (or plan complete)
        self.assertIsNotNone(d2)

    def test_optimizer_dedup(self):
        b = PlanBuilder()
        opt = PlanOptimizer()
        plan = b.build("Run tests")
        # Add duplicate
        specs = [{"title": "Dup", "description": "d",
                  "task_type": TaskType.SHELL, "tool_name": "shell",
                  "tool_args": {"command": "echo hi"}}]
        plan2 = b.build("Run tests", extra_specs=specs)
        plan2 = b.build("Run tests", extra_specs=specs)
        optimized = opt._deduplicate(plan2)
        self.assertLessEqual(len(optimized.tasks), len(plan2.tasks))

    def test_plan_reporter(self):
        b = PlanBuilder()
        plan = b.build("Test goal")
        reporter = PlanReporter()
        rep = reporter.report(plan)
        self.assertEqual(rep.plan_id, plan.id)
        text = reporter.format_text(rep, plan)
        self.assertIn("Progress", text)


class TestResearchPlanner(unittest.TestCase):
    def test_query_generator(self):
        qg = QueryGenerator()
        queries = qg.generate("How to fix Python import errors?", max_queries=5)
        self.assertGreater(len(queries), 0)
        self.assertLessEqual(len(queries), 5)

    def test_query_intent_classification(self):
        qg = QueryGenerator()
        self.assertEqual(qg._classify_intent("fix my error"), "error")
        self.assertEqual(qg._classify_intent("X vs Y"), "compare")

    def test_source_scorer(self):
        scorer = SourceScorer()
        src = Source(url="https://docs.python.org/3/tutorial/",
                     title="Python Tutorial")
        scorer.classify(src)
        self.assertEqual(src.reliability, 3)

    def test_fact_triangulator(self):
        tri = FactTriangulator()
        facts = [
            Fact(statement="Python is a programming language",
                 sources=["http://a.com"]),
            Fact(statement="Python is a programming language used widely",
                 sources=["http://b.com"]),
        ]
        merged = tri.triangulate(facts)
        self.assertEqual(len(merged), 1)
        self.assertGreater(merged[0].confidence, 0.5)
        self.assertEqual(len(merged[0].sources), 2)

    def test_fact_conflicts(self):
        tri = FactTriangulator()
        facts = [
            Fact(statement="Python is fast", sources=["a"]),
            Fact(statement="Python is not fast", sources=["b"]),
        ]
        conflicts = tri.find_conflicts(facts)
        self.assertGreater(len(conflicts), 0)

    def test_research_orchestrator(self):
        orch = ResearchOrchestrator()
        plan = orch.plan_research("What is Python?")
        self.assertGreater(len(plan.queries), 0)
        # Register fake results
        orch.register_search_results(plan, plan.queries[0].query, [
            {"url": "https://docs.python.org", "title": "Python Docs",
             "snippet": "Python is a language"}])
        self.assertGreater(len(plan.sources), 0)


class TestGoalManager(unittest.TestCase):
    def test_create_goal(self):
        gm = GoalManager(store=__import__(
            "agent.planning.goal_manager",
            fromlist=["GoalStore"]).GoalStore("/tmp/test_goals_1"))
        goal = gm.create_goal("Test Goal", milestones=["M1", "M2"])
        self.assertEqual(len(goal.milestones), 2)
        self.assertEqual(goal.status, GoalStatus.ACTIVE)

    def test_milestone_lifecycle(self):
        from agent.planning.goal_manager import GoalStore
        gm = GoalManager(store=GoalStore("/tmp/test_goals_2"))
        goal = gm.create_goal("G", milestones=["M1"])
        ms = goal.milestones[0]
        self.assertTrue(gm.start_milestone(goal.id, ms.id))
        self.assertTrue(gm.complete_milestone(goal.id, ms.id))
        # Goal should auto-complete
        self.assertEqual(gm.get_goal(goal.id).status, GoalStatus.COMPLETED)

    def test_next_actions(self):
        from agent.planning.goal_manager import GoalStore
        gm = GoalManager(store=GoalStore("/tmp/test_goals_3"))
        gm.create_goal("G1", milestones=["Do X"])
        gm.create_goal("G2", milestones=["Do Y"])
        actions = gm.next_actions()
        self.assertGreater(len(actions), 0)


class TestDependencyGraph(unittest.TestCase):
    def test_basic(self):
        g = DependencyGraph()
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        self.assertEqual(g.topological_sort(), ["a", "b", "c"])
        self.assertFalse(g.has_cycle())

    def test_cycle(self):
        g = DependencyGraph()
        g.add_edge("a", "b")
        g.add_edge("b", "a")
        self.assertTrue(g.has_cycle())
        self.assertIsNotNone(g.find_cycle())

    def test_levels(self):
        g = DependencyGraph()
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        levels = g.levels()
        self.assertEqual(levels["a"], 0)
        self.assertEqual(levels["d"], 2)

    def test_ancestors(self):
        g = DependencyGraph()
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        self.assertEqual(g.ancestors("c"), {"a", "b"})


class TestRetryPolicy(unittest.TestCase):
    def test_fatal_not_retried(self):
        p = RetryPolicy()
        task = Task(id="t", title="T", description="",
                    task_type=TaskType.SHELL, tool_name="shell")
        d = p.decide(task, "DENIED by policy container_escape")
        self.assertEqual(d, RetryDecision.ABORT_PLAN)

    def test_transient_retried(self):
        p = RetryPolicy()
        task = Task(id="t", title="T", description="",
                    task_type=TaskType.SHELL, tool_name="shell")
        d = p.decide(task, "connection timeout")
        self.assertEqual(d, RetryDecision.RETRY)

    def test_backoff(self):
        p = RetryPolicy(backoff_base_seconds=2.0)
        self.assertEqual(p.backoff_for(0), 2.0)
        self.assertEqual(p.backoff_for(1), 4.0)
        self.assertLessEqual(p.backoff_for(10), 60.0)


class TestControlLoop(unittest.TestCase):
    def _make_loop(self):
        def runner(name, args):
            return {"ok": True, "output": "done"}
        def checker(name, args):
            return {"allowed": True, "reason": "ok"}
        from agent.planning.plan_executor import ControlLoop
        return ControlLoop(tool_runner=runner,
                           permission_checker=checker)

    def test_full_loop(self):
        loop = self._make_loop()
        task = Task(id="t1", title="Echo", description="echo hi",
                    task_type=TaskType.SHELL, tool_name="shell",
                    tool_args={"command": "echo hi"})
        ex = loop.execute_task(task)
        self.assertTrue(ex.final_ok)
        phases = [p.phase for p in ex.phases]
        self.assertIn(LoopPhase.EXECUTE, phases)
        self.assertIn(LoopPhase.VERIFY, phases)

    def test_denied_tool(self):
        def runner(name, args):
            return {"ok": True, "output": "x"}
        def checker(name, args):
            return {"allowed": False, "reason": "denied"}
        from agent.planning.plan_executor import ControlLoop
        loop = ControlLoop(tool_runner=runner,
                           permission_checker=checker)
        task = Task(id="t1", title="T", description="d",
                    task_type=TaskType.SHELL, tool_name="shell")
        ex = loop.execute_task(task)
        self.assertFalse(ex.final_ok)

    def test_plan_runner(self):
        from agent.planning.plan_executor import PlanRunner
        loop = self._make_loop()
        runner = PlanRunner(loop)
        b = PlanBuilder()
        plan = b.build("Echo hello")
        result = runner.run(plan, max_tasks=3)
        self.assertGreater(len(result.executions), 0)


class TestPlannerFacade(unittest.TestCase):
    def test_planner(self):
        p = Planner()
        plan = p.plan("Run the test suite")
        self.assertGreater(len(plan.tasks), 0)

    def test_task_decomposer(self):
        td = TaskDecomposer()
        subs = td.decompose_with_verification("Search docs and run tests")
        self.assertGreater(len(subs), 0)
        for s in subs:
            self.assertTrue(s.verify_criterion)


if __name__ == "__main__":
    unittest.main()
