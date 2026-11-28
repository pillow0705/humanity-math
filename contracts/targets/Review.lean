import Mathlib

-- These are proposition definitions, NOT proofs of the open problems.
def EulerTarget : Prop := Irrational Real.eulerMascheroniConstant
def PiETarget : Prop := Irrational (Real.pi + Real.exp 1)
def CollatzStep (m : Nat) : Nat := if m % 2 = 0 then m / 2 else 3 * m + 1
def CollatzTarget : Prop := ∀ n : Nat, 0 < n → ∃ k : Nat, (Nat.iterate CollatzStep k) n = 1

-- Mathlib's constant is the harmonic-minus-logarithm limit in the public statement.
theorem euler_limit_spec :
    Filter.Tendsto (fun n : Nat => (harmonic n : Real) - Real.log n)
      Filter.atTop (nhds Real.eulerMascheroniConstant) :=
  Real.tendsto_harmonic_sub_log

theorem euler_irrationality_spec :
    EulerTarget ↔ ∀ q : Rat, (q : Real) ≠ Real.eulerMascheroniConstant := by
  simp only [EulerTarget, Irrational, Set.mem_range, not_exists]

theorem pi_e_irrationality_spec :
    PiETarget ↔ ∀ q : Rat, (q : Real) ≠ Real.pi + Real.exp 1 := by
  simp only [PiETarget, Irrational, Set.mem_range, not_exists]

-- The map is defined on Nat; the positive-input condition is on the quantified initial value.
example : CollatzStep 0 = 0 := by decide
example : CollatzStep 1 = 4 := by decide
example : CollatzStep 2 = 1 := by decide
example : CollatzStep 3 = 10 := by decide
example : CollatzStep 4 = 2 := by decide
example : (Nat.iterate CollatzStep 0) 1 = 1 := rfl
example : (Nat.iterate CollatzStep 7) 3 = 1 := by decide

-- A disproof must exhibit a positive input whose entire trajectory avoids 1.
theorem collatz_negation_spec :
    (¬ CollatzTarget) ↔
      ∃ n : Nat, 0 < n ∧ ∀ k : Nat, (Nat.iterate CollatzStep k) n ≠ 1 := by
  classical
  simp only [CollatzTarget, not_forall, Classical.not_imp, not_exists, exists_prop]

-- Identity bridges check representation compatibility without proving any conjecture.
example (h : EulerTarget) : Irrational Real.eulerMascheroniConstant := h
example (h : PiETarget) : Irrational (Real.pi + Real.exp 1) := h
example (h : CollatzTarget) : ∀ n : Nat, 0 < n → ∃ k : Nat, (Nat.iterate (fun m : Nat => if m % 2 = 0 then m / 2 else 3 * m + 1) k) n = 1 := h

#print axioms euler_limit_spec
#print axioms euler_irrationality_spec
#print axioms pi_e_irrationality_spec
#print axioms collatz_negation_spec
