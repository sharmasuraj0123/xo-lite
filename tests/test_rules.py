"""The engine's decisions, and the one thing that may skip an ask.

Stdlib only, so `python -m unittest discover tests` runs it with nothing
installed beyond the server's own dependencies.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xolite.engine import rules  # noqa: E402
from xolite.engine.base import TOOLS, PROJECT, THREAD, WORKSPACE, ProjectFacts, TurnSpec  # noqa: E402

BRIEF = "The plan is drifting. Check the state file and then remove the stale lock."


def tools(spec: TurnSpec) -> list[str]:
    return [c.tool for c in asyncio.run(rules.run_turn(spec)).calls]


def thread_spec(**over) -> TurnSpec:
    base = dict(
        role=THREAD, session_id="cse_t", chat_id="chat_infra", thread_ts="t1", cwd=".",
        message={"text": BRIEF},
        thread={"ts": "t1", "title": "Drift", "status_md": "", "chat_id": "chat_infra"},
    )
    base.update(over)
    return TurnSpec(**base)


def rule(sentence: str, **over) -> dict:
    row = {"sentence": sentence, "scope": "thread", "effect": "allow",
           "thread_ts": "t1", "chat_id": "chat_infra", "revoked_at": None}
    row.update(over)
    return row


class ReadingAMessage(unittest.TestCase):
    def test_an_imperative_anywhere_counts_as_work(self):
        # Not just the opening word: the ask often arrives in sentence two.
        self.assertTrue(rules.is_work("The totals are wrong. Fix the rounding."))
        self.assertTrue(rules.is_work("Profile the query path."))

    def test_a_question_is_not_work(self):
        self.assertFalse(rules.is_work("What is running in atlas-web?"))
        self.assertFalse(rules.is_work("Any news on the billing migration?"))

    def test_a_brief_splits_into_its_steps(self):
        self.assertEqual(
            rules.steps_of(BRIEF),
            ["The plan is drifting", "Check the state file", "Remove the stale lock"],
        )

    def test_risk_is_judged_per_step_not_per_brief(self):
        self.assertIsNone(rules.risk_of("Check the state file")[0])
        self.assertIsNotNone(rules.risk_of("Remove the stale lock")[0])

    def test_routing_names_the_project_in_the_text(self):
        projects = [ProjectFacts(name="atlas-web", chat_id="chat_atlas-web"),
                    ProjectFacts(name="billing-api", chat_id="chat_billing-api")]
        picked, why = rules.pick_project("the billing-api invoices endpoint times out", projects)
        self.assertIsNotNone(picked)
        self.assertEqual(picked.name, "billing-api")
        self.assertTrue(why)


class StoppingAtTheRightStep(unittest.TestCase):
    def test_a_risky_step_stops_the_turn(self):
        self.assertEqual(tools(thread_spec())[-1], "ask_owner")

    def test_it_asks_about_the_risky_step_not_the_first_one(self):
        calls = asyncio.run(rules.run_turn(thread_spec())).calls
        ask = next(c for c in calls if c.tool == "ask_owner")
        self.assertEqual(ask.args["title"], "Remove the stale lock")

    def test_the_ask_offers_the_rule_it_would_make_word_for_word(self):
        calls = asyncio.run(rules.run_turn(thread_spec())).calls
        ask = next(c for c in calls if c.tool == "ask_owner")
        scoped = [o for o in ask.args["options"] if o.get("scope") == "thread"]
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0]["sentence"], "Remove the stale lock")


class WhatAStandingRuleMay(unittest.TestCase):
    """A rule covers the sentence the owner allowed, and nothing adjacent."""

    def test_a_matching_rule_carries_the_thread_past_the_ask(self):
        calls = tools(thread_spec(rules=[rule("Remove the stale lock")]))
        self.assertNotIn("ask_owner", calls)
        self.assertEqual(calls[-1], "finish")

    def test_it_still_says_it_did_so(self):
        calls = asyncio.run(rules.run_turn(thread_spec(rules=[rule("Remove the stale lock")]))).calls
        notes = [c.args["text"] for c in calls if c.tool == "note"]
        self.assertTrue(any("you have a rule for that" in n for n in notes))

    def test_a_rule_for_another_sentence_does_not_widen(self):
        self.assertIn("ask_owner", tools(thread_spec(rules=[rule("Remove the users table")])))

    def test_a_revoked_rule_does_not_count(self):
        revoked = rule("Remove the stale lock", revoked_at="2026-01-01T00:00:00Z")
        self.assertIn("ask_owner", tools(thread_spec(rules=[revoked])))

    def test_a_deny_rule_never_allows(self):
        self.assertIn("ask_owner", tools(thread_spec(rules=[rule("Remove the stale lock", effect="deny")])))

    def test_matching_ignores_case_and_spacing_only(self):
        spec = thread_spec(rules=[rule("  remove   the STALE lock ")])
        self.assertNotIn("ask_owner", tools(spec))


class WhatARoleMay(unittest.TestCase):
    def test_every_role_the_engine_claims_has_a_vocabulary(self):
        for role in rules.SUPPORTED_ROLES:
            self.assertTrue(TOOLS[role], role)

    def test_an_engine_only_ever_returns_tools_its_role_owns(self):
        cases = [
            (WORKSPACE, TurnSpec(role=WORKSPACE, session_id="s", chat_id="chat_workspace",
                                 thread_ts=None, cwd=".", message={"text": BRIEF},
                                 projects=[ProjectFacts(name="infra", chat_id="chat_infra")])),
            (PROJECT, TurnSpec(role=PROJECT, session_id="s", chat_id="chat_infra",
                               thread_ts=None, cwd=".", message={"text": BRIEF},
                               projects=[ProjectFacts(name="infra", chat_id="chat_infra")])),
            (THREAD, thread_spec()),
        ]
        for role, spec in cases:
            for call in asyncio.run(rules.run_turn(spec)).calls:
                self.assertIn(call.tool, TOOLS[role], f"{role} called {call.tool}")

    def test_every_call_carries_a_reason(self):
        for call in asyncio.run(rules.run_turn(thread_spec())).calls:
            self.assertTrue(call.why.strip(), call.tool)


if __name__ == "__main__":
    unittest.main(verbosity=2)
