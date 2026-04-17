"""admin dashboard, transcript turns, post-session/follow-up columns

Revision ID: 002
Revises: 001
Create Date: 2026-04-17

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- sessions: post-session/follow-up/dashboard columns ---
    op.add_column("sessions", sa.Column("tdd_s3_key", sa.Text(), nullable=True))
    op.add_column("sessions", sa.Column("claude_md_s3_key", sa.Text(), nullable=True))
    op.add_column("sessions", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("sessions", sa.Column("business_summary", sa.Text(), nullable=True))
    op.add_column(
        "sessions",
        sa.Column("followup_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("last_manual_followup_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- session_turns ---
    op.create_table(
        "session_turns",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("speaker", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "seq", name="uq_session_turns_session_seq"),
        sa.CheckConstraint(
            "speaker IN ('user','agent')",
            name="ck_session_turns_speaker",
        ),
    )
    op.create_index(
        "idx_session_turns_session_seq",
        "session_turns",
        ["session_id", "seq"],
    )

    # --- admin_audit ---
    op.create_table(
        "admin_audit",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("actor_email", sa.String(255), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_session_id", sa.UUID(), nullable=True),
        sa.Column("target_user_id", sa.UUID(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["target_session_id"], ["sessions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_admin_audit_actor_email", "admin_audit", ["actor_email"])
    op.create_index("idx_admin_audit_action", "admin_audit", ["action"])
    op.create_index("idx_admin_audit_created_at", "admin_audit", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_admin_audit_created_at", table_name="admin_audit")
    op.drop_index("idx_admin_audit_action", table_name="admin_audit")
    op.drop_index("idx_admin_audit_actor_email", table_name="admin_audit")
    op.drop_table("admin_audit")

    op.drop_index("idx_session_turns_session_seq", table_name="session_turns")
    op.drop_table("session_turns")

    op.drop_column("sessions", "last_manual_followup_at")
    op.drop_column("sessions", "followup_sent_at")
    op.drop_column("sessions", "business_summary")
    op.drop_column("sessions", "summary")
    op.drop_column("sessions", "claude_md_s3_key")
    op.drop_column("sessions", "tdd_s3_key")
