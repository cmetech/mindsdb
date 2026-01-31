"""convert_company_id_to_string

Revision ID: 54ed56beb47a
Revises: 608e376c19a7
Create Date: 2025-10-24 15:05:30.187143

OSCAR Customization: Added explicit VARCHAR lengths for MySQL compatibility.
"""

from alembic import op
import sqlalchemy as sa
import mindsdb.interfaces.storage.db  # noqa


# revision identifiers, used by Alembic.
revision = "54ed56beb47a"
down_revision = "608e376c19a7"
branch_labels = None
depends_on = None


def upgrade():
    # OSCAR: Use String(255) instead of String() for MySQL compatibility
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.INTEGER(), type_=sa.String(255), existing_nullable=True)

    with op.batch_alter_table("file", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.INTEGER(), type_=sa.String(255), existing_nullable=True)

    with op.batch_alter_table("integration", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.INTEGER(), type_=sa.String(255), existing_nullable=True)

    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.INTEGER(), type_=sa.String(255), existing_nullable=True)

    with op.batch_alter_table("jobs_history", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.INTEGER(), type_=sa.String(255), existing_nullable=True)

    with op.batch_alter_table("json_storage", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.INTEGER(), type_=sa.String(255), existing_nullable=True)

    with op.batch_alter_table("llm_log", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.INTEGER(), type_=sa.String(255), existing_nullable=False)

    with op.batch_alter_table("predictor", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.INTEGER(), type_=sa.String(255), existing_nullable=True)

    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.INTEGER(), type_=sa.String(255), existing_nullable=True)

    with op.batch_alter_table("queries", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.INTEGER(), type_=sa.String(255), existing_nullable=True)

    with op.batch_alter_table("query_context", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.INTEGER(), type_=sa.String(255), existing_nullable=True)

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.INTEGER(), type_=sa.String(255), existing_nullable=True)

    with op.batch_alter_table("view", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.INTEGER(), type_=sa.String(255), existing_nullable=True)


def downgrade():
    # OSCAR: Use String(255) instead of String() for MySQL compatibility
    with op.batch_alter_table("view", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.String(255), type_=sa.INTEGER(), existing_nullable=True)

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.String(255), type_=sa.INTEGER(), existing_nullable=True)

    with op.batch_alter_table("query_context", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.String(255), type_=sa.INTEGER(), existing_nullable=True)

    with op.batch_alter_table("queries", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.String(255), type_=sa.INTEGER(), existing_nullable=True)

    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.String(255), type_=sa.INTEGER(), existing_nullable=True)

    with op.batch_alter_table("predictor", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.String(255), type_=sa.INTEGER(), existing_nullable=True)

    with op.batch_alter_table("llm_log", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.String(255), type_=sa.INTEGER(), existing_nullable=False)

    with op.batch_alter_table("json_storage", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.String(255), type_=sa.INTEGER(), existing_nullable=True)

    with op.batch_alter_table("jobs_history", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.String(255), type_=sa.INTEGER(), existing_nullable=True)

    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.String(255), type_=sa.INTEGER(), existing_nullable=True)

    with op.batch_alter_table("integration", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.String(255), type_=sa.INTEGER(), existing_nullable=True)

    with op.batch_alter_table("file", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.String(255), type_=sa.INTEGER(), existing_nullable=True)

    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.alter_column("company_id", existing_type=sa.String(255), type_=sa.INTEGER(), existing_nullable=True)
