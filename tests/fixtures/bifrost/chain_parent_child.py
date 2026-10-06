"""Multi-runbook fixture: parent CHAINS INTO child (aa24, name target).

Converted WITHOUT --function: both @workflow functions become runbooks in
one --apply, which creates the child FIRST (dependency order from the
_chains sidecar) and patches the parent's aa24 step with the child's id,
then fires both. Proof shape mirrors runbook_chain_matrix.py (2561/2562):
the parent's run TERMINATES at the chain step and the child runs to
Success - plus a chain_started_runs row NEWER than the child's own
direct-fire runlog, which proves the aa24 (not our direct fire) started
that run.
"""

from bifrost import workflow


async def child_work(client) -> dict:
    """Stand-in helper: becomes a hop phase in the converted child."""
    return {}


async def prepare(client) -> dict:
    """Stand-in helper: becomes a hop phase in the converted parent."""
    return {}


@workflow(name="Chain Fixture: Child", description="Child runbook - the chain target.")
async def chain_fixture_child(client, tag: str) -> dict:
    await child_work(client)
    return {"tag": tag}


@workflow(
    name="Chain Fixture: Parent",
    description="Parent runbook - awaits the child workflow, chains into it.",
)
async def chain_fixture_parent(client, tag: str) -> dict:
    await prepare(client)
    await chain_fixture_child(client, tag=tag)
    return {"done": True}
