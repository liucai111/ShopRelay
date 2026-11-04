"""Allow separately modeled policy retrieval lifecycle events.

Revision ID: 20261027_17
Revises: 20261023_16
"""
from alembic import op
import sqlalchemy as sa

revision = "20261027_17"
down_revision = "20261023_16"
branch_labels = None
depends_on = None


_EVENT_CHECK = (
    "event_type IN ('started','route_decided','agent_started','retrieval_hop','tool_started',"
    "'tool_finished','policy_retrieval_started','policy_retrieval_finished','action_pending',"
    "'answer','done','error')"
)
_PREVIOUS_EVENT_CHECK = (
    "event_type IN ('started','route_decided','agent_started','retrieval_hop','tool_started',"
    "'tool_finished','action_pending','answer','done','error')"
)


def upgrade() -> None:
    with op.batch_alter_table("run_events", recreate="always") as batch:
        batch.drop_constraint("ck_run_events_type", type_="check")
        batch.create_check_constraint("ck_run_events_type", _EVENT_CHECK)


def downgrade() -> None:
    bind = op.get_bind()
    new_events = bind.execute(
        sa.text(
            "SELECT count(*) FROM run_events WHERE event_type IN "
            "('policy_retrieval_started','policy_retrieval_finished')"
        )
    ).scalar_one()
    if new_events:
        raise RuntimeError("refusing to remove policy retrieval event types while such RunEvents exist")
    with op.batch_alter_table("run_events", recreate="always") as batch:
        batch.drop_constraint("ck_run_events_type", type_="check")
        batch.create_check_constraint("ck_run_events_type", _PREVIOUS_EVENT_CHECK)
