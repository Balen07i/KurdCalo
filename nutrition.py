# nutrition.py
#
# Pure calculation helpers - no AI calls, no network, 100% free and instant.
# Covers: BMR/TDEE/daily targets from a user profile, and rule-based
# qualitative meal analysis (high protein / balanced / suited to goal etc).

ACTIVITY_MULTIPLIERS = {
    "sedentary": 1.2,    # little/no exercise
    "light": 1.375,      # light exercise 1-3 days/week
    "moderate": 1.55,    # moderate exercise 3-5 days/week
    "active": 1.725,     # hard exercise 6-7 days/week
    "very_active": 1.9,  # very hard exercise + physical job
}

ACTIVITY_LABELS_KURDISH = {
    "sedentary": "کەم جوڵە (کارکردنی دانیشتوو)",
    "light": "جوڵەی سووک (1-3 ڕۆژ لە هەفتەیەک وەرزش)",
    "moderate": "جوڵەی مامناوەند (3-5 ڕۆژ لە هەفتەیەک وەرزش)",
    "active": "جوڵەی زۆر (6-7 ڕۆژ لە هەفتەیەک وەرزش)",
    "very_active": "جوڵەی زۆر زۆر (وەرزشی قورس + کاری جەستەیی)",
}

GOAL_LABELS_KURDISH = {
    "lose": "کەمکردنەوەی چەوری",
    "maintain": "پاراستنی کێش",
    "gain": "بەهێزکردنی ماسولکە",
}

MIN_SAFE_KCAL = 1200  # never recommend below this, regardless of deficit math


def calculate_bmr(sex: str, weight_kg: float, height_cm: float, age: int) -> float:
    """Mifflin-St Jeor equation - the most widely validated free formula."""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + 5 if sex == "male" else base - 161


def calculate_targets(
    sex: str, weight_kg: float, height_cm: float, age: int, goal: str, activity_level: str
) -> dict:
    """Returns BMR, TDEE, and daily calorie/macro targets for a profile."""
    bmr = calculate_bmr(sex, weight_kg, height_cm, age)
    tdee = bmr * ACTIVITY_MULTIPLIERS.get(activity_level, 1.375)

    if goal == "lose":
        target_kcal = max(tdee - 500, MIN_SAFE_KCAL)
        protein_per_kg = 2.0  # higher protein protects muscle in a deficit
    elif goal == "gain":
        target_kcal = tdee + 300
        protein_per_kg = 1.8
    else:  # maintain
        target_kcal = tdee
        protein_per_kg = 1.6

    protein_g = weight_kg * protein_per_kg
    fat_g = (target_kcal * 0.25) / 9
    remaining_kcal = target_kcal - (protein_g * 4) - (fat_g * 9)
    carbs_g = max(remaining_kcal, 0) / 4

    return {
        "bmr": round(bmr),
        "tdee": round(tdee),
        "target_kcal": round(target_kcal),
        "target_protein_g": round(protein_g),
        "target_carbs_g": round(carbs_g),
        "target_fat_g": round(fat_g),
    }


def analyze_meal(total: dict, targets: dict | None, goal: str | None) -> str:
    """
    Rule-based (not AI) qualitative read on a meal - free and instant.
    total: {"kcal", "protein_g", "carbs_g", "fat_g"} for THIS meal.
    targets/goal: the user's profile, or None if they haven't set one up.
    Returns one short natural Kurdish sentence.
    """
    kcal = max(total.get("kcal", 0), 1)  # avoid div-by-zero
    protein_pct = (total.get("protein_g", 0) * 4) / kcal
    carbs_pct = (total.get("carbs_g", 0) * 4) / kcal
    fat_pct = (total.get("fat_g", 0) * 9) / kcal

    tags = []
    if protein_pct >= 0.28:
        tags.append("پرۆتینی بەرز")
    elif protein_pct <= 0.10:
        tags.append("پرۆتینی کەم")

    if carbs_pct >= 0.60:
        tags.append("کاربۆهایدرەیتی بەرز")

    if fat_pct >= 0.40:
        tags.append("چەوری بەرز")

    if not tags:
        tags.append("هاوسەنگ")

    tag_text = "، ".join(tags)

    goal_note = ""
    if goal == "lose" and protein_pct >= 0.25 and kcal <= 600:
        goal_note = " — گونجاوە بۆ ئامانجی کەمکردنەوەی چەوریت"
    elif goal == "gain" and protein_pct >= 0.20 and kcal >= 500:
        goal_note = " — گونجاوە بۆ ئامانجی بەهێزکردنی ماسولکەت"
    elif goal == "lose" and fat_pct >= 0.40:
        goal_note = " — ئەگەر ئامانجت کەمکردنەوەی چەورییە، وریابە لەم خواردنە"

    return f"📋 {tag_text}{goal_note}"
