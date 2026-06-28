"""baseline schema — raw tables + extraction cache + decisions

Revision ID: 0001
Revises:
Create Date: 2026-06-28

Mirrors PRD §6. Every table carries org_id so multi-tenancy is additive, not a retrofit.
Raw tables are idempotent upsert targets (PK on org + natural key).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── raw_patients ───────────────────────────────────────────────────
    op.create_table(
        "raw_patients",
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("patient_id", sa.Text(), nullable=False),
        sa.Column("internal_id", sa.Integer(), nullable=False),
        sa.Column("facility_id", sa.Integer(), nullable=False),
        sa.Column("first_name", sa.Text()),
        sa.Column("last_name", sa.Text()),
        sa.Column("birth_date", sa.Date()),
        sa.Column("gender", sa.Text()),
        sa.Column("primary_payer_code", sa.Text()),
        sa.Column("last_modified_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("is_new_admission", sa.Boolean()),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("org_id", "patient_id"),
    )
    op.create_index("ix_raw_patients_facility", "raw_patients", ["org_id", "facility_id"])
    op.create_index(
        "ix_raw_patients_modified", "raw_patients", ["org_id", "last_modified_at"]
    )
    op.create_index("ix_raw_patients_internal_id", "raw_patients", ["org_id", "internal_id"])

    # ── raw_diagnoses ──────────────────────────────────────────────────
    op.create_table(
        "raw_diagnoses",
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Text(), nullable=False),
        sa.Column("icd10_code", sa.Text()),
        sa.Column("icd10_description", sa.Text()),
        sa.Column("clinical_status", sa.Text()),
        sa.Column("onset_date", sa.Date()),
        sa.Column("last_modified_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("org_id", "id"),
    )
    op.create_index("ix_raw_diagnoses_patient", "raw_diagnoses", ["org_id", "patient_id"])
    op.create_index(
        "ix_raw_diagnoses_code", "raw_diagnoses", ["org_id", "icd10_code", "clinical_status"]
    )

    # ── raw_coverage ───────────────────────────────────────────────────
    op.create_table(
        "raw_coverage",
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Text(), nullable=False),
        sa.Column("payer_name", sa.Text()),
        sa.Column("payer_code", sa.Text()),
        sa.Column("payer_type", sa.Text()),
        sa.Column("effective_from", sa.TIMESTAMP(timezone=True)),
        sa.Column("effective_to", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_modified_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("org_id", "id"),
    )
    op.create_index("ix_raw_coverage_patient", "raw_coverage", ["org_id", "patient_id"])
    op.create_index(
        "ix_raw_coverage_active",
        "raw_coverage",
        ["org_id", "payer_code", "effective_to"],
    )

    # ── raw_notes ──────────────────────────────────────────────────────
    op.create_table(
        "raw_notes",
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),   # internal int id
        sa.Column("source_org_id", sa.Text()),                   # API-provided 'ORG-101' etc.
        sa.Column("pcc_note_id", sa.Integer()),
        sa.Column("note_type", sa.Text()),
        sa.Column("effective_date", sa.TIMESTAMP(timezone=True)),
        sa.Column("note_text", sa.Text()),
        sa.Column("created_by", sa.Text()),
        sa.Column("note_label", sa.Text()),
        sa.Column("sync_version", sa.Integer()),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("org_id", "id"),
    )
    op.create_index("ix_raw_notes_patient", "raw_notes", ["org_id", "patient_id", "is_current"])
    op.create_index(
        "ix_raw_notes_effective", "raw_notes", ["org_id", "patient_id", "effective_date"]
    )

    # ── raw_assessments ────────────────────────────────────────────────
    op.create_table(
        "raw_assessments",
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("source_org_id", sa.Text()),
        sa.Column("pcc_assessment_id", sa.Integer()),
        sa.Column("assessment_type", sa.Text()),
        sa.Column("status", sa.Text()),
        sa.Column("assessment_date", sa.Date()),
        sa.Column("completion_date", sa.Date()),
        sa.Column("template_id", sa.Integer()),
        sa.Column("assessment_type_description", sa.Text()),
        sa.Column("raw_json", postgresql.JSONB()),
        sa.Column("sync_version", sa.Integer()),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("org_id", "id"),
    )
    op.create_index(
        "ix_raw_assessments_patient",
        "raw_assessments",
        ["org_id", "patient_id", "is_current"],
    )
    op.create_index(
        "ix_raw_assessments_date",
        "raw_assessments",
        ["org_id", "patient_id", "assessment_date"],
    )

    # ── sync_watermarks ────────────────────────────────────────────────
    op.create_table(
        "sync_watermarks",
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("facility_id", sa.Integer()),
        sa.Column("last_success_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True)),
        sa.PrimaryKeyConstraint("org_id", "endpoint", "facility_id"),
    )

    # ── extraction_cache ───────────────────────────────────────────────
    op.create_table(
        "extraction_cache",
        sa.Column("content_hash", sa.Text(), primary_key=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("extracted_json", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Float()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )

    # ── extracted_wounds ───────────────────────────────────────────────
    op.create_table(
        "extracted_wounds",
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("patient_id", sa.Text(), nullable=False),
        sa.Column("internal_id", sa.Integer(), nullable=False),
        sa.Column("wound_type", sa.Text()),
        sa.Column("wound_stage", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("length_cm", sa.Float()),
        sa.Column("width_cm", sa.Float()),
        sa.Column("depth_cm", sa.Float()),
        sa.Column("drainage_amount", sa.Text()),
        sa.Column("drainage_type", sa.Text()),
        sa.Column("confidence_wound_type", sa.Float()),
        sa.Column("confidence_measurements", sa.Float()),
        sa.Column("confidence_drainage", sa.Float()),
        sa.Column("overall_confidence", sa.Float()),
        sa.Column("source_table", sa.Text()),
        sa.Column("source_record_id", sa.Integer()),
        sa.Column("extraction_method", sa.Text()),
        sa.Column("extraction_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("extracted_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("org_id", "patient_id"),
    )

    # ── eligibility_decisions ──────────────────────────────────────────
    op.create_table(
        "eligibility_decisions",
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("patient_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("has_active_wound", sa.Boolean()),
        sa.Column("has_active_mcb", sa.Boolean()),
        sa.Column("has_measurements", sa.Boolean()),
        sa.Column("has_drainage", sa.Boolean()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("audit_json", postgresql.JSONB(), nullable=False),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("org_id", "patient_id"),
    )
    op.create_index(
        "ix_decisions_decision", "eligibility_decisions", ["org_id", "decision"]
    )
    op.create_index(
        "ix_decisions_decided_at", "eligibility_decisions", ["org_id", "decided_at"]
    )


def downgrade() -> None:
    op.drop_table("eligibility_decisions")
    op.drop_table("extracted_wounds")
    op.drop_table("extraction_cache")
    op.drop_table("sync_watermarks")
    op.drop_table("raw_assessments")
    op.drop_table("raw_notes")
    op.drop_table("raw_coverage")
    op.drop_table("raw_diagnoses")
    op.drop_table("raw_patients")
