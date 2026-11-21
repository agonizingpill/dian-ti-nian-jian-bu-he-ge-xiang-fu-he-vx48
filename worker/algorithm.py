from sklearn.tree import DecisionTreeClassifier, export_text
FEATURE_NAMES = ["critical_fail_count", "average_score", "failed_item_count", "documentation_gap"]

def classify(features):
    X = [[0,96,0,0],[0,84,1,0],[0,78,2,0],[0,72,2,1],[1,96,1,0],[1,68,3,1],[0,91,0,1],[0,66,3,1],[2,88,2,0]]
    y = ["ready","ready","review","hold","hold","hold","review","hold","hold"]
    model = DecisionTreeClassifier(max_depth=3, random_state=1066)
    model.fit(X, y)
    vector = [[features[n] for n in FEATURE_NAMES]]
    return {
        "prediction": model.predict(vector)[0],
        "probability": float(max(model.predict_proba(vector)[0])),
        "path_rules": export_text(model, feature_names=FEATURE_NAMES),
        "model": "DecisionTreeClassifier(random_state=1066,max_depth=3)",
    }
