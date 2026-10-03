import joblib
import os

# ── Folder where your trained .pkl files live
ML_MODELS_DIR = os.path.join(os.path.dirname(__file__), "ml_models")

full_time_model = None
part_time_model = None


def load_models():
    """
    Loads your two trained RandomForestClassifier models on server startup.

    Expected files inside ml_models/:
      FINAL_full_time_random_forest_model.pkl   (10 features — full time job matching)
      FINAL_part_time_random_forest_model.pkl   (5 features  — part time job matching)
    """
    global full_time_model, part_time_model

    full_time_path = os.path.join(
        ML_MODELS_DIR, "FINAL_full_time_random_forest_model.pkl")
    part_time_path = os.path.join(
        ML_MODELS_DIR, "FINAL_part_time_random_forest_model.pkl")

    if os.path.exists(full_time_path):
        full_time_model = joblib.load(full_time_path)
        print(
            f"✅ Loaded FINAL_full_time_random_forest_model.pkl  (features: {list(full_time_model.feature_names_in_)})")
    else:
        print("⚠️  FINAL_full_time_random_forest_model.pkl not found in ml_models/")

    if os.path.exists(part_time_path):
        part_time_model = joblib.load(part_time_path)
        print(
            f"✅ Loaded FINAL_part_time_random_forest_model.pkl  (features: {list(part_time_model.feature_names_in_)})")
    else:
        print("⚠️  FINAL_part_time_random_forest_model.pkl not found in ml_models/")
