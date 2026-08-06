# v0.3.1 Code Wiki Specificity Experiment

## Status

`DEVELOPMENT_PASSED_REGRESSION_PENDING`

## Frozen baseline

- Dataset: `learn_claude_code_qa_dev_v031.json`
- Dataset SHA256: `3085859283115b351ce1c38a6bf1c111b1ac7e669f96ed1e3aa4f9fc31610b7d`
- Answer accuracy: 60.0%
- Source recall@3: 100.0%
- Citation grounding: 88.9%
- Multi-source coverage: 100.0%
- Abstention accuracy: 0.0%
- Repository path isolation: 100.0%

## Prior evidence

- Preserving complete snake_case identifiers produced 90% Answer and 100% Citation, but
  Abstention remained 0%.
- Removing code kind words produced 100% Abstention, but identifier selection failures remained.

Both isolated candidates were rejected and production code was reverted.

## Candidate

Apply the two complementary specificity rules together:

1. preserve complete snake_case identifiers while retaining existing component terms;
2. remove generic code kind words from Code Wiki overlap.

Do not change page routing, ranking tuple order, thresholds, labels, dependencies, or document-page
morphology.

## Preregistered hypothesis

The four development failures have two shared causes:

1. dropping complete snake_case identifiers creates ambiguous fact ties for `check_permission`,
   `run_todo_write`, and the two-source `agent_loop` question;
2. generic code kind words satisfy the minimum overlap for the unsupported vector-database
   question.

Combining only these two previously isolated rules should fix all four failures without changing
the three-page source recall.

## Acceptance

The candidate is accepted only if all frozen development gates pass:

- Answer accuracy >= 80%
- Source recall@3 = 100%
- Citation grounding = 100%
- Multi-source coverage = 100%
- Abstention accuracy = 100%
- Repository path isolation = 100%

Only an accepted development candidate may run the frozen confirmation split once.

## Development result

- Candidate diff SHA256: `05d746a0bfb7ef64956806596c6ddf02e5190f208b6c586aaaef8e6423125524`
- Result:
  [`learn_claude_code_qa_dev_v031_codewiki_specificity_candidate.json`](../demo/results/learn_claude_code_qa_dev_v031_codewiki_specificity_candidate.json)
- Result SHA256: `a283fa6bbed7a6867cf5efc3cf99ce673e97b2320003852de48507107871b450`

| Metric | Baseline | Candidate |
| --- | ---: | ---: |
| Answer accuracy | 60.0% | 100.0% |
| Source recall@3 | 100.0% | 100.0% |
| Citation grounding | 88.9% | 100.0% |
| Multi-source coverage | 100.0% | 100.0% |
| Abstention accuracy | 0.0% | 100.0% |
| Repository path isolation | 100.0% | 100.0% |

Error flow:

- `dev-s03-check-permission-signature`: wrong `check_*` fact to exact `check_permission`;
- `dev-s05-todo-write-signature`: wrong `run_*` fact to exact `run_todo_write`;
- `dev-agent-loop-multi-source`: module fact to both expected function signatures and Sources;
- `dev-unknown-vector-database`: unsupported answer to `unknown`.

All ten development cases pass. Confirmation remains unrun until the candidate Commit, public
regressions, and structural benchmarks are frozen and pass.
