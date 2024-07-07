"""Reference implementation of ChillDKG.

The public API consists of all functions with docstrings, including the types in
their arguments and return values, and the exceptions they raise; see also the
`__all__` list. All other definitions are internal.
"""

from secrets import token_bytes as random_bytes
from typing import Tuple, List, NamedTuple, NewType, Optional

from secp256k1ref.secp256k1 import Scalar, GE
from secp256k1ref.bip340 import schnorr_sign, schnorr_verify
from secp256k1ref.keys import pubkey_gen_plain
from secp256k1ref.util import int_from_bytes, bytes_from_int

from .vss import VSS, VSSCommitment
from .simplpedpop import DKGOutput
from . import encpedpop
from .util import (
    BIP_TAG,
    tagged_hash_bip_dkg,
    prf,
    InvalidContributionError,
)

__all__ = [
    # Functions
    "hostpubkey",
    "session_params",
    "participant_step1",
    "participant_step2",
    "participant_finalize",
    "coordinator_step1",
    "coordinator_finalize",
    "recover",
    # Exceptions
    "InvalidContributionError",
    "InvalidRecoveryDataError",
    "DuplicateHostpubkeyError",
    "SessionNotFinalizedError",
    # Types
    "SessionParams",
    "DKGOutput",
    "ParticipantMsg1",
    "ParticipantMsg2",
    "ParticipantState1",
    "ParticipantState2",
    "CoordinatorMsg1",
    "CoordinatorMsg2",
    "CoordinatorState",
    "RecoveryData",
]


###
### Exceptions
###
# TODO Document in all public functions what exceptions they can raise


class DuplicateHostpubkeyError(ValueError):
    pass


class ThresholdError(ValueError):
    pass


# TODO Use this
class SeedError(ValueError):
    pass


class SessionNotFinalizedError(Exception):
    pass


class InvalidRecoveryDataError(Exception):
    pass


###
### Equality check protocol CertEq
###


CERTEQ_MSG_TAG = BIP_TAG + "certeq message"


def certeq_message(x: bytes, idx: int) -> bytes:
    return idx.to_bytes(4, "big")


def certeq_participant_step(hostseckey: bytes, idx: int, x: bytes) -> bytes:
    msg = certeq_message(x, idx)
    return schnorr_sign(
        msg, hostseckey, aux_rand=random_bytes(32), challenge_tag=CERTEQ_MSG_TAG
    )


def certeq_cert_len(n: int) -> int:
    return 64 * n


def certeq_verify(hostpubkeys: List[bytes], x: bytes, cert: bytes) -> bool:
    n = len(hostpubkeys)
    if len(cert) != certeq_cert_len(n):
        return False
    for i in range(n):
        msg = certeq_message(x, i)
        valid = schnorr_verify(
            msg,
            hostpubkeys[i][1:33],
            cert[i * 64 : (i + 1) * 64],
            challenge_tag=CERTEQ_MSG_TAG,
        )
        if not valid:
            return False
    return True


def certeq_coordinator_step(sigs: List[bytes]) -> bytes:
    cert = b"".join(sigs)
    return cert


###
### Host keys
###


def hostseckey(seed: bytes) -> bytes:
    return prf(seed, "chilldkg hostseckey")


def hostkeypair(seed: bytes) -> Tuple[bytes, bytes]:
    if len(seed) != 32:
        raise ValueError
    seckey = hostseckey(seed)
    pubkey = pubkey_gen_plain(seckey)
    return (seckey, pubkey)


def hostpubkey(seed: bytes) -> bytes:
    """Compute the participant's host public key from the seed.

    This is the long-term cryptographic identity of the participant. It is
    derived deterministically from the secret seed.

    Arguments:
        seed: This participant's long-term secret seed (32 bytes).
            The seed must be 32 bytes of cryptographically secure randomness
            with sufficient entropy to be unpredictable. All outputs of a
            successful participant in a session can be recovered from (a backup
            of) the seed and per-session recovery data.

            The same seed (and thus host public key) can be used in multiple DKG
            sessions. A host public key can be correlated to the threshold
            public key resulting from a DKG session only by parties who observed
            the session, namely the participants, the coordinator (and any

    Returns:
        The host public key.

    Raises:
        ValueError: If the length of `seed` is not 32 bytes.
    """
    return hostkeypair(seed)[1]


###
### Session input and outputs
###


# It would be more idiomatic Python to make this a real (data)class, perform
# data validation in the constructor, and add methods to it, but let's stick to
# simple tuples in the public API in order to keep it approachable to readers
# who are not too familiar with Python.
class SessionParams(NamedTuple):
    """A `SessionParams` tuple holds the common parameters of session.

    Attributes:
        hostpubkeys: Ordered list of the host public keys of all participants.
        t: The participation threshold `t`.
            This is the number of participants that will be required to sign.
            It must hold that `1 <= t <= len(hostpubkeys)` and `t <= 2^32 - 1`.

    Participants must ensure that they have obtained authentic host
    public keys of all the other participants in the session to make
    sure that they run the DKG and generate a threshold public key with
    the intended set of participants. This is analogous to traditional
    threshold signatures (known as "multisig" in the Bitcoin community,
    [[BIP383](https://github.com/bitcoin/bips/blob/master/bip-0383.mediawiki)],
    where the participants need to obtain authentic extended public keys
    ("xpubs") from the other participants to generate multisig
    addresses, or MuSig2
    [[BIP327](https://github.com/bitcoin/bips/blob/master/bip-0327.mediawiki)],
    where the participants need to obtain authentic individual public
    keys of the other participants to generate an aggregated public key.

    All participants and the coordinator in a session must be given an identical
    `SessionParams` tuple. In particular, the host public keys must be in the
    same order. This will make sure that honest participants agree on the order
    as part of the session, which is useful if the order carries an implicit
    meaning in the application (e.g., if the first `t` participants are the
    primary participants for signing and the others are fallback participants).
    If there is no canonical order of the participants in the application, the
    caller can sort the list of host public keys with the [KeySort algorithm
    specified in
    BIP327](https://github.com/bitcoin/bips/blob/master/bip-0327.mediawiki#key-sorting)
    to abstract away from the order.
    """

    hostpubkeys: List[bytes]
    t: int


def params_validate(params: SessionParams) -> None:
    (hostpubkeys, t) = params

    if not (1 <= t <= len(hostpubkeys)):
        raise ThresholdError

    for i, hostpubkey in enumerate(hostpubkeys):
        try:
            _ = GE.from_bytes_compressed(hostpubkey)
        except ValueError as e:
            raise InvalidContributionError(
                i, "Participant has provided an invalid host public key"
            ) from e

    if len(hostpubkeys) != len(set(hostpubkeys)):
        raise DuplicateHostpubkeyError


def params_id(params: SessionParams) -> bytes:
    """Returns the parameters ID, a unique representation of the`SessionParams`.

    In the common scenario that the participants obtain host public keys from
    the other participants over channels that do not provide end-to-end
    authentication of the sending participant (e.g., if the participants simply
    send their unauthenticated host public keys to the coordinator who is
    supposed to relay them to all participants), the parameters ID serves as a
    convenient way to perform an out-of-band comparison of all host public keys.
    It is a collision-resistant cryptographic hash of the `SessionParams`
    object. As a result, if all participants have obtained an identical
    parameters ID (as can be verified out-of-band), then they all agree on all
    host public keys and the threshold `t`, and in particular, all participants
    have obtained authentic public host keys.

    Returns:
        bytes: The parameters ID, a 32-byte string.

    Raises:
        InvalidContributionError(i,...): If `hostpubkeys[i]` is not a valid
            public key.
        DuplicateHostpubkeyError: If `hostpubkeys` contains duplicates.
        ThresholdError: If `1 <= t <= len(hostpubkeys)` does not hold.
        OverflowError: If `t >= 2^32` (so `t` cannot be serialized in 4 bytes).
    """
    params_validate(params)
    (hostpubkeys, t) = params

    t_bytes = t.to_bytes(4, byteorder="big")  # OverflowError if t >= 2^32
    params_id = tagged_hash_bip_dkg(
        "params_id",
        t_bytes + b"".join(hostpubkeys),
    )
    assert len(params_id) == 32
    return params_id


RecoveryData = NewType("RecoveryData", bytes)


###
### Messages
###


class ParticipantMsg1(NamedTuple):
    enc_pmsg: encpedpop.ParticipantMsg


class ParticipantMsg2(NamedTuple):
    sig: bytes


class CoordinatorMsg1(NamedTuple):
    enc_cmsg: encpedpop.CoordinatorMsg
    enc_secshares: List[Scalar]


class CoordinatorMsg2(NamedTuple):
    cert: bytes


def deserialize_recovery_data(
    b: bytes,
) -> Tuple[int, VSSCommitment, List[bytes], List[bytes], List[Scalar], bytes]:
    rest = b

    # Read t (4 bytes)
    if len(rest) < 4:
        raise ValueError
    t, rest = int.from_bytes(rest[:4], byteorder="big"), rest[4:]

    # Read sum_coms (33*t bytes)
    if len(rest) < 33 * t:
        raise ValueError
    sum_coms, rest = (
        VSSCommitment.from_bytes_and_t(rest[: 33 * t], t),
        rest[33 * t :],
    )

    # Compute n
    n, remainder = divmod(len(rest), (33 + 33 + 32 + 64))
    if remainder != 0:
        raise ValueError

    # Read hostpubkeys (33*n bytes)
    if len(rest) < 33 * n:
        raise ValueError
    hostpubkeys, rest = [rest[i : i + 33] for i in range(0, 33 * n, 33)], rest[33 * n :]

    # Read pubnonces (33*n bytes)
    if len(rest) < 33 * n:
        raise ValueError
    pubnonces, rest = [rest[i : i + 33] for i in range(0, 33 * n, 33)], rest[33 * n :]

    # Read enc_secshares (32*n bytes)
    if len(rest) < 32 * n:
        raise ValueError
    enc_secshares, rest = (
        [Scalar(int_from_bytes(rest[i : i + 32])) for i in range(0, 32 * n, 32)],
        rest[32 * n :],
    )

    # Read cert
    cert_len = certeq_cert_len(n)
    if len(rest) < cert_len:
        raise ValueError
    cert, rest = rest[:cert_len], rest[cert_len:]

    if len(rest) != 0:
        raise ValueError
    return (t, sum_coms, hostpubkeys, pubnonces, enc_secshares, cert)


###
### Participant
###


class ParticipantState1(NamedTuple):
    params: SessionParams
    idx: int
    enc_state: encpedpop.ParticipantState


class ParticipantState2(NamedTuple):
    params: SessionParams
    eq_input: bytes
    dkg_output: DKGOutput


def participant_step1(
    seed: bytes, params: SessionParams, random: bytes
) -> Tuple[ParticipantState1, ParticipantMsg1]:
    """Perform a participant's first step of a ChillDKG session.

    The returned `ParticipantState1` should be kept locally, and the returned
    `ParticipantMsg1` should be sent to the coordinator.

    Arguments:
        seed: Participant's long-term secret seed (32 bytes).
        params: Common session parameters.
        random: FRESH random byte string (32 bytes).

    Returns:
        ParticipantState1: The participant's state after this step.
        ParticipantMsg1: The first message for the coordinator.

    Raises:
        ValueError: If the participant's host public key is not in argument
        `hostpubkeys`.
        ValueError: If the length of `seed` is not 32 bytes.
        InvalidContributionError(i,...): If `hostpubkeys[i]` is not a valid
            public key.
        DuplicateHostpubkeyError: If `hostpubkeys` contains duplicates.
        ThresholdError: If `1 <= t <= len(hostpubkeys)` does not hold.
        OverflowError: If `t >= 2^32` (so `t` cannot be serialized in 4 bytes).
    """
    hostseckey, hostpubkey = hostkeypair(seed)  # ValueError if len(seed) != 32

    params_validate(params)
    (hostpubkeys, t) = params

    idx = hostpubkeys.index(hostpubkey)  # ValueError if not found
    enc_state, enc_pmsg = encpedpop.participant_step1(seed, t, hostpubkeys, idx, random)
    state1 = ParticipantState1(params, idx, enc_state)
    return state1, ParticipantMsg1(enc_pmsg)


def participant_step2(
    seed: bytes,
    state1: ParticipantState1,
    cmsg1: CoordinatorMsg1,
) -> Tuple[ParticipantState2, ParticipantMsg2]:
    """Perform a participant's second step of a ChillDKG session.

    The returned `ParticipantState2` should be kept locally, and the returned
    `ParticipantMsg2` should be sent to the coordinator.

    Arguments:
        seed: Participant's long-term secret seed (32 bytes).
        state1: The participant's state after the previous step.
        cmsg1: The first message received from the coordinator.

    Returns:
        ParticipantState2: The participant's state after this step.
        ParticipantMsg2: The second message for the coordinator.

    Raises:
        TODO
    """
    (hostseckey, _) = hostkeypair(seed)
    (params, idx, enc_state) = state1
    enc_cmsg, enc_secshares = cmsg1

    dkg_output, eq_input = encpedpop.participant_step2(
        enc_state, hostseckey, enc_cmsg, enc_secshares[idx]
    )
    # Include the enc_shares in eq_input to ensure that participants agree on all
    # shares, which in turn ensures that they have the right recovery data.
    eq_input += b"".join([bytes_from_int(int(share)) for share in enc_secshares])
    state2 = ParticipantState2(params, eq_input, dkg_output)
    sig = certeq_participant_step(hostseckey, idx, eq_input)
    pmsg2 = ParticipantMsg2(sig)
    return state2, pmsg2


def participant_finalize(
    state2: ParticipantState2, cmsg2: CoordinatorMsg2
) -> Tuple[DKGOutput, RecoveryData]:
    """Perform a participant's final step of a ChillDKG session.

    If this functions returns properly (without an exception), then this
    participant deems the DKG session successful. It is, however, possible that
    other participants have received a `cmsg2` from the coordinator that made
    them raise a `SessionNotFinalizedError` instead, or that they have not
    received a `cmsg2` from the coordinator at all. These participants can, at
    any point in time in the future (e.g., when initiating a signing session),
    be convinced to deem the session successful by presenting them the recovery
    data, from which they can recover the DKG outputs using the `recover`
    function.

    **Warning:**
    Changing perspectives, this implies that even when obtaining a
    `SessionNotFinalizedError`, you MUST NOT conclude that the DKG session has
    failed, and as a consequence, you MUST NOT erase the seed. The underlying
    reason is that it is possible that some other participant deems the DKG
    session successful, and uses the resulting threshold public key (e.g., by
    sending funds to it). That other participant can, at any point in the
    future, wish to convince us of the success of the DKG session by presenting
    us recovery data.

    Arguments:
        state2: Participant's state after the previous step.

    Returns:
        DKGOutput: The DKG output.
        bytes: The serialized recovery data.

    Raises:
        SessionNotFinalizedError: If finalizing the DKG session was not
        successful from this participant's point of view (see above).
    """
    (params, eq_input, dkg_output) = state2
    if not certeq_verify(params.hostpubkeys, eq_input, cmsg2.cert):
        raise SessionNotFinalizedError
    return dkg_output, RecoveryData(eq_input + cmsg2.cert)


###
### Coordinator
###


class CoordinatorState(NamedTuple):
    params: SessionParams
    eq_input: bytes
    dkg_output: DKGOutput


def coordinator_step1(
    pmsgs1: List[ParticipantMsg1], params: SessionParams
) -> Tuple[CoordinatorState, CoordinatorMsg1]:
    """Perform the coordinator's first step of a ChillDKG session.

    The returned `CoordinatorState` should be kept locally, and the returned
    `CoordinatorMsg1` should be sent to all participants.

    Arguments:
        pmsgs1: List of first messages received from the participants.
        params: Common session parameters.

    Returns:
        CoordinatorState: The coordinator's state after this step.
        CoordinatorMsg1: The first message for all participants.

    Raises:
        InvalidContributionError(i,...): If `hostpubkeys[i]` is not a valid
            public key.
        DuplicateHostpubkeyError: If `hostpubkeys` contains duplicates.
        ThresholdError: If `1 <= t <= len(hostpubkeys)` does not hold.
        OverflowError: If `t >= 2^32` (so `t` cannot be serialized in 4 bytes).
    """
    params_validate(params)
    (hostpubkeys, t) = params

    enc_cmsg, dkg_output, eq_input, enc_secshares = encpedpop.coordinator_step(
        [pmsg1.enc_pmsg for pmsg1 in pmsgs1], t, hostpubkeys
    )
    eq_input += b"".join([bytes_from_int(int(share)) for share in enc_secshares])
    state = CoordinatorState(params, eq_input, dkg_output)
    cmsg1 = CoordinatorMsg1(enc_cmsg, enc_secshares)
    return state, cmsg1


def coordinator_finalize(
    state: CoordinatorState, pmsgs2: List[ParticipantMsg2]
) -> Tuple[CoordinatorMsg2, DKGOutput, RecoveryData]:
    """Perform the coordinator's final step of a ChillDKG session.

    The returned `CoordinatorMsg2` should be sent to all participants.

    Since the coordinator does not have a secret shares, the DKG output will
    have the `secshare` field set to `None`.

    Arguments:
        state: Coordinator's state after the previous step.
        pmsgs2: List of second messages received from the participants.

    Returns:
        CoordinatorMsg2: The second message for all participants
        DKGOutput: The DKG output.
        bytes: The serialized recovery data.

    Raises:
        SessionNotFinalizedError: If finalizing the DKG session was not
            successful from the point of view of the coordinator. In this case,
            it is, in principle, possible to recover the DKG outputs of the
            coordinator using the recovery data from a successful participant,
            should one exist. Any such successful participant would need to have
            received messages from other participants via communication channel
            beside the coordinator (or be malicious).
    """
    (params, eq_input, dkg_output) = state
    cert = certeq_coordinator_step([pmsg2.sig for pmsg2 in pmsgs2])
    if not certeq_verify(params.hostpubkeys, eq_input, cert):
        raise SessionNotFinalizedError
    return CoordinatorMsg2(cert), dkg_output, RecoveryData(eq_input + cert)


###
### Recovery
###


def recover(
    seed: Optional[bytes], recovery_data: RecoveryData
) -> Tuple[DKGOutput, SessionParams]:
    """Recover the DKG output of a session from the seed and recovery data.

    This function serves two different purposes:
    1. To recover from a `SessionNotFinalizedError` after obtaining the recovery
       data from another participant or the coordinator (see
       `participant_finalize`).
    2. To reproduce the DKG outputs on a new device, e.g., to recover from a
       backup after data loss, or to clone a participant or the coordinator to
       a second device.

    Arguments:
        seed: This participant's long-term secret seed (32 bytes) or `None` if
            recovering the coordinator.
        recovery_data: Recovery data from a successful session.

    Returns:
        DKGOutput: The recovered DKG output.
        SessionParams: The common parameters of the recovered session.

    Raises:
        InvalidRecoveryDataError: If recovery failed due to invalid recovery
            data or recovery data that does not match the provided seed.
    """
    try:
        (t, sum_coms, hostpubkeys, pubnonces, enc_secshares, cert) = (
            deserialize_recovery_data(recovery_data)
        )
    except Exception as e:
        raise InvalidRecoveryDataError("Failed to deserialize recovery data") from e

    n = len(hostpubkeys)
    params = SessionParams(hostpubkeys, t)
    params_validate(params)

    # Verify cert
    certeq_verify(hostpubkeys, recovery_data[: 64 * n], cert)

    # Compute threshold pubkey and individual pubshares
    threshold_pubkey = sum_coms.commitment_to_secret()
    pubshares = [sum_coms.pubshare(i) for i in range(n)]

    if seed:
        # Find our hostpubkey
        hostseckey, hostpubkey = hostkeypair(seed)
        try:
            idx = hostpubkeys.index(hostpubkey)
        except ValueError as e:
            raise InvalidRecoveryDataError("Seed and recovery data don't match") from e

        # Decrypt share
        enc_context = encpedpop.serialize_enc_context(t, hostpubkeys)
        their_pubnonces = pubnonces.copy()
        pubnonce = their_pubnonces.pop(idx)
        session_seed = encpedpop.derive_session_seed(seed, pubnonce, enc_context)
        secshare = encpedpop.decrypt_sum(
            hostseckey,
            hostpubkeys[idx],
            their_pubnonces,
            enc_secshares[idx],
            enc_context,
            idx,
        )

        # Derive my_share
        vss = VSS.generate(session_seed, t)
        my_share = vss.secshare_for(idx)
        secshare += my_share

        # This is just a sanity check. Our signature is valid, so we have done
        # this check already during the actual session.
        assert VSSCommitment.verify_secshare(secshare, pubshares[idx])
    else:
        secshare = None

    dkg_output = DKGOutput(
        None if secshare is None else secshare.to_bytes(),
        threshold_pubkey.to_bytes_compressed(),
        [pubshare.to_bytes_compressed() for pubshare in pubshares],
    )
    return dkg_output, params
