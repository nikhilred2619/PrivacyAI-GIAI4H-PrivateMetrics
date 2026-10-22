# Host configuration example (synthetic)

**Nothing here is a recommendation approved by ITU, the GI-AI4H organizers or any data host.** Every value is the host's decision; the open questions are listed in `docs/GAP_ANALYSIS.md` (G4, G7, G9, G15, G17). Data written by `make_synthetic.py` is synthetic.

```sh
python examples/host-config/make_synthetic.py --items 1000
python -m privatemetrics host-register --config examples/host-config/host.example.json \
    --epsilon-total 20 --patient-map examples/host-config/patients.csv
python -m privatemetrics ledger --ledger examples/host-config/ledger.db
```

## The decisions a host makes, and what the measurements say

| Decision | Where | What the evidence in this repository shows | Owner |
|---|---|---|---|
| ε per release | `epsilon_per_release` | Simulation (`docs/EXPERIMENT_REPORT.md` §2), ε = 1, moderate model: accuracy SD 0.13 at 30 items, 0.052 at 100, 0.019 at 300, 0.006 at 1,000. On the 30-item reference task, ε below 8 gave visibly biased, noisy metrics (`docs/REFERENCE_TASK_FIDELITY_REPORT.md`). Smaller ε protects each label more and costs utility | Host **[DECISION G4]** |
| Scope cap | `--epsilon-total` at registration | Total label-level ε any participant group can extract from the scope; releases allowed = cap / ε, for **all** participants together. Raising it later needs `change_cap` with a named approver | Host, with the organizers on allocation **[G4, G15]** |
| Allocation among participants | Not implemented here | The ledger is first-come, first-served across all dispatches; upstream quotas (3 per week, 10 per task, per participant) pace submissions but are not a privacy control. A fair-share policy would need an organizer decision | Organizers **[G4]** |
| Group factor | `group_factor`, checked with `--patient-map` | Patient-level ε = g × label-level ε per release; the ledger reports both. Without a map the factor is an unverified declaration | Host **[G9]** |
| Multi-machine scoring | — | One ledger file on one machine; several processes on that machine are safe (tested). Several machines sharing one ledger are not supported | Host **[G7]** |
| Backups and restoration | Host operations | Use SQLite's online backup. Never restore an older copy over a live ledger: it refunds spent budget and cannot be detected by this component | Host **[G17]** |

The values in `host.example.json` (ε = 1, g = 2) exist only so the example runs; with the cap of 20 in the command above, the scope allows 20 releases in total.
