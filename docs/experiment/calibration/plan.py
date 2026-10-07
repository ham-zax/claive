#!/usr/bin/env python3
"""plan.py BATCH ARMS K [SEED]: print the interleaved run schedule as TSV.

Order is repeat -> shuffled task -> shuffled arm, so gateway drift and quota windows
do not line up with an arm. The seed belongs in the batch notes; the same arguments
always give the same schedule. Columns: n, repeat, task, arm, batch.
"""
import json, os, random, sys

if len(sys.argv) < 4:
    sys.exit(__doc__)
batch, arms, k = sys.argv[1], sys.argv[2].split(","), int(sys.argv[3])
seed = sys.argv[4] if len(sys.argv) > 4 else "0"
manifest = os.path.join(os.environ.get("CORPUS_ROOT", os.path.expanduser("~/corpus")), "calibration.json")
ids = [task["id"] for task in json.load(open(manifest))]
rng = random.Random(f"{batch}:{seed}")
n = 0
for repeat in range(1, k + 1):
    tasks = ids[:]
    rng.shuffle(tasks)
    for task in tasks:
        order = arms[:]
        rng.shuffle(order)
        for arm in order:
            n += 1
            print(n, repeat, task, arm, batch, sep="\t")
