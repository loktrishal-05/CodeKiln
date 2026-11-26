"""Candidate state machine. A candidate only advances when its gate passes; no LLM self-reports."""
from enum import StrEnum


class State(StrEnum):
    QUEUED = "QUEUED"
    PROFILED = "PROFILED"
    ENV_READY = "ENV_READY"
    PLANNED = "PLANNED"
    DIVERSITY_OK = "DIVERSITY_OK"
    SYNTHESIZED = "SYNTHESIZED"
    BLIND_READER_OK = "BLIND_READER_OK"
    STATE_A_P2P_OK = "STATE_A_P2P_OK"  # base passes existing tests
    STATE_B_F2P_FAIL = "STATE_B_F2P_FAIL"  # base + feature tests fail on missing behavior (x3)
    STATE_C_ALL_PASS = "STATE_C_ALL_PASS"  # base + reference patch passes F2P and P2P (x3)
    MUTATION_OK = "MUTATION_OK"  # >=80% semantic mutants killed
    RTM_OK = "RTM_OK"  # every REQ-n has >=1 F2P test
    SCRUBBED = "SCRUBBED"  # single orphan commit, no site-packages leaks
    PACKAGED = "PACKAGED"
    REJECTED = "REJECTED"


PIPELINE = [s for s in State if s is not State.REJECTED]
MAX_REPAIR_RETRIES = 2  # SYNTH / STATE_B / STATE_C
MAX_STRENGTHEN_PASSES = 1  # after MUTATION_FAIL
