#!/usr/bin/env python3
"""ProofSight command entrypoint.

Compatibility note: the original implementation file remains `vasper_qa.py`
for now so existing imports/history do not break; this wrapper exposes the
new ProofSight project name as the public command.
"""
from vasper_qa import main

if __name__ == "__main__":
    raise SystemExit(main())
