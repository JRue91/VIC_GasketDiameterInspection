from __future__ import annotations

"""
Recipe model, persistence, and app-side evaluation for the Gasket Diameter
Inspection System.

A "recipe" is a full product profile: which Cognex job to load onto the sensor,
the scan parameters for that product, and the pass/fail criteria the application
applies to the measured result. The Cognex returns raw data; this module owns
the accept/reject verdict.

Recipes are persisted as a single JSON store (`../recipes/recipes.json`), a dict
keyed by recipe name. This mirrors the app-side nature of recipes (named config,
not per-run output like the timestamped calibration/data CSVs).
"""

import json
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

# Output dir sibling to ../calibration and ../data (gui.py os.chdir's into the
# script folder, so this relative path resolves the same way).
RECIPES_DIR = Path("../recipes")
RECIPES_FILE = RECIPES_DIR / "recipes.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Recipe:
    """A product profile: Cognex job + scan params + evaluation criteria.

    `calibration_file` is a basename in ../calibration (or None). `rms_limit`
    and `repeatability_limit` are None when that check is disabled.
    """
    name: str
    cognex_job: str = ""
    diameter_cell: str = "B21"
    step_deg: int = 5
    num_rotations: int = 1
    calibration_file: str | None = None
    nominal_diameter: float = 0.0
    tol_plus: float = 0.0
    tol_minus: float = 0.0
    rms_limit: float | None = None
    repeatability_limit: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Recipe":
        """Build a Recipe from a dict, ignoring unknown keys (forward-compat)."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_recipes() -> dict[str, Recipe]:
    """Load all recipes from the JSON store. Returns {} if none/unreadable."""
    if not RECIPES_FILE.is_file():
        return {}
    try:
        with open(RECIPES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[Recipe] Could not read {RECIPES_FILE}: {e}")
        return {}
    recipes: dict[str, Recipe] = {}
    for name, data in raw.items():
        try:
            data.setdefault("name", name)
            recipes[name] = Recipe.from_dict(data)
        except TypeError as e:
            print(f"[Recipe] Skipping malformed recipe '{name}': {e}")
    return recipes


def save_recipes(recipes: dict[str, Recipe]) -> None:
    """Write all recipes to the JSON store (creates ../recipes if needed)."""
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    payload = {name: r.to_dict() for name, r in recipes.items()}
    with open(RECIPES_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    print(f"[Recipe] Saved {len(recipes)} recipe(s) to {RECIPES_FILE}")


def list_recipe_names() -> list[str]:
    """Sorted list of recipe names in the store."""
    return sorted(load_recipes().keys())


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name: str          # e.g. "Diameter", "Roundness (RMS)", "Repeatability"
    value: float       # the measured quantity
    limit_desc: str    # human-readable limit, e.g. "5.000 +0.010/-0.010"
    passed: bool


@dataclass
class Verdict:
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)

    def summary(self) -> str:
        """One-line-per-check human-readable breakdown."""
        lines = [f"Result: {'PASS' if self.passed else 'FAIL'}"]
        for c in self.checks:
            mark = "PASS" if c.passed else "FAIL"
            lines.append(f"  [{mark}] {c.name}: {c.value:.4f}  (limit: {c.limit_desc})")
        return "\n".join(lines)


def evaluate_recipe(recipe: Recipe, fit, rotation_fits=None) -> Verdict:
    """Evaluate a circle-fit result against a recipe's criteria.

    `fit` is a DiameterScan.CircleFitResult (needs `.diameter`, `.residual_rms`).
    `rotation_fits` is an optional list of CircleFitResult (one per rotation) used
    for the repeatability check. Checks with unset limits are skipped.
    """
    checks: list[CheckResult] = []

    # --- Diameter: nominal - tol_minus <= d <= nominal + tol_plus ---
    lower = recipe.nominal_diameter - recipe.tol_minus
    upper = recipe.nominal_diameter + recipe.tol_plus
    checks.append(CheckResult(
        name="Diameter",
        value=fit.diameter,
        limit_desc=f"{recipe.nominal_diameter:.4f} "
                   f"+{recipe.tol_plus:.4f}/-{recipe.tol_minus:.4f} "
                   f"[{lower:.4f}, {upper:.4f}]",
        passed=(lower <= fit.diameter <= upper),
    ))

    # --- Roundness / RMS limit (optional) ---
    if recipe.rms_limit is not None:
        checks.append(CheckResult(
            name="Roundness (RMS)",
            value=fit.residual_rms,
            limit_desc=f"<= {recipe.rms_limit:.4f}",
            passed=(fit.residual_rms <= recipe.rms_limit),
        ))

    # --- Per-rotation repeatability (optional) ---
    if recipe.repeatability_limit is not None and rotation_fits:
        diameters = [f.diameter for f in rotation_fits]
        d_range = max(diameters) - min(diameters)
        checks.append(CheckResult(
            name="Repeatability (diameter range)",
            value=d_range,
            limit_desc=f"<= {recipe.repeatability_limit:.4f} "
                       f"across {len(diameters)} rotations",
            passed=(d_range <= recipe.repeatability_limit),
        ))

    return Verdict(passed=all(c.passed for c in checks), checks=checks)
