import pytest

from hub.domain.participants import ParticipantValidationError, validate_participants


def test_two_participants_ok():
    validate_participants([10, 11], initiator_id=1, initiator_participates=False)


def test_five_participants_ok():
    validate_participants([10, 11, 12, 13, 14], initiator_id=1, initiator_participates=False)


def test_one_participant_rejected():
    with pytest.raises(ParticipantValidationError, match="2–5"):
        validate_participants([10], initiator_id=1, initiator_participates=False)


def test_six_participants_rejected():
    with pytest.raises(ParticipantValidationError, match="2–5"):
        validate_participants([10, 11, 12, 13, 14, 15], initiator_id=1,
                              initiator_participates=False)


def test_duplicate_participants_rejected():
    with pytest.raises(ParticipantValidationError, match="重复"):
        validate_participants([10, 10], initiator_id=1, initiator_participates=False)


def test_initiator_without_self_answer_rejected():
    with pytest.raises(ParticipantValidationError, match="未勾选自答"):
        validate_participants([1, 10], initiator_id=1, initiator_participates=False)


def test_initiator_self_answer_counts_as_slot():
    # initiator + 1 other = 2 slots, valid
    validate_participants([1, 10], initiator_id=1, initiator_participates=True)


def test_self_answer_flag_but_initiator_missing_rejected():
    with pytest.raises(ParticipantValidationError, match="自答"):
        validate_participants([10, 11], initiator_id=1, initiator_participates=True)
