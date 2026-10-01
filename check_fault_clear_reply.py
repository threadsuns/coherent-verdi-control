"""One-shot, isolated capture of the raw ?F reply (HARDWARE_VALIDATION.md). Run:
python check_fault_clear_reply.py

This is the ONLY read in this session -- do not add others here. The result is
NOT proof of "no active faults" by itself: independently confirm its exact
meaning against the front panel and the operator/manual before ever using it
as active_fault_clear_reply. Do not clear faults as part of reading them.
"""

from coherent_verdi import VerdiController
from coherent_verdi.controller import VerdiError

PORT = "COM6"
MODEL = "V5"
BAUDRATE = 19200

with VerdiController(PORT, model=MODEL, baudrate=BAUDRATE, allow_writes=False) as laser:
    try:
        faults = laser.read("?F")
    except VerdiError as exc:
        print("Raw ?F exchange (unrecognized as a clear reply by default):")
        print(repr(exc))
        print()
        print("Compare this exact text to the front panel's fault/status display.")
        print("Confirm its meaning with the operator/manual before using it as")
        print("active_fault_clear_reply. Do not assume it means no active faults.")
    else:
        print("Parsed as a numeric fault-code list (no clear-reply text needed):")
        print(faults)
