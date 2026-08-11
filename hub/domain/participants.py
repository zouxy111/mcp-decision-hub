"""Participant roster rules (PRD 3.0 / FR-05). Pure function, no I/O."""

MIN_PARTICIPANTS = 2
MAX_PARTICIPANTS = 5


class ParticipantValidationError(ValueError):
    """Roster violates the 2-5 / dedup / initiator rules."""


def validate_participants(
    participant_ids: list[int],
    initiator_id: int,
    initiator_participates: bool,
) -> None:
    if len(set(participant_ids)) != len(participant_ids):
        raise ParticipantValidationError("参与人存在重复")
    if initiator_participates and initiator_id not in participant_ids:
        raise ParticipantValidationError("发起人勾选自答后必须包含在参与人列表中")
    if not initiator_participates and initiator_id in participant_ids:
        raise ParticipantValidationError("未勾选自答时发起人不能是参与人")
    if not MIN_PARTICIPANTS <= len(participant_ids) <= MAX_PARTICIPANTS:
        raise ParticipantValidationError("参与人数量必须为 2–5 名（不含未自答的发起人）")
