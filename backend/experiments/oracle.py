"""Independent Oracle and Time-Sealed Hidden Label Store for OpsBlaze S1 Replay."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HiddenLabel:
    fixture_id: str
    is_negative_control: bool
    true_root_cause: str
    expected_action_kind: str  # e.g. "restart_pod", "scale_up", or "NO_OP"
    target_id: str


class IndependentOracle:
    """Independent verification oracle adjudicating replay decisions against time-sealed ground truth."""

    def __init__(self, version: str = "oracle-v1") -> None:
        self.version = version

    def adjudicate(
        self,
        hidden_label: HiddenLabel,
        proposed_action: str | None,
        abstained: bool,
    ) -> tuple[bool, str]:
        """Adjudicate strategy decision. Returns (is_correct, outcome_label)."""
        # Case 1: Negative Control
        if hidden_label.is_negative_control:
            if proposed_action is not None and proposed_action != "NO_OP":
                return (
                    False,
                    "FALSE_POSITIVE_MUTATION_ON_NEGATIVE_CONTROL",
                )
            return (True, "CORRECT_NO_OP_ON_NEGATIVE_CONTROL")

        # Case 2: Real Fault
        if abstained or proposed_action is None:
            return (False, "FALSE_ABSTENTION_ON_ACTIVE_FAULT")

        if proposed_action.strip().lower() == hidden_label.expected_action_kind.strip().lower():
            return (True, "CORRECT_RECOVERY_DECISION")

        return (
            False,
            "INCORRECT_ACTION",
        )
