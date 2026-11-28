-- Trusted dependency inspection. Follow proof terms and their declaration types.
run_cmd do
  let env ← Lean.getEnv
  let mut todo : Array Lean.Name := #[`MCVerifiedResult]
  let mut visited : Lean.NameSet := {}
  let mut modules : Array String := #[]
  while let some decl := todo.back? do
    todo := todo.pop
    if visited.contains decl then
      continue
    visited := visited.insert decl
    if let some idx := env.getModuleIdxFor? decl then
      let mod := env.allImportedModuleNames[idx.toNat]!.toString
      if mod.startsWith "MC." then
        if !modules.contains mod then
          modules := modules.push mod
      else if !mod.startsWith "MCTargets." then
        -- Installed libraries cannot refer to workspace modules.
        continue
    if let some info := env.find? decl then
      todo := todo ++ info.type.getUsedConstants
      if let some value := info.value? (allowOpaque := true) then
        todo := todo ++ value.getUsedConstants
      if let .inductInfo info := info then
        todo := todo ++ info.ctors.toArray
  Lean.logInfo ("MC_USED_MODULES:" ++ (Lean.toJson modules).compress)
