"""Candidate Evaluation Manifest Service for Feature E5.

Responsible for:
- Manifest registration with Candidate Provenance Validation (INV-E5-3).
- Holdout Contamination Checking (INV-E5-11).
- Cryptographic SHA-256 Digest Computation.
- Freezing manifests into permanently immutable state (INV-E5-1).
- Manifest lifecycle management (REGISTERED -> FROZEN -> RUNNING -> CANCELLED / INVALIDATED).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.evaluation import (
    CandidateBundle,
    CandidateProvenanceType,
    CandidateProvenanceValidationError,
    EvaluationConcurrencyConflictError,
    HoldoutContaminationError,
    IncumbentBundle,
    ManifestLifecycleError,
    ManifestLifecycleState,
    ManifestNotFoundError,
    MetricSpec,
)
from backend.persistence.models import (
    CandidateEvaluationManifest,
    FailureCase,
    ImprovementProposal,
    OperationalLesson,
    OutcomeRevision,
)


def compute_bundle_digest(bundle_dict: dict[str, Any]) -> str:
    """Computes deterministic SHA-256 digest of a bundle payload."""
    canonical_json = json.dumps(bundle_dict, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def compute_manifest_sha256(manifest_data: dict[str, Any]) -> str:
    """Computes the authoritative SHA-256 digest over frozen manifest parameters."""
    canonical_dict = {
        "tenant_id": str(manifest_data["tenant_id"]),
        "title": manifest_data["title"],
        "incumbent_digest": manifest_data["incumbent_digest"],
        "candidate_digest": manifest_data["candidate_digest"],
        "candidate_provenance_type": str(manifest_data["candidate_provenance_type"]),
        "model_id": manifest_data["model_id"],
        "model_version": manifest_data["model_version"],
        "tool_catalog_version": manifest_data["tool_catalog_version"],
        "a2_grant_version": manifest_data["a2_grant_version"],
        "fixture_family": manifest_data["fixture_family"],
        "fixture_set_digest": manifest_data["fixture_set_digest"],
        "holdout_set_digest": manifest_data["holdout_set_digest"],
        "oracle_version": manifest_data["oracle_version"],
        "adjudication_rule_version": manifest_data["adjudication_rule_version"],
        "metric_spec": manifest_data["metric_spec"],
        "sample_size_plan": manifest_data["sample_size_plan"],
        "random_seed": manifest_data["random_seed"],
        "budget_ceiling_micros": manifest_data["budget_ceiling_micros"],
        "max_tokens_ceiling": manifest_data["max_tokens_ceiling"],
    }
    canonical_json = json.dumps(canonical_dict, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class CandidateManifestService:
    """Manages the creation, validation, freezing, and lifecycle of evaluation manifests."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def register_manifest(
        self,
        *,
        tenant_id: uuid.UUID,
        title: str,
        incumbent_bundle: IncumbentBundle | dict[str, Any],
        candidate_bundle: CandidateBundle | dict[str, Any],
        candidate_provenance_type: CandidateProvenanceType | str,
        source_proposal_id: uuid.UUID | None = None,
        source_lesson_ids: list[uuid.UUID] | None = None,
        model_id: str = "gemini-2.0-flash",
        model_version: str = "v1.0",
        tool_catalog_version: str = "v1",
        a2_grant_version: str = "v1",
        fixture_family: str = "k8s_container_crash",
        fixture_set_digest: str = "default_fixture_digest",
        holdout_set_digest: str = "default_holdout_digest",
        holdout_incident_ids: Sequence[str | uuid.UUID] | None = None,
        oracle_version: str = "oracle-v1",
        adjudication_rule_version: str = "v1",
        source_outcome_revision_ids: list[uuid.UUID] | None = None,
        metric_spec: MetricSpec | dict[str, Any] | None = None,
        sample_size_plan: int = 20,
        random_seed: int = 42,
        budget_ceiling_micros: int = 1000000,
        max_tokens_ceiling: int = 100000,
        created_by: str = "operator",
        auto_freeze: bool = False,
    ) -> CandidateEvaluationManifest:
        """Registers a new candidate evaluation manifest with provenance and holdout checks."""
        # 1. Validate Typed Bundles
        if isinstance(candidate_bundle, dict):
            cand_bundle_obj = CandidateBundle.model_validate(candidate_bundle)
        else:
            cand_bundle_obj = candidate_bundle

        if isinstance(incumbent_bundle, dict):
            inc_bundle_obj = IncumbentBundle.model_validate(incumbent_bundle)
        else:
            inc_bundle_obj = incumbent_bundle

        if metric_spec is None:
            spec_obj = MetricSpec()
        elif isinstance(metric_spec, dict):
            spec_obj = MetricSpec.model_validate(metric_spec)
        else:
            spec_obj = metric_spec

        prov_type = CandidateProvenanceType(candidate_provenance_type)
        cand_digest = compute_bundle_digest(cand_bundle_obj.model_dump(mode="json"))
        inc_digest = compute_bundle_digest(inc_bundle_obj.model_dump(mode="json"))

        async with self._session_factory() as session:
            # 2. Candidate Provenance Validation (INV-E5-3)
            source_proposal_version: int | None = None
            cand_source_incident_ids: set[str] = set()

            if prov_type == CandidateProvenanceType.E3_PROPOSAL:
                if not source_proposal_id:
                    raise CandidateProvenanceValidationError(
                        "source_proposal_id is required when candidate_provenance_type is E3_PROPOSAL"
                    )
                prop = await session.scalar(
                    select(ImprovementProposal).where(
                        ImprovementProposal.tenant_id == tenant_id,
                        ImprovementProposal.id == source_proposal_id,
                    )
                )
                if not prop:
                    raise CandidateProvenanceValidationError(
                        f"Improvement proposal {source_proposal_id} not found for tenant"
                    )
                if prop.status not in ("EVALUATION_READY", "REVIEWED"):
                    raise CandidateProvenanceValidationError(
                        f"Improvement proposal {source_proposal_id} status '{prop.status}' is not eligible for evaluation (must be EVALUATION_READY or REVIEWED)"
                    )
                source_proposal_version = prop.version

                # Collect incident IDs from proposal cluster for holdout contamination check
                cases = (
                    await session.scalars(
                        select(FailureCase.incident_id).where(
                            FailureCase.tenant_id == tenant_id,
                            FailureCase.cluster_id == prop.cluster_id,
                        )
                    )
                ).all()
                for c_inc in cases:
                    cand_source_incident_ids.add(str(c_inc))

            elif prov_type == CandidateProvenanceType.E4_LESSON_SET:
                if not source_lesson_ids:
                    raise CandidateProvenanceValidationError(
                        "source_lesson_ids is required when candidate_provenance_type is E4_LESSON_SET"
                    )
                lessons = (
                    await session.scalars(
                        select(OperationalLesson).where(
                            OperationalLesson.tenant_id == tenant_id,
                            OperationalLesson.lesson_id.in_(source_lesson_ids),
                        )
                    )
                ).all()
                found_lesson_ids = {l.lesson_id for l in lessons}
                missing_ids = set(source_lesson_ids) - found_lesson_ids
                if missing_ids:
                    raise CandidateProvenanceValidationError(
                        f"Source lessons not found: {[str(m) for m in missing_ids]}"
                    )
                for lesson in lessons:
                    if lesson.lifecycle_state != "ACTIVE":
                        raise CandidateProvenanceValidationError(
                            f"Source lesson {lesson.lesson_id} is '{lesson.lifecycle_state}', not ACTIVE"
                        )
                    if lesson.source_outcome_revision_id:
                        # Find incident id for this revision with tenant scoping
                        rev = await session.scalar(
                            select(OutcomeRevision).where(
                                OutcomeRevision.tenant_id == tenant_id,
                                OutcomeRevision.id == lesson.source_outcome_revision_id,
                            )
                        )
                        if rev:
                            cand_source_incident_ids.add(str(rev.incident_id))

            elif prov_type == CandidateProvenanceType.COMBINED_OFFLINE_STUDY:
                if not source_proposal_id and not source_lesson_ids:
                    raise CandidateProvenanceValidationError(
                        "COMBINED_OFFLINE_STUDY requires at least source_proposal_id or source_lesson_ids"
                    )
                if source_proposal_id:
                    prop = await session.scalar(
                        select(ImprovementProposal).where(
                            ImprovementProposal.tenant_id == tenant_id,
                            ImprovementProposal.id == source_proposal_id,
                        )
                    )
                    if not prop:
                        raise CandidateProvenanceValidationError(
                            f"Improvement proposal {source_proposal_id} not found for tenant"
                        )
                    if prop.status not in ("EVALUATION_READY", "REVIEWED"):
                        raise CandidateProvenanceValidationError(
                            f"Improvement proposal {source_proposal_id} status '{prop.status}' is not eligible for evaluation (must be EVALUATION_READY or REVIEWED)"
                        )
                    source_proposal_version = prop.version
                    cases = (
                        await session.scalars(
                            select(FailureCase.incident_id).where(
                                FailureCase.tenant_id == tenant_id,
                                FailureCase.cluster_id == prop.cluster_id,
                            )
                        )
                    ).all()
                    for c_inc in cases:
                        cand_source_incident_ids.add(str(c_inc))

                if source_lesson_ids:
                    lessons = (
                        await session.scalars(
                            select(OperationalLesson).where(
                                OperationalLesson.tenant_id == tenant_id,
                                OperationalLesson.lesson_id.in_(source_lesson_ids),
                            )
                        )
                    ).all()
                    found_lesson_ids = {l.lesson_id for l in lessons}
                    missing_ids = set(source_lesson_ids) - found_lesson_ids
                    if missing_ids:
                        raise CandidateProvenanceValidationError(
                            f"Source lessons not found: {[str(m) for m in missing_ids]}"
                        )
                    for lesson in lessons:
                        if lesson.lifecycle_state != "ACTIVE":
                            raise CandidateProvenanceValidationError(
                                f"Source lesson {lesson.lesson_id} is '{lesson.lifecycle_state}', not ACTIVE"
                            )
                        if lesson.source_outcome_revision_id:
                            rev = await session.scalar(
                                select(OutcomeRevision).where(
                                    OutcomeRevision.tenant_id == tenant_id,
                                    OutcomeRevision.id == lesson.source_outcome_revision_id,
                                )
                            )
                            if rev:
                                cand_source_incident_ids.add(str(rev.incident_id))

            # Add source outcome revisions
            for rev_id in (source_outcome_revision_ids or []):
                rev = await session.scalar(
                    select(OutcomeRevision).where(
                        OutcomeRevision.tenant_id == tenant_id,
                        OutcomeRevision.id == rev_id,
                    )
                )
                if rev:
                    cand_source_incident_ids.add(str(rev.incident_id))
                else:
                    raise CandidateProvenanceValidationError(
                        f"Source outcome revision {rev_id} not found for tenant"
                    )

            # 3. Holdout Contamination Gate (INV-E5-11)
            if holdout_incident_ids:
                holdout_str_set = {str(hid) for hid in holdout_incident_ids}
                intersection = cand_source_incident_ids.intersection(holdout_str_set)
                if intersection:
                    raise HoldoutContaminationError(
                        f"Holdout contamination detected (INV-E5-11): candidate source incidents {sorted(intersection)} overlap with frozen holdout set"
                    )

            # 4. Construct Manifest Record
            manifest_id = uuid.uuid4()
            manifest_dict_for_sha = {
                "tenant_id": tenant_id,
                "title": title,
                "incumbent_digest": inc_digest,
                "candidate_digest": cand_digest,
                "candidate_provenance_type": prov_type.value,
                "model_id": model_id,
                "model_version": model_version,
                "tool_catalog_version": tool_catalog_version,
                "a2_grant_version": a2_grant_version,
                "fixture_family": fixture_family,
                "fixture_set_digest": fixture_set_digest,
                "holdout_set_digest": holdout_set_digest,
                "oracle_version": oracle_version,
                "adjudication_rule_version": adjudication_rule_version,
                "metric_spec": spec_obj.model_dump(),
                "sample_size_plan": sample_size_plan,
                "random_seed": random_seed,
                "budget_ceiling_micros": budget_ceiling_micros,
                "max_tokens_ceiling": max_tokens_ceiling,
            }
            manifest_sha = compute_manifest_sha256(manifest_dict_for_sha)

            manifest = CandidateEvaluationManifest(
                id=manifest_id,
                tenant_id=tenant_id,
                title=title,
                manifest_sha256=manifest_sha,
                incumbent_bundle=inc_bundle_obj.model_dump(mode="json"),
                incumbent_digest=inc_digest,
                candidate_bundle=cand_bundle_obj.model_dump(mode="json"),
                candidate_digest=cand_digest,
                candidate_provenance_type=prov_type.value,
                source_proposal_id=source_proposal_id,
                source_proposal_version=source_proposal_version,
                source_lesson_ids=[str(lid) for lid in (source_lesson_ids or [])],
                model_id=model_id,
                model_version=model_version,
                tool_catalog_version=tool_catalog_version,
                a2_grant_version=a2_grant_version,
                fixture_family=fixture_family,
                fixture_set_digest=fixture_set_digest,
                holdout_set_digest=holdout_set_digest,
                oracle_version=oracle_version,
                adjudication_rule_version=adjudication_rule_version,
                source_outcome_revision_ids=[str(rid) for rid in (source_outcome_revision_ids or [])],
                metric_spec=spec_obj.model_dump(),
                sample_size_plan=sample_size_plan,
                random_seed=random_seed,
                budget_ceiling_micros=budget_ceiling_micros,
                max_tokens_ceiling=max_tokens_ceiling,
                lifecycle_state=ManifestLifecycleState.FROZEN.value if auto_freeze else ManifestLifecycleState.REGISTERED.value,
                version=1,
                created_by=created_by,
                frozen_at=datetime.now(UTC) if auto_freeze else None,
            )

            session.add(manifest)
            await session.commit()
            await session.refresh(manifest)
            return manifest

    async def freeze_manifest(
        self,
        *,
        tenant_id: uuid.UUID,
        manifest_id: uuid.UUID,
        expected_version: int = 1,
    ) -> CandidateEvaluationManifest:
        """Freezes a REGISTERED manifest into permanent immutable state (INV-E5-1)."""
        async with self._session_factory() as session:
            manifest = await session.scalar(
                select(CandidateEvaluationManifest).where(
                    CandidateEvaluationManifest.tenant_id == tenant_id,
                    CandidateEvaluationManifest.id == manifest_id,
                )
            )
            if not manifest:
                raise ManifestNotFoundError(f"Manifest {manifest_id} not found for tenant")

            if manifest.version != expected_version:
                raise EvaluationConcurrencyConflictError(
                    f"Manifest version conflict: expected {expected_version}, found {manifest.version}"
                )

            if manifest.lifecycle_state != ManifestLifecycleState.REGISTERED.value:
                if manifest.lifecycle_state == ManifestLifecycleState.FROZEN.value:
                    return manifest  # Idempotent
                raise ManifestLifecycleError(
                    f"Manifest {manifest_id} cannot be frozen from state '{manifest.lifecycle_state}'"
                )

            manifest.lifecycle_state = ManifestLifecycleState.FROZEN.value
            manifest.frozen_at = datetime.now(UTC)
            manifest.version += 1
            manifest.updated_at = datetime.now(UTC)

            await session.commit()
            await session.refresh(manifest)
            return manifest

    async def cancel_manifest(
        self,
        *,
        tenant_id: uuid.UUID,
        manifest_id: uuid.UUID,
        reason: str = "",
    ) -> CandidateEvaluationManifest:
        """Cancels a registered or frozen manifest."""
        async with self._session_factory() as session:
            manifest = await session.scalar(
                select(CandidateEvaluationManifest).where(
                    CandidateEvaluationManifest.tenant_id == tenant_id,
                    CandidateEvaluationManifest.id == manifest_id,
                )
            )
            if not manifest:
                raise ManifestNotFoundError(f"Manifest {manifest_id} not found")

            if manifest.lifecycle_state in (
                ManifestLifecycleState.COMPLETED.value,
                ManifestLifecycleState.INVALIDATED.value,
                ManifestLifecycleState.CANCELLED.value,
            ):
                raise ManifestLifecycleError(
                    f"Manifest {manifest_id} cannot be cancelled from terminal state '{manifest.lifecycle_state}'"
                )

            manifest.lifecycle_state = ManifestLifecycleState.CANCELLED.value
            manifest.version += 1
            manifest.updated_at = datetime.now(UTC)

            await session.commit()
            await session.refresh(manifest)
            return manifest

    async def get_manifest(
        self,
        *,
        tenant_id: uuid.UUID,
        manifest_id: uuid.UUID,
    ) -> CandidateEvaluationManifest:
        """Retrieves an evaluation manifest by ID."""
        async with self._session_factory() as session:
            manifest = await session.scalar(
                select(CandidateEvaluationManifest).where(
                    CandidateEvaluationManifest.tenant_id == tenant_id,
                    CandidateEvaluationManifest.id == manifest_id,
                )
            )
            if not manifest:
                raise ManifestNotFoundError(f"Manifest {manifest_id} not found")
            return manifest
