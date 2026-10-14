"""Cron turns: drop the job's static prompt, keep the per-run data the scheduler injected.

25.09.2026: the static inbox-watch prompt was re-extracted as ~24 duplicate facts/day in
hermes-secretary, so the prompt is replaced by a marker. The same user message also carries
pre-run script stdout / upstream job output / ``## Run Context`` — fresh facts that must survive.
The message shape below is what cron/scheduler_prompt.py::_prepend_context_block builds.
"""

from __future__ import annotations

import json

RULES = "RULE: check the inbox and never write to prod."


def _user_text(fake):
    item = fake.retains[0]["items"][0] if "items" in fake.retains[0] else fake.retains[0]
    turn = json.loads(item["content"][1:-1])
    return turn[0]["content"]


def _cron(provider):
    instance, fake = provider({"bank_id": "team"})
    instance._platform = "cron"
    return instance, fake


def test_static_cron_prompt_is_not_retained(provider):
    instance, fake = _cron(provider)
    instance.sync_turn(RULES, "nothing new")
    instance.shutdown()
    text = _user_text(fake)
    assert RULES not in text
    assert "prompt omitted" in text


def test_script_output_and_run_context_survive(provider):
    instance, fake = _cron(provider)
    msg = (
        "## Script Output\nThe following data was collected by a pre-run script. Use it.\n\n"
        "```\ninbox: 3 new, from Ivanov re MLPO-1709\n```\n\n"
        f"{RULES}\n\n## Run Context\nlook at t_123"
    )
    instance.sync_turn(msg, "done")
    instance.shutdown()
    text = _user_text(fake)
    assert "Ivanov re MLPO-1709" in text and "t_123" in text
    assert RULES not in text


def test_interactive_turn_is_untouched(provider):
    instance, fake = provider({"bank_id": "team"})
    instance.sync_turn(RULES, "ok")
    instance.shutdown()
    assert RULES in _user_text(fake)
