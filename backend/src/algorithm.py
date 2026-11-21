import json
from sklearn.tree import DecisionTreeClassifier, export_text

FEATURE_NAMES = ["critical_fail_count", "average_score", "failed_item_count", "documentation_gap"]

def classify(features: dict):
    # Reproducible training set mirrors the regulator's triage labels for a compact rule extractor.
    X = [
        [0, 96, 0, 0], [0, 84, 1, 0], [0, 78, 2, 0],
        [0, 72, 2, 1], [1, 96, 1, 0], [1, 68, 3, 1],
        [0, 91, 0, 1], [0, 66, 3, 1], [2, 88, 2, 0],
    ]
    y = ["ready", "ready", "review", "hold", "hold", "hold", "review", "hold", "hold"]
    model = DecisionTreeClassifier(max_depth=3, random_state=1066)
    model.fit(X, y)
    vector = [[features[n] for n in FEATURE_NAMES]]
    prediction = model.predict(vector)[0]
    probability = float(max(model.predict_proba(vector)[0]))
    rules = export_text(model, feature_names=FEATURE_NAMES)
    return {"prediction": prediction, "probability": probability, "path_rules": rules, "features": features, "model": "DecisionTreeClassifier(random_state=1066,max_depth=3)"}

def build_features(items):
    failed = [x for x in items if x["result"] == "fail"]
    scores = [float(x["score"]) for x in items if x["result"] != "na"]
    return {
        "critical_fail_count": sum(1 for x in failed if x["critical"]),
        "average_score": round(sum(scores) / len(scores), 2) if scores else 0,
        "failed_item_count": len(failed),
        "documentation_gap": sum(1 for x in items if x["category"] == "documentation" and x["result"] == "fail"),
    }
