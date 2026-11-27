-- Trusted dependency inspection. This walks proof terms; it does not rerun their elaboration.
run_cmd do
  let env ← Lean.getEnv
  let (_, state) := ((Lean.CollectAxioms.collect `MCVerifiedResult).run env).run {}
  let mut modules : Array String := #[]
  for name in state.visited.toList do
    if let some idx := env.getModuleIdxFor? name then
      let name := env.allImportedModuleNames[idx.toNat]!.toString
      if name.startsWith "MC." && !modules.contains name then
        modules := modules.push name
  Lean.logInfo ("MC_USED_MODULES:" ++ (Lean.toJson modules).compress)
