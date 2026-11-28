# Fixed mathematical targets

These files define the three propositions the final verifier must check. They do not contain proofs of the open problems.

| Problem | Lean proposition | Accepted final direction |
|---|---|---|
| P007: Euler–Mascheroni constant | `Irrational Real.eulerMascheroniConstant` | Prove |
| P009: pi plus e | `Irrational (Real.pi + Real.exp 1)` | Prove |
| P003: Collatz | Every positive natural number reaches 1 after a finite number of iterations of the stated map | Prove or disprove |

`Irrational x` is Mathlib's predicate that the real number x is not the image of any rational under the canonical embedding. In particular, e is `Real.exp 1`, not the Euler–Mascheroni constant. Mathlib's `Real.tendsto_harmonic_sub_log` identifies its Euler–Mascheroni constant with the harmonic-minus-logarithm limit used by the public problem statement.

The exact Collatz expression in P003.json uses `Nat.iterate` with the map `fun m : Nat => if m % 2 = 0 then m / 2 else 3 * m + 1`. The domain is natural numbers, and the quantified starting value must satisfy `0 < n`. Division by 2 occurs only on the even branch and is therefore exact. `k : Nat` includes zero: starting at 1 is already successful. The ordinary map is retained at 1 (1 → 4 → 2 → 1); the goal is reaching 1, not staying at 1.

The negative Collatz target is exactly the negation of the full positive proposition. `Review.lean` proves its equivalence to the existence of a positive natural n such that every finite iterate is different from 1. A finite experiment that has not yet reached 1 is insufficient.

Each target's `definitions` consists only of `import Mathlib`. There are no new axioms, conjecture declarations, custom constants, placeholders or problem-specific definitions introduced into the trusted checker. Agents can use `import Mathlib` and copy the expression directly as their final theorem type. Definitionally equivalent helper definitions are accepted by Lean. An entry theorem that assumes the original conjecture as a hypothesis does not have the required type.

`Review.lean` checks the existing constant definition, rational-number interpretation, iteration edge cases, negation equivalence and exact expression compatibility. Its identity-bridge examples have explicit hypotheses solely to check type compatibility; they are not submitted solutions. The integration tests separately confirm that both unrelated True proofs and a theorem assuming its own target fail the final verifier.

The original review used Lean 4.19.0 / Mathlib v4.19.0 and isolated test storage. The real Lean CI suite checks the same target sources under the current toolchain. `validation.json` binds the reviewed source and target hashes to that environment. The review is by Codex, with Lean checking the stated equivalences and examples; it is not independent mathematical peer review of a conjecture proof.

After validation, run `python -m seeds.install_targets`. It installs all targets in one file-store transaction, refuses to replace an already fixed target with a different one, and leaves the problem statuses unchanged. Run it after `python -m seeds.bootstrap` on a fresh installation. GET `/api/v1/problems/{id}` and claimed research packages expose the actual fixed `lean_target` and its hash. Final checks continue to reject a modified target hash.
