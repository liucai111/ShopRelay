"""Persist chat run result metadata and make request keys user-scoped.

Revision ID: 20261019_03
Revises: 20261019_02
"""
from alembic import op
import sqlalchemy as sa

revision = "20261019_03"
down_revision = "20261019_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("uq_runs_idempotency", type_="unique")
        batch.add_column(sa.Column("idempotency_scope", sa.String(192), nullable=True))
        batch.add_column(sa.Column("error_detail_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("result_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("model_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("usage_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("latency_ms", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE runs SET idempotency_scope = 'legacy:' || run_id WHERE idempotency_scope IS NULL"))
    with op.batch_alter_table("runs") as batch:
        batch.alter_column("idempotency_scope", existing_type=sa.String(192), nullable=False)
        batch.create_unique_constraint("uq_runs_idempotency", ["user_id", "idempotency_scope", "client_request_id"])


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("uq_runs_idempotency", type_="unique")
        batch.drop_column("finished_at")
        batch.drop_column("started_at")
        batch.drop_column("latency_ms")
        batch.drop_column("usage_json")
        batch.drop_column("model_id")
        batch.drop_column("result_json")
        batch.drop_column("error_detail_json")
        batch.drop_column("idempotency_scope")
        batch.create_unique_constraint("uq_runs_idempotency", ["user_id", "session_id", "client_request_id"])
