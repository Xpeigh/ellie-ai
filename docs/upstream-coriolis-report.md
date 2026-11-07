# Draft: issue for EDCD/coriolis-data

**Not posted.** This is text for the project owner to read, adjust and post on the public EDCD/coriolis-data repository if they choose.

---

**Title:** Data differences found by replaying real EngineerCraft journal events (specials costs, missile rack specials, a typo, duplicates)

I replayed 257 `EngineerCraft` events from my own journals (Sept 2026) against `modifications/blueprints.json`, `specials.json` and `modules.json`. All 48 distinct (blueprint, grade) combinations matched exactly. These differ:

**1. `special_weapon_lightweight` ("Stripped Down", weapons)** lists `Carbon: 3`. Two crafts on a pulse laser (2026-09-26) consumed 5 Carbon, 5 Salvaged Alloys and 1 Tin.

**2. `special_drag_munitions`** lists `Molybdenum: 5`. One craft on a basic missile rack (2026-09-25) consumed 5 Carbon, 5 Grid Resistors and 2 Molybdenum. (A single observation.)

**3. Missile racks have no experimental effects in `modules.json`.** The `mr` group has no `specials` list, yet Drag Munitions and Thermal Cascade were both applied to `hpt_basicmissilerack_fixed_small` (2026-09-25). The Thermal Cascade cost matched `specials.json` exactly. `special_drag_munitions` is listed only for `fc`, `special_thermal_cascade` only for `c`.

**4. Typo in `specials.json`:** `special_lock_breaker` has the component `"Adaptive Encyptors Capture"`; the game and `blueprints.json` use `"Adaptive Encryptors Capture"`.

**5. Duplicate effect:** `special_mass_lock` ("Mass Lock Munition") and `special_mass_lock_munition` ("Mass lock munition") are both listed for torpedo pylons with the same cost.

**6. Missing costs:** `CargoRack_IncreasedCapacity` grade 5 has empty `components`; `special_choke_canister`, `special_feedback_cascade`, `special_plasma_slug` and `special_super_penetrator` have no `components`.

How I checked: a small script reads each `EngineerCraft` event (`BlueprintName` equals the `fdname`, `Level` the grade, `ApplyExperimentalEffect` marks an effect craft, `Ingredients` the materials consumed) and compares them with the data. I can share it and the raw events if useful.
