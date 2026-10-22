# Third-party material

| Path | Origin | Licence |
|---|---|---|
| `tests/upstream/scoring.ts` | Verbatim copy of `apps/api/src/modules/evaluation/scoring.ts` from `FG-AI4H/oci-platform` (commit in `tests/upstream/UPSTREAM_COMMIT`). Copyright (c) 2026 Global Initiative on AI for Health (GI-AI4H) | BSD 3-Clause |
| `fixtures/reference/labels.csv` | Verbatim copy of `packages/database/seed/fixtures/idrid-grading-demo/test-labels.hidden.csv` from `FG-AI4H/oci-platform`: grades of 30 images of the Indian Diabetic Retinopathy Image Dataset (IDRiD), Porwal et al., *Data* 3(3):25, 2018, https://doi.org/10.3390/data3030025. Changes made upstream: 30 testing-set images selected; this repository copies only the grade table, not the images | CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/) |

Runtime dependency: OpenDP (MIT). The integration script optionally uses the upstream `worker-eval` package from a local checkout of `FG-AI4H/oci-platform` (BSD 3-Clause); it is not redistributed here.

Everything else: BSD 3-Clause, `LICENSE`.
